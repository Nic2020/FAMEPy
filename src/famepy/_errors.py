# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Status conventions adapted from FAME.jl; see licenses/FAME.jl.txt.
# Copyright (c) 2020-2021, Bank of Canada. All rights reserved.
"""Errors never include database names, connection strings or native messages."""

import operator


class FameError(RuntimeError):
    """A nonzero CHLI status. Native message text is deliberately not retrieved."""

    def __init__(self, status: int) -> None:
        self.status = operator.index(status)
        message = {13: "Object does not exist.", 67: "Invalid option."}.get(
            self.status, "CHLI operation failed."
        )
        super().__init__(f"CHLI status {self.status}: {message}")


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


def check_status(status: int) -> None:
    """Raise FameError for nonzero signed 32-bit status; preserve unknown codes."""
    if isinstance(status, bool):
        raise TypeError("A status must be an integer, not a Boolean.")
    value = operator.index(status)
    if not -(2**31) <= value < 2**31:
        raise OverflowError("Status does not fit a signed 32-bit integer.")
    if value:
        raise FameError(value)
