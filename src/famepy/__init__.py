# SPDX-License-Identifier: MIT
"""FAME library discovery and binding foundations; import never loads CHLI."""

from ._errors import (
    FameError,
    LibraryLoadError,
    LibraryNotFoundError,
    SymbolNotFoundError,
    UnsupportedPlatformError,
    check_status,
)
from .diagnostics import diagnose

__version__ = "0.0.1.dev0"
__all__ = [
    "FameError",
    "LibraryLoadError",
    "LibraryNotFoundError",
    "SymbolNotFoundError",
    "UnsupportedPlatformError",
    "check_status",
    "diagnose",
]
