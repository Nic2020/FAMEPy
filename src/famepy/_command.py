# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Command semantics adapted from FAME.jl (Command.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2021, Bank of Canada. All rights reserved.
"""FAME command execution with captured output and recursive INPUT expansion.

The reference notes that the command interface does not process INPUT and
limits a command to 2**20 bytes, so INPUT lines are replaced by file contents
before execution. Output is redirected to a temporary file for the duration of
one locked operation and always restored, even on failure. The temporary
file is a fresh name inside a private directory created for the call; the
package never pre-creates the file, so the library opens it itself.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import IO

from ._errors import HSUCC, CommandError, IncludeError
from ._runtime import Session, current_session
from ._text import TextEncodingError, to_native

MAX_COMMAND_BYTES = 2**20
MAX_INCLUDE_DEPTH = 32
MAX_INCLUDE_BYTES = 2**24

# An INPUT statement starts at the beginning of the text or right after a
# statement delimiter (``;`` or newline) and ends before the next delimiter.
# Both boundaries are zero-width so consecutive statements share delimiters.
_INPUT = re.compile(
    rb"(?<![^;\n])[ \t]*input[ \t]+(?P<file>file[ \t]*\([ \t]*)?"
    rb"(?P<value>.*?)(?(file)\))[ \t]*(?=[;\n]|$)",
    re.IGNORECASE,
)


def _resolve_include(value: bytes, base: Path) -> Path:
    """Apply the reference suffix rules: trailing ``!`` keeps the name, else ``.inp``."""
    name = value.strip()
    quoted = name.startswith(b'"') and name.endswith(b'"') and len(name) >= 2
    if quoted:
        name = name[1:-1]
    if name.endswith(b"!"):
        name = name[:-1]
    elif not os.path.splitext(name)[1]:
        name += b".inp"
    try:
        text = name.decode("ascii")
    except UnicodeDecodeError:
        raise IncludeError("INPUT file names must be ASCII in this release.") from None
    if not text.strip():
        raise IncludeError("INPUT requires a file name.")
    path = Path(text)
    return path if path.is_absolute() else base / path


def expand_input(
    command: bytes,
    *,
    base_dir: str | os.PathLike[str] | None = None,
    max_depth: int = MAX_INCLUDE_DEPTH,
    max_bytes: int = MAX_INCLUDE_BYTES,
) -> bytes:
    """Replace INPUT statements by file contents recursively.

    Literal ``FILE("name")`` arguments are supported; a computed FILE() argument
    is refused. Relative names resolve against ``base_dir`` (the working
    directory by default, matching the reference). Cycles, excessive depth and
    excessive expanded size are refused before anything is executed. File
    system errors surface as IncludeError without file names.
    """
    base = Path.cwd() if base_dir is None else Path(os.fspath(base_dir))
    return _expand(command, base, (), max_depth, max_bytes, [0])


def _expand(
    command: bytes,
    base: Path,
    stack: tuple[Path, ...],
    depth: int,
    max_bytes: int,
    total: list[int],
) -> bytes:
    def replace(match: re.Match[bytes]) -> bytes:
        has_file = match.group("file") is not None
        value = match.group("value").strip()
        quoted = value.startswith(b'"') and value.endswith(b'"') and len(value) >= 2
        if has_file and not quoted:
            raise IncludeError(
                "INPUT FILE() with a computed argument is not expanded; use EXECUTE instead."
            )
        path = _resolve_include(value, base)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise IncludeError("An INPUT file does not exist.") from None
        if resolved in stack:
            raise IncludeError("INPUT files include each other in a cycle.")
        if len(stack) + 1 > depth:
            raise IncludeError("INPUT nesting exceeds the maximum depth.")
        try:
            if not resolved.is_file():
                raise IncludeError("An INPUT name is not a regular file.")
            size = resolved.stat().st_size
        except OSError:
            raise IncludeError("An INPUT file cannot be inspected.") from None
        total[0] += size
        if size > max_bytes or total[0] > max_bytes:
            raise IncludeError("INPUT expansion exceeds the maximum size.")
        try:
            content = resolved.read_bytes()
        except OSError:
            raise IncludeError("An INPUT file cannot be read.") from None
        if b"\0" in content:
            raise IncludeError("An INPUT file contains NUL bytes.")
        body = b"\n".join(content.splitlines())
        nested = _expand(body, base, (*stack, resolved), depth, max_bytes, total)
        return b"\n" + nested + b"\n"

    # Split on unquoted delimiters. A semicolon inside a quoted FAME string
    # is text, not an INPUT boundary (and quoted filenames can contain it).
    result: list[bytes] = []
    start = 0
    quote: int | None = None
    for offset, byte in enumerate(command):
        if byte in (34, 39):
            if quote == byte:
                quote = None
            elif quote is None:
                quote = byte
        elif byte in (59, 10) and quote is None:
            statement = command[start:offset]
            match = _INPUT.fullmatch(statement)
            result.append(replace(match) if match else statement)
            result.append(command[offset : offset + 1])
            start = offset + 1
    statement = command[start:]
    match = _INPUT.fullmatch(statement)
    result.append(replace(match) if match else statement)
    return b"".join(result)


def _quote_path(path: Path) -> bytes:
    text = os.fspath(path)
    if not text.isascii():
        raise TextEncodingError("The temporary output directory must have an ASCII path.")
    if '"' in text:
        raise TextEncodingError("The temporary output directory cannot contain quotes.")
    return text.encode("ascii")


def _captured_output(path: Path) -> bytes:
    """The redirected output; a file the library never created reads as empty."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _remove_quietly(path: Path) -> None:
    try:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    except OSError:
        pass


