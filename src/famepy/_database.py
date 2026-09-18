# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Database semantics adapted from FAME.jl (Databases.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2021, Bank of Canada. All rights reserved.
"""Owning database handles: open, post, close, work database and access modes.

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
from ._constants import AccessMode, access_mode
from ._errors import StaleHandleError
from ._native import NativeInterface
from ._runtime import Session, current_session
from ._text import to_native


class Database:
    """An open FAME database owned by a session. Use as a context manager."""

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
        return f"Database({kind}, mode={self._mode.name.lower()}, {state})"

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

    def post(self) -> None:
        """Make updates durable. Required before closing to keep changes."""
        with self.operation("post") as native:
            native.post_database(self._key)

    def close(self) -> None:
        """Close without posting. Closing twice is a no-op.

        If the native close fails, the handle stays open and tracked: the
        error propagates, ``close()`` can be retried, and ``finalize()`` still
        attempts to close it (recording the status).
        """
        with _runtime.LOCK:
            if not self._open:
                return
            if self._generation != self._session.generation or self._session.is_terminal:
                # The runtime that owned this handle is gone; nothing to close.
                self._open = False
                self._session._unregister(self)
                return
            with self._session.operation("close") as native:
                native.close_database(self._key)
            self._open = False
            self._session._unregister(self)

    def __enter__(self) -> Database:
        self._check_open("enter")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def open_database(
    name: str | bytes | os.PathLike[str],
    mode: Any = AccessMode.READONLY,
    *,
    session: Session | None = None,
) -> Database:
    """Open a local database path or remote connection string.

    ``mode`` accepts the seven reference modes as integers 1-7, names such as
    ``"readonly"`` or ``"direct_write"``, or AccessMode members. The connection
    text is never stored on the handle or included in errors.
    """
    if isinstance(name, os.PathLike):
        name = os.fspath(name)
    if isinstance(name, str) and not name.strip():
        raise ValueError("A database name or connection string is required.")
    text = to_native(name, what="database name")
    selected = access_mode(mode)
    owner = current_session() if session is None else session
    with owner.operation("open database") as native:
        key = native.open_database(text, int(selected))
        return Database(owner, key, selected, is_work=False)


def work_database(*, session: Session | None = None) -> Database:
    """Return the session's work database, opening it on first use."""
    owner = current_session() if session is None else session
    with owner.operation("open work database") as native:
        existing = owner._work
        if existing is not None and existing.is_open:
            return existing
        key = native.open_work()
        database = Database(owner, key, AccessMode.UPDATE, is_work=True)
        owner._work = database
        return database
