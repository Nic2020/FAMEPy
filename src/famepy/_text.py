# SPDX-License-Identifier: MIT
"""Byte-preserving text boundary for names, paths and commands.

No vendor text encoding has been established. The native layer therefore
exchanges bytes. Convenience conversion is explicit and strict: ``str`` input is
accepted only when it is ASCII, and bytes returned from the library are decoded
only when they are ASCII. Errors never echo the offending content.
"""

from __future__ import annotations

from ._constants import NAME_CAPACITY


class TextEncodingError(ValueError):
    """Text cannot cross the native boundary under the ASCII validation policy."""


def to_native(value: str | bytes | bytearray | memoryview, *, what: str = "text") -> bytes:
    """Return NUL-free bytes. ``str`` must be ASCII; bytes are preserved as given."""
    if isinstance(value, str):
        try:
            data = value.encode("ascii")
        except UnicodeEncodeError:
            raise TextEncodingError(
                f"The {what} contains non-ASCII characters; pass explicit bytes or use ASCII."
            ) from None
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    else:
        raise TypeError(f"The {what} must be str or bytes, not {type(value).__name__}.")
    if b"\0" in data:
        raise TextEncodingError(f"The {what} contains an embedded NUL byte.")
    return data


def from_native(data: bytes, *, what: str = "text", strict: bool = True) -> str:
    """Decode ASCII bytes. Non-ASCII raises unless ``strict`` is False (escaped)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"Native {what} must be bytes.")
    try:
        return bytes(data).decode("ascii")
    except UnicodeDecodeError:
        if strict:
            raise TextEncodingError(
                f"Native {what} contains non-ASCII bytes; read the raw bytes instead."
            ) from None
        return bytes(data).decode("ascii", errors="backslashreplace")


def is_ascii(value: str | bytes) -> bool:
    if isinstance(value, str):
        return value.isascii()
    return all(byte < 128 for byte in value)


def object_name(value: str | bytes) -> bytes:
    """Validate an object's byte capacity before any mutating operation."""
    data = to_native(value, what="object name")
    if not 1 <= len(data) <= NAME_CAPACITY:
        raise ValueError(f"An object name must contain 1 to {NAME_CAPACITY} bytes.")
    return data
