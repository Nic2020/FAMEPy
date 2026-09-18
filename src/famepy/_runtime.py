# SPDX-License-Identifier: MIT
"""Process-owned library loader and the single initialized CHLI session.

One process holds at most one initialized CHLI owner. Every operation runs
under one reentrant lock for its whole duration, because work databases, ITEM
options, wildcard cursors, output redirection and the extended error state are
process-global inside the library. A runtime inherited across ``fork`` is
refused, even through a freshly created wrapper, because the child inherits
initialized native state it cannot safely use; use ``spawn``.

Ownership is released only by a successful finalization. When ``cfmfin``
fails, the session becomes ``broken`` and keeps the process ownership, so no
other session can initialize until a retried finalization succeeds. The
native library is fixed for the process lifetime once loaded: no unload is
attempted and no second library can be chosen in the same process.
"""

from __future__ import annotations

import ctypes as ct
import os
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ._discovery import Candidate, discover
from ._errors import (
    FameError,
    InheritedRuntimeError,
    LibraryLoadError,
    LicensingConfigurationError,
    RuntimeStateError,
    UnsupportedOperationError,
)
from ._native import CtypesNative, NativeInterface, Sentinels, read_extended_error

if TYPE_CHECKING:
    from ._database import Database

LOCK = threading.RLock()

# Set in a forked child when the parent had loaded or initialized native state.
_INHERITED = False
_LOADED_NATIVE = False
_OWNER: Session | None = None
_DEFAULT: Session | None = None


def _after_fork() -> None:
    global LOCK, _INHERITED
    LOCK = threading.RLock()
    if _LOADED_NATIVE or _OWNER is not None or (_DEFAULT is not None and _DEFAULT.is_loaded):
        _INHERITED = True


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _check_not_inherited() -> None:
    if _INHERITED:
        raise InheritedRuntimeError(
            "This process inherited CHLI state across fork; start a spawned process instead."
        )


class Runtime:
    """Keep library/dependency-directory handles alive; forbid use after fork.

    Internal only. An explicit path loads native code and must be trusted.
    This owner does not initialize/finalize CHLI or promise an unload operation.
    """

    def __init__(self, candidate: Candidate) -> None:
        self._candidate = candidate
        self._pid = os.getpid()
        self._library: Any = None
        self._directories: list[Any] = []

    @property
    def is_loaded(self) -> bool:
        return self._library is not None

    def load(self) -> Any:
        global _LOADED_NATIVE
        if self._pid != os.getpid():
            raise InheritedRuntimeError(
                "An inherited CHLI runtime cannot be used after fork; use spawn."
            )
        _check_not_inherited()
        with LOCK:
            if self._library is not None:
                return self._library
            directories: list[Any] = []
            try:
                if sys.platform == "win32":
                    for directory in self._candidate.trusted_directories:
                        directories.append(os.add_dll_directory(str(directory)))
                library = ct.CDLL(str(self._candidate.path))
            except OSError as error:
                for handle in directories:
                    handle.close()
                raise LibraryLoadError(
                    errno=error.errno, winerror=getattr(error, "winerror", None)
                ) from None
            self._directories = directories
            self._library = library
            _LOADED_NATIVE = True
            return library


class ExtendedErrorRetrieval:
    """Opt-in retrieval callables. The package ships no vendor declaration.

    ``query_length`` returns the message length excluding the terminator and
    ``fetch`` fills a NUL-terminated caller buffer. The session invokes both
    immediately at a failure, under the same lock and before any other native
    call, and attaches the text to the raised error.
    """

    def __init__(
        self,
        query_length: Callable[[NativeInterface], int],
        fetch: Callable[[NativeInterface, Any], None],
    ) -> None:
        self.query_length = query_length
        self.fetch = fetch


