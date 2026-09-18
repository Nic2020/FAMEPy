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
    """A located library. ``root`` is the trusted installation root when known."""

    path: Path
    source: str
    root: Path | None = None

    def __repr__(self) -> str:
        return f"Candidate(source={self.source!r}, path=<redacted>)"

    @property
    def trusted_directories(self) -> tuple[Path, ...]:
        """Directories whose native dependencies may be loaded for this library."""
        directories = [self.path.parent]
        if self.root is not None and self.root != self.path.parent:
            directories.append(self.root)
        return tuple(directories)


def discover(
    library: str | os.PathLike[str] | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    machine: str | None = None,
    pointer_bits: int | None = None,
) -> Candidate:
    """Resolve an absolute explicit path, FAMEPY_LIBRARY, or the FAME root.

    ``root`` optionally names the trusted installation root for an explicit
    library so that root-level native dependencies can be located without PATH
    changes. Discovery through ``FAME`` records that root automatically.
    """
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
    trusted_root: Path | None = None if root is None else Path(os.fspath(root))
    if value is None:
        fame_root = env.get("FAME")
        if not fame_root:
            raise LibraryNotFoundError("Set FAME or supply an absolute CHLI library path.")
        suffix = ("64", "chli.dll") if system == "win32" else ("hli", "64", "libchli.so")
        path = Path(fame_root).joinpath(*suffix)
        source = "FAME"
        if trusted_root is None:
            trusted_root = Path(fame_root)
    else:
        if not value:
            raise LibraryNotFoundError("The explicit CHLI library path is empty.")
        path = Path(value)
    if not path.is_absolute():
        raise LibraryNotFoundError("CHLI paths must be absolute; relative paths are not searched.")
    if trusted_root is not None and not trusted_root.is_absolute():
        raise LibraryNotFoundError("The trusted installation root must be an absolute path.")
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        raise LibraryNotFoundError("CHLI library file was not found at the configured location.")
    if trusted_root is not None:
        try:
            if not trusted_root.is_dir():
                raise LibraryNotFoundError("The trusted installation root is not a directory.")
        except OSError:
            raise LibraryNotFoundError("The trusted installation root is not accessible.") from None
    return Candidate(path, source, trusted_root)
