# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Status conventions adapted from FAME.jl; see licenses/FAME.jl.txt.
# Copyright (c) 2020-2021, Bank of Canada. All rights reserved.
"""Errors never include database names, connection strings or native messages."""

from __future__ import annotations

import operator

# Status codes confirmed by header inspection on both inspected installations.
HSUCC = 0
HFIN = 3
HNOOBJ = 13
HTRUNC = 18
HBOPT = 67
HFMENV = 97
HLICFL = 98
HFAMER = 513

_MESSAGES = {
    HFIN: "CHLI was already finalized in this process; it initializes once per process.",
    HNOOBJ: "Object does not exist.",
    HTRUNC: "Data or text was truncated.",
    HBOPT: "Bad option.",
    HFMENV: "The FAME environment variable is not set to the installation.",
    HLICFL: "The FAME licensing file was not found.",
    HFAMER: "FAME reported a command or server error; extended text is opt-in.",
}


class FameError(RuntimeError):
    """A nonzero CHLI status. Native message text is deliberately not retrieved."""

    def __init__(self, status: int, *, operation: str | None = None) -> None:
        self.status = operator.index(status)
        self.operation = operation
        # Opt-in extended text captured at the failure under the session lock.
        self.extended_text: bytes | None = None
        self.extended_captured = False
        message = _MESSAGES.get(self.status, "CHLI operation failed.")
        prefix = f"CHLI status {self.status}"
        if operation is not None:
            prefix += f" during {operation}"
        super().__init__(f"{prefix}: {message}")


class LibraryNotFoundError(RuntimeError):
    """CHLI is not configured, not present, or cannot be loaded."""


def error_number(value: object) -> int | None:
    """Retain only bounded integers, never Boolean values or exception text."""
    return value if type(value) is int and -(2**31) <= value < 2**32 else None


class LibraryLoadError(LibraryNotFoundError):
    """Native loading failed; OS numbers are retained without native messages."""

    def __init__(self, *, errno: int | None = None, winerror: int | None = None) -> None:
        self.errno = error_number(errno)
        self.winerror = error_number(winerror)
        super().__init__("CHLI could not load; check architecture and native dependencies.")


class SymbolNotFoundError(RuntimeError):
    """A declared native function is unavailable in the loaded library."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"Required CHLI symbol is unavailable: {symbol}")


class UnsupportedPlatformError(RuntimeError):
    """Native discovery is restricted to Windows/Linux x86-64."""


class RuntimeStateError(RuntimeError):
    """The runtime lifecycle does not permit the requested operation."""


class InheritedRuntimeError(RuntimeStateError):
    """CHLI state was inherited across fork; use a spawned process instead."""


class StaleHandleError(RuntimeStateError):
    """A database handle belongs to a finalized runtime or was closed."""


class LicensingConfigurationError(RuntimeStateError):
    """The FAME environment variable required for licensing is not configured."""


class UnsupportedOperationError(RuntimeError):
    """The operation needs vendor facts that have not been established."""


class DataValidationError(ValueError):
    """A buffer, range or value cannot be passed to the native library safely."""


class NameTruncatedError(RuntimeError):
    """A listed object name exceeded the requested capacity."""

    def __init__(self, returned_length: int, capacity: int) -> None:
        self.returned_length = returned_length
        self.capacity = capacity
        super().__init__(
            f"An object name of {returned_length} bytes exceeded the {capacity}-byte capacity."
        )


class IncludeError(ValueError):
    """Recursive INPUT expansion failed; the message never contains file text."""


COMMAND_STAGES = ("redirect", "command", "restore")


class CommandError(FameError):
    """A FAME command failed. Any captured output is kept on ``output`` only.

    ``stage`` names which of the three native calls returned the status:
    ``redirect`` (output redirection to the temporary file), ``command`` (the
    payload itself) or ``restore`` (``output terminal``). ``restore_status``
    keeps the status of the restoration when the payload had already failed,
    so the original failure is what propagates and nothing is lost.
    """

    def __init__(
        self,
        status: int,
        *,
        output: bytes | None = None,
        extended_text: bytes | None = None,
        stage: str = "command",
        restore_status: int | None = None,
    ) -> None:
        if stage not in COMMAND_STAGES:
            raise ValueError("Unknown command stage.")
        super().__init__(status, operation=f"command execution ({stage})")
        self.output = output
        self.extended_text = extended_text
        self.extended_captured = True
        self.stage = stage
        self.restore_status = restore_status


def check_status(status: int, *, operation: str | None = None) -> None:
    """Raise FameError for nonzero signed 32-bit status; preserve unknown codes."""
    if isinstance(status, bool):
        raise TypeError("A status must be an integer, not a Boolean.")
    value = operator.index(status)
    if not -(2**31) <= value < 2**31:
        raise OverflowError("Status does not fit a signed 32-bit integer.")
    if value:
        raise FameError(value, operation=operation)