class Session:
    """The process-wide initialized CHLI owner.

    States: ``created`` -> ``loaded`` -> ``initialized`` <-> ``finalized``; a
    failed finalization leaves ``broken`` and keeps process ownership. Each
    initialization increments the generation so that database handles from
    earlier generations are stale.
    """

    def __init__(
        self,
        candidate: Candidate | None = None,
        *,
        native: NativeInterface | None = None,
        environ: Mapping[str, str] | None = None,
        require_licensing_environment: bool | None = None,
    ) -> None:
        if candidate is None and native is None:
            raise TypeError("A Session needs a discovered library or an injected backend.")
        self._candidate = candidate
        self._runtime = Runtime(candidate) if candidate is not None else None
        self._native = native
        self._environ = environ
        if require_licensing_environment is None:
            require_licensing_environment = native is None
        self._require_licensing = require_licensing_environment
        self._pid = os.getpid()
        self._state = "created" if native is None else "loaded"
        self._generation = 0
        self._sentinels: Sentinels | None = None
        self._databases: dict[int, Database] = {}
        self._work: Database | None = None
        self._version: float | None = None
        self._last_extended_error: bytes | None = None
        self.extended_error_retrieval: ExtendedErrorRetrieval | None = None
        self.extended_error_capture_failure: str | None = None
        self.last_cleanup_statuses: tuple[int, ...] = ()

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def is_loaded(self) -> bool:
        return self._native is not None

    @property
    def is_initialized(self) -> bool:
        return self._state == "initialized"

    @property
    def sentinels(self) -> Sentinels:
        if self._sentinels is None:
            raise RuntimeStateError("Missing-value sentinels are available after initialization.")
        return self._sentinels

    @property
    def open_databases(self) -> tuple[Database, ...]:
        return tuple(self._databases.values())

    def __repr__(self) -> str:
        return f"Session(state={self._state!r}, generation={self._generation})"

    def _check_process(self) -> None:
        _check_not_inherited()
        if self._pid != os.getpid():
            raise InheritedRuntimeError(
                "This session belongs to another process; start a spawned process instead."
            )

    # -- lifecycle --------------------------------------------------------

    def load(self) -> NativeInterface:
        """Load the library without initializing CHLI."""
        self._check_process()
        with LOCK:
            if self._native is None:
                assert self._runtime is not None
                self._native = CtypesNative(self._runtime.load())
                self._state = "loaded"
            return self._native

    def initialize(self) -> Session:
        """Initialize CHLI once per process; idempotent while initialized."""
        global _OWNER
        self._check_process()
        with LOCK:
            if self._state == "initialized":
                return self
            if self._state == "broken":
                raise RuntimeStateError(
                    "A failed finalization left this runtime unusable; retry finalize() first."
                )
            if _OWNER is not None and _OWNER is not self:
                if _OWNER.state == "broken":
                    raise RuntimeStateError(
                        "The process runtime owner failed to finalize and still owns CHLI; "
                        "retry famepy.finalize() before initializing another session."
                    )
                raise RuntimeStateError("Another session already owns the initialized runtime.")
            native = self.load()
            if self._require_licensing:
                environ = os.environ if self._environ is None else self._environ
                if not environ.get("FAME"):
                    raise LicensingConfigurationError(
                        "Set the FAME environment variable to the installation before "
                        "initialization; the library requires it for licensing."
                    )
            native.initialize()
            self._generation += 1
            self._state = "initialized"
            _OWNER = self
            try:
                self._sentinels = native.sentinels()
            except Exception:
                self._teardown(native)
                raise
            return self

    def _teardown(self, native: NativeInterface) -> None:
        """Cleanup after a failure that followed a successful cfmini.

        A failed cfmfin keeps process ownership (state ``broken``) so that no
        other session can initialize over a possibly still-active library.
        """
        global _OWNER
        self._sentinels = None
        try:
            native.finalize()
        except FameError:
            self._state = "broken"
            return
        self._state = "finalized"
        if _OWNER is self:
            _OWNER = None

    def version(self) -> float:
        with self.operation("version") as native:
            if self._version is None:
                self._version = native.version()
            return self._version

    def finalize(self) -> None:
        """Close tracked databases, finalize CHLI and invalidate all handles.

        Ownership is released only when cfmfin succeeds. A failure leaves the
        session ``broken`` and still owning the process; calling finalize()
        again retries cfmfin.
        """
        global _OWNER
        self._check_process()
        with LOCK:
            if self._state == "broken":
                native = self._native
                assert native is not None
                native.finalize()
                self._state = "finalized"
                if _OWNER is self:
                    _OWNER = None
                return
            if self._state != "initialized":
                return
            native = self._native
            assert native is not None
            statuses: list[int] = []
            for database in list(self._databases.values()):
                try:
                    native.close_database(database.key)
                except FameError as error:
                    statuses.append(error.status)
                database._invalidate()
            self._databases.clear()
            self._work = None
            self.last_cleanup_statuses = tuple(statuses)
            self._version = None
            self._sentinels = None
            try:
                native.finalize()
            except FameError:
                self._state = "broken"
                raise
            self._state = "finalized"
            if _OWNER is self:
                _OWNER = None

    def reset(self) -> Session:
        """Finalize and initialize again; existing database handles become stale."""
        with LOCK:
            self.finalize()
            return self.initialize()

    # -- operations -------------------------------------------------------

    @contextmanager
    def operation(self, name: str) -> Iterator[NativeInterface]:
        """Hold the process lock for a complete native operation.

        A FameError raised inside gets the opt-in extended text attached
        before the lock is released and before any other native call.
        """
        self._check_process()
        with LOCK:
            if self._state != "initialized" or self._native is None:
                raise RuntimeStateError(f"CHLI must be initialized before {name}.")
            try:
                yield self._native
            except FameError as error:
                self._attach_extended_error(self._native, error)
                raise

    def _register(self, database: Database) -> None:
        self._databases[database.key] = database

    def _unregister(self, database: Database) -> None:
        self._databases.pop(database.key, None)
        if self._work is database:
            self._work = None

    # -- extended error text ------------------------------------------------

    def _capture_extended_error(self, native: NativeInterface) -> bytes | None:
        """Read the opt-in text now, under the lock; never raise from here."""
        retrieval = self.extended_error_retrieval
        if retrieval is None:
            return None
        try:
            text = read_extended_error(
                lambda: retrieval.query_length(native),
                lambda buffer: retrieval.fetch(native, buffer),
            )
        except Exception as failure:  # noqa: BLE001 - capture must not mask the failure
            self.extended_error_capture_failure = type(failure).__name__
            self._last_extended_error = None
            return None
        self.extended_error_capture_failure = None
        self._last_extended_error = text
        return text

    def _attach_extended_error(self, native: NativeInterface, error: FameError) -> None:
        if error.extended_captured or self.extended_error_retrieval is None:
            return
        error.extended_captured = True
        error.extended_text = self._capture_extended_error(native)

    def extended_error_text(self) -> bytes:
        """Return the text captured at the most recent failure.

        Raises UnsupportedOperationError when no retrieval has been configured
        (the vendor declaration needed to size the buffer is not established)
        and RuntimeStateError when the last failure captured nothing. The text
        is never placed in exception messages.
        """
        if self.extended_error_retrieval is None:
            raise UnsupportedOperationError(
                "Extended error text needs a verified cfmlerr/cfmferr declaration; none is "
                "configured. Status codes remain available on FameError."
            )
        with LOCK:
            if self._last_extended_error is None:
                raise RuntimeStateError(
                    "No extended error text was captured at the most recent failure."
                )
            return self._last_extended_error


