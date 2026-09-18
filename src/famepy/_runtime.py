# SPDX-License-Identifier: MIT
"""Lazy, process-owned library loader. No initialization or vendor calls yet."""

import ctypes as ct
import os
import sys
import threading
from typing import Any

from ._discovery import Candidate
from ._errors import LibraryLoadError

LOCK = threading.RLock()


def _after_fork() -> None:
    global LOCK
    LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class Runtime:
    """Keep library/dependency-directory handles alive; forbid use after fork.

    Internal only. An explicit path loads native code and must be trusted.
    This owner does not initialize/finalize CHLI or promise an unload operation.
    """

    def __init__(self, candidate: Candidate) -> None:
        self._candidate = candidate
        self._pid = os.getpid()
        self._library: Any = None
        self._directory: Any = None

    def load(self) -> Any:
        if self._pid != os.getpid():
            raise RuntimeError("An inherited CHLI runtime cannot be used after fork; use spawn.")
        with LOCK:
            if self._library is not None:
                return self._library
            directory = None
            try:
                if sys.platform == "win32":
                    directory = os.add_dll_directory(str(self._candidate.path.parent))
                library = ct.CDLL(str(self._candidate.path))
            except OSError as error:
                if directory is not None:
                    directory.close()
                raise LibraryLoadError(
                    errno=error.errno, winerror=getattr(error, "winerror", None)
                ) from None
            self._directory = directory
            self._library = library
            return library
