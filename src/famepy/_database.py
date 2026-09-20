# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Database semantics adapted from FAME.jl (Databases.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2021, Bank of Canada. All rights reserved.
"""Database handles: ``FameDatabase``, ``opendb``, ``workdb``, ``postdb``, ``closedb``.

Read-only is the default mode. Closing never posts. Posting is explicit. The
package does not promise rollback or mode-specific persistence semantics; the
native validation campaign records what each mode actually does.

Every operation on a handle validates the handle inside the process lock it
holds for the native call, so a concurrent close or finalization cannot slip
between the check and the call. A failed native close keeps the handle open
and tracked so that it can be retried or cleaned up at finalization.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any

from . import _runtime
from ._constants import LOCAL_ACCESS_MODES, AccessMode, access_mode
from ._errors import StaleHandleError, UnsupportedOperationError
from ._native import NativeInterface
from ._runtime import Session, current_session
from ._text import to_native


class FameDatabase:
    """An open FAME database owned by a session (the reference's ``FameDatabase``).

    Instances come from ``opendb`` and ``workdb``; ``postdb`` and ``closedb``
    act on them. A handle is a context manager: leaving the ``with`` block
    closes it (without posting), which replaces the reference's do-block form
    of ``opendb``. The database name or connection string is never stored on
    the handle and never appears in ``repr`` or errors.
    """

    def __init__(self, session: Session, key: int, mode: AccessMode, *, is_work: bool) -> None:
        self._session = session
        self._key = key
        self._mode = mode
        self._is_work = is_work
        self._generation = session.generation
        self._open = True
        session._register(self)

    # -- identity ---------------------------------------------------------

    @property
    def session(self) -> Session:
        return self._session

    @property
    def key(self) -> int:
        return self._key

    @property
    def mode(self) -> AccessMode:
        return self._mode

    @property
    def is_work(self) -> bool:
        return self._is_work

    @property
    def is_open(self) -> bool:
        return (
            self._open
            and self._generation == self._session.generation
            and not self._session.is_terminal
        )

    @property
    def is_writable(self) -> bool:
        return self._mode != AccessMode.READONLY

    def __repr__(self) -> str:
        kind = "work database" if self._is_work else "database"
        state = "open" if self.is_open else "closed"
        return f"FameDatabase({kind}, mode={self._mode.name.lower()}, {state})"

    # -- lifecycle --------------------------------------------------------

    def _invalidate(self) -> None:
        self._open = False

    def _check_open(self, action: str) -> None:
        if self._generation != self._session.generation or self._session.is_terminal:
            raise StaleHandleError(
                f"Cannot {action}: the database handle belongs to a finalized runtime."
            )
        if not self._open:
            raise StaleHandleError(f"Cannot {action}: the database is closed.")

    @contextmanager
    def operation(self, action: str) -> Iterator[NativeInterface]:
        """Validate the handle and run one native operation under the same lock."""
        with _runtime.LOCK:
            self._check_open(action)
            with self._session.operation(action) as native:
                yield native

    def __enter__(self) -> FameDatabase:
        self._check_open("enter")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        closedb(self)


def opendb(
    dbname: str | bytes | os.PathLike[str],
    mode: Any = AccessMode.READONLY,
    *,
    session: Session | None = None,
) -> FameDatabase:
    """Open a local database path or remote connection string.

    ``mode`` accepts the seven reference modes as integers 1-7, names such as
    ``"readonly"`` or ``"update"``, or AccessMode members, but only the five
    local modes can be opened: ``write`` and ``direct_write`` are modes of a
    database opened on a named server connection, which this release does
    not bind, so they raise ``UnsupportedOperationError`` before any native
    call instead of being remapped. The connection text is never stored on
    the handle or included in errors. Use the handle as a context manager
    for the reference's do-block form.
    """
    if isinstance(dbname, os.PathLike):
        dbname = os.fspath(dbname)
    if isinstance(dbname, str) and not dbname.strip():
        raise ValueError("A database name or connection string is required.")
    text = to_native(dbname, what="database name")
    selected = access_mode(mode)
    if selected not in LOCAL_ACCESS_MODES:
        raise UnsupportedOperationError(
            f"Access mode {selected.name.lower()} needs a database opened on a named "
            "server connection, which this release does not bind; the local open "
            "accepts readonly, create, overwrite, update and shared."
        )
    owner = current_session() if session is None else session
    with owner.operation("open database") as native:
        key = native.open_database(text, int(selected))
        return FameDatabase(owner, key, selected, is_work=False)


def workdb(*, session: Session | None = None) -> FameDatabase:
    """Return the session's work database, opening it on first use.

    The work database can be open only once, so the same handle is returned
    while it is open and a new one is opened after it was closed.
    """
    owner = current_session() if session is None else session
    with owner.operation("open work database") as native:
        existing = owner._work
        if existing is not None and existing.is_open:
            return existing
        key = native.open_work()
        database = FameDatabase(owner, key, AccessMode.UPDATE, is_work=True)
        owner._work = database
        return database


def postdb(db: FameDatabase) -> None:
    """Post the database: make updates durable. Required before closing to keep them."""
    if not isinstance(db, FameDatabase):
        raise TypeError("postdb expects a FameDatabase.")
    with db.operation("post") as native:
        native.post_database(db.key)


def closedb(db: FameDatabase) -> FameDatabase:
    """Close the database without posting and return it (the reference's ``closedb!``).

    Closing twice is a no-op. If the native close fails, the handle stays
    open and tracked: the error propagates, ``closedb`` can be retried, and
    ``close_chli()`` still attempts to close it (recording the status).
    """
    if not isinstance(db, FameDatabase):
        raise TypeError("closedb expects a FameDatabase.")
    with _runtime.LOCK:
        if not db._open:
            return db
        session = db._session
        if db._generation != session.generation or session.is_terminal:
            # The runtime that owned this handle is gone; nothing to close.
            db._open = False
            session._unregister(db)
            return db
        with session.operation("close") as native:
            native.close_database(db._key)
        db._open = False
        session._unregister(db)
    return db