# -- module-level convenience -------------------------------------------


def default_session(
    library: str | os.PathLike[str] | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
) -> Session:
    """Return the process default session, discovering the library once.

    The library is fixed for the process lifetime once loaded; a different
    library can only be chosen while nothing has been loaded yet.
    """
    global _DEFAULT
    _check_not_inherited()
    with LOCK:
        if _DEFAULT is None or _DEFAULT._pid != os.getpid():
            _DEFAULT = Session(discover(library, root=root))
        elif library is not None or root is not None:
            if _DEFAULT.is_loaded:
                raise RuntimeStateError(
                    "The native library is fixed for the process lifetime once loaded; "
                    "use a new process for a different library or root."
                )
            _DEFAULT = Session(discover(library, root=root))
        return _DEFAULT


def initialize(
    library: str | os.PathLike[str] | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
) -> Session:
    """Discover, load and initialize the process default session."""
    with LOCK:
        if _OWNER is not None and (_DEFAULT is None or _OWNER is not _DEFAULT):
            raise RuntimeStateError("Another session already owns the initialized runtime.")
        if _DEFAULT is not None and _DEFAULT.state == "initialized":
            if library is not None or root is not None:
                raise RuntimeStateError(
                    "The runtime is initialized with the process-fixed library; a different "
                    "library needs a new process."
                )
            return _DEFAULT
        return default_session(library, root=root).initialize()


def current_session() -> Session:
    """Return the process owner (initialized, or broken awaiting finalization) or raise."""
    _check_not_inherited()
    with LOCK:
        if _OWNER is None:
            raise RuntimeStateError("No session is initialized; call famepy.initialize().")
        return _OWNER


def finalize() -> None:
    with LOCK:
        if _OWNER is not None:
            _OWNER.finalize()


def reset() -> Session:
    with LOCK:
        return current_session().reset()


def version() -> float:
    return current_session().version()


def _reset_module_state_for_tests() -> None:
    """Forget the default and owner references (test isolation only)."""
    global _DEFAULT, _OWNER, _INHERITED, _LOADED_NATIVE
    with LOCK:
        _DEFAULT = None
        _OWNER = None
        _INHERITED = False
        _LOADED_NATIVE = False
