# SPDX-License-Identifier: MIT
"""Byte-preserving text boundary for names, paths, commands and string values.

The native layer exchanges bytes. Names, database strings and commands keep
the initial validation boundary of ``to_native``/``from_native``: ``str``
input is accepted only when it is ASCII, and bytes returned from the library
are decoded only when they are ASCII. String *values* (string scalars and
string series observations) have their own codec, ``encode_value`` and
``decode_value``, selected by the value text policy: ``"ascii"`` (the
default) and ``"bytes"`` keep that boundary, and ``"utf-8"`` encodes and
decodes strictly as UTF-8. No policy replaces, escapes or guesses: an
embedded NUL, a lone surrogate, a byte sequence that is not the selected
encoding, are failures. Errors never echo the offending content.
"""

from __future__ import annotations

from typing import Any

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


VALUE_TEXT_POLICIES = ("ascii", "bytes", "utf-8")
_ENCODINGS = {"ascii": "ascii", "bytes": "ascii", "utf-8": "utf-8"}


def check_value_policy(policy: Any) -> str:
    """The value text policy as one of ``VALUE_TEXT_POLICIES`` or ``ValueError``."""
    if not isinstance(policy, str) or policy not in VALUE_TEXT_POLICIES:
        raise ValueError("text must be 'ascii', 'bytes' or 'utf-8'.")
    return policy


def encode_value(
    value: str | bytes | bytearray | memoryview,
    policy: str = "ascii",
    *,
    what: str = "string value",
) -> bytes:
    """Encode one string value for the library under the value text policy.

    ``bytes`` are preserved as given under every policy. ``str`` is encoded
    as ASCII under ``"ascii"`` and ``"bytes"`` and strictly as UTF-8 under
    ``"utf-8"`` (a lone surrogate cannot be encoded and is refused). The
    result never contains a NUL byte. Capacities that apply to string
    values are byte capacities of this result, never character counts.
    """
    encoding = _ENCODINGS[check_value_policy(policy)]
    if isinstance(value, str):
        try:
            data = value.encode(encoding)
        except UnicodeEncodeError:
            raise TextEncodingError(
                f"The {what} cannot be encoded as {encoding.upper()} under the "
                f"{policy!r} text policy; pass explicit bytes or select another policy."
            ) from None
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
    else:
        raise TypeError(f"The {what} must be str or bytes, not {type(value).__name__}.")
    if b"\0" in data:
        raise TextEncodingError(f"The {what} contains an embedded NUL byte.")
    return data


def decode_value(data: bytes, policy: str = "ascii", *, what: str = "string value") -> str | bytes:
    """Decode one string value read from the library under the value text policy.

    ``"bytes"`` returns the bytes untouched; ``"ascii"`` and ``"utf-8"``
    decode strictly and raise ``TextEncodingError`` when the bytes are not
    that encoding. Missing sentinels are classified by the caller before any
    value reaches this function, so a sentinel is never decoded.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"Native {what} must be bytes.")
    if check_value_policy(policy) == "bytes":
        return bytes(data)
    encoding = _ENCODINGS[policy]
    try:
        return bytes(data).decode(encoding)
    except UnicodeDecodeError:
        raise TextEncodingError(
            f"Native {what} is not valid {encoding.upper()}; read the raw bytes instead."
        ) from None


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
