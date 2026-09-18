# SPDX-License-Identifier: MIT
"""Deterministic discovery without PATH searches or native library loading."""

import os
import platform
import struct
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ._errors import LibraryNotFoundError, UnsupportedPlatformError


@dataclass(frozen=True, repr=False)
class Candidate:
    path: Path
    source: str

    def __repr__(self) -> str:
        return f"Candidate(source={self.source!r}, path=<redacted>)"


def discover(
    library: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    machine: str | None = None,
    pointer_bits: int | None = None,
) -> Candidate:
    """Resolve an absolute explicit path, FAMEPY_LIBRARY, or the FAME root."""
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    bits = struct.calcsize("P") * 8 if pointer_bits is None else pointer_bits
    if (
        system not in {"win32", "linux"}
        or bits != 64
        or machine.lower()
        not in {
            "amd64",
            "x86_64",
        }
    ):
        raise UnsupportedPlatformError("CHLI discovery requires Windows/Linux x86-64 Python.")
    env = os.environ if environ is None else environ
    value = os.fspath(library) if library is not None else env.get("FAMEPY_LIBRARY")
    source = "argument" if library is not None else "FAMEPY_LIBRARY"
    if value is None:
        root = env.get("FAME")
        if not root:
            raise LibraryNotFoundError("Set FAME or supply an absolute CHLI library path.")
        suffix = ("64", "chli.dll") if system == "win32" else ("hli", "64", "libchli.so")
        path = Path(root).joinpath(*suffix)
        source = "FAME"
    else:
        if not value:
            raise LibraryNotFoundError("The explicit CHLI library path is empty.")
        path = Path(value)
    if not path.is_absolute():
        raise LibraryNotFoundError("CHLI paths must be absolute; relative paths are not searched.")
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        raise LibraryNotFoundError("CHLI library file was not found at the configured location.")
    return Candidate(path, source)