def fame(
    command: str | bytes,
    *,
    session: Session | None = None,
    quiet: bool = False,
    output: IO[bytes] | None = None,
    expand_includes: bool = True,
    base_dir: str | os.PathLike[str] | None = None,
    temp_dir: str | os.PathLike[str] | None = None,
) -> bytes:
    """Execute one FAME command and return its captured output bytes.

    The reference's ``fame(io, command)`` form is the ``output`` keyword;
    there is no string-macro form. ``INPUT`` statements are expanded first.

    Output goes to the temporary file for the whole operation and is returned
    (or written to ``output``). ``quiet`` discards it. On failure a CommandError
    carries the status code, the failing ``stage`` (``redirect``, ``command`` or
    ``restore``) and any partial output on its ``output`` attribute; output
    redirection is restored and the temporary directory removed regardless.
    When the payload fails and the restoration fails too, the payload error is
    what propagates, with the restoration status on ``restore_status``. When
    an extended-error retrieval is configured on the session, its text is
    captured immediately at the failing status, before the redirection is
    restored, and attached as ``extended_text``.
    """
    text = to_native(command, what="command")
    owner = current_session() if session is None else session
    if expand_includes:
        text = expand_input(text, base_dir=base_dir)
    if len(text) > MAX_COMMAND_BYTES:
        raise ValueError("The expanded command exceeds the maximum command size.")
    with owner.operation("command") as native:
        # A private directory holds one fresh file name; the file itself does
        # not exist until the library creates it through the redirection.
        directory = Path(tempfile.mkdtemp(prefix="famepy-", dir=temp_dir))
        path = directory / "output.txt"
        try:
            redirect = b'output file("' + _quote_path(path) + b'!")'
            status = native.execute(redirect)
            if status != HSUCC:
                raise CommandError(
                    status,
                    stage="redirect",
                    extended_text=owner._capture_extended_error(native),
                )
            extended: bytes | None = None
            try:
                status = native.execute(text)
                if status != HSUCC:
                    extended = owner._capture_extended_error(native)
            finally:
                restore = native.execute(b"output terminal")
            captured = _captured_output(path)
            if status != HSUCC:
                raise CommandError(
                    status,
                    output=captured,
                    extended_text=extended,
                    stage="command",
                    restore_status=None if restore == HSUCC else restore,
                )
            if restore != HSUCC:
                raise CommandError(
                    restore,
                    output=captured,
                    extended_text=owner._capture_extended_error(native),
                    stage="restore",
                )
        finally:
            _remove_quietly(path)
            _remove_quietly(directory)
    if output is not None:
        output.write(captured)
    return b"" if quiet else captured
