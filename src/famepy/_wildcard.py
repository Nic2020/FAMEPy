# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Listing semantics adapted from FAME.jl (Objects.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2022, Bank of Canada. All rights reserved.
"""Wildcard listing with ITEM filters and deterministic cursor cleanup.

ITEM options are process-global inside the library and there is no declared
call to read them back, so the package cannot restore an arbitrary prior
state. ``list_objects`` therefore *normalizes* the four options it uses
(CLASS, TYPE, FREQUENCY, ALIAS) to ON when it finishes, whatever they were
before. Commands that changed those options must set them again afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable

from ._constants import NAME_CAPACITY, ObjectClass, ObjectType
from ._database import Database
from ._errors import (
    HNOOBJ,
    HSUCC,
    HTRUNC,
    DataValidationError,
    FameError,
    NameTruncatedError,
    check_status,
)
from ._objects import ObjectInfo, query_info
from ._text import to_native

_FILTERS = ("CLASS", "TYPE", "FREQUENCY")
NORMALIZED_OPTIONS: tuple[tuple[bytes, bytes], ...] = (
    (b"ITEM CLASS", b"ON"),
    (b"ITEM TYPE", b"ON"),
    (b"ITEM FREQUENCY", b"ON"),
    (b"ITEM ALIAS", b"ON"),
)


def is_wildcard(pattern: str | bytes) -> bool:
    """``?`` matches any run of characters and ``^`` exactly one."""
    if isinstance(pattern, str):
        return "?" in pattern or "^" in pattern
    return b"?" in pattern or b"^" in pattern


def _values(option: str, values: Iterable[str] | str | None) -> list[bytes]:
    if values is None:
        return []
    items = [values] if isinstance(values, str) else list(values)
    result: list[bytes] = []
    for item in items:
        for part in str(item).split(","):
            token = part.strip().upper()
            if not token:
                continue
            if not token.isascii() or not token.replace("_", "").isalnum():
                raise ValueError(f"Invalid {option.lower()} filter value.")
            if option == "CLASS" and token not in {member.name for member in ObjectClass}:
                raise ValueError("Unknown class filter value.")
            if option == "TYPE" and token not in {member.name for member in ObjectType}:
                raise ValueError("Unknown type filter value.")
            result.append(token.encode("ascii"))
    return result


def _normalize_options(native: object) -> list[FameError]:
    """Set every listing option to ON; attempt all of them and return failures."""
    failures: list[FameError] = []
    for name, value in NORMALIZED_OPTIONS:
        try:
            native.set_option(name, value)  # type: ignore[attr-defined]
        except FameError as error:
            failures.append(error)
    return failures


def list_objects(
    database: Database,
    pattern: str | bytes = "?",
    *,
    alias: bool = True,
    classes: Iterable[str] | str | None = None,
    types: Iterable[str] | str | None = None,
    frequencies: Iterable[str] | str | None = None,
    capacity: int = NAME_CAPACITY,
) -> list[ObjectInfo]:
    """List objects matching ``pattern`` with optional class/type/frequency filters.

    The ITEM options are set for the listing and normalized to ON afterwards
    within the same locked operation (see the module note). Names longer than
    ``capacity`` bytes raise NameTruncatedError with the returned length,
    because the cursor cannot re-fetch that entry. Scalars are re-queried with
    quick_info because the reference notes that wildcard ranges are unreliable
    for them. Cleanup always frees the cursor and attempts every option reset,
    even after a failure; the first failure is what propagates.
    """
    text = to_native(pattern, what="wildcard pattern")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 2**20:
        raise DataValidationError("Name capacity must be between 1 and 2**20 bytes.")
    filters = {
        "CLASS": _values("CLASS", classes),
        "TYPE": _values("TYPE", types),
        "FREQUENCY": _values("FREQUENCY", frequencies),
    }
    results: list[ObjectInfo] = []
    with database.operation("list objects") as native:
        key = database.key
        failed = False
        try:
            native.set_option(b"ITEM ALIAS", b"ON" if alias else b"OFF")
            for option in _FILTERS:
                selected = filters[option]
                if not selected:
                    native.set_option(f"ITEM {option}".encode(), b"ON")
                    continue
                native.set_option(f"ITEM {option}".encode(), b"OFF")
                for value in selected:
                    native.set_option(b"ITEM " + option.encode() + b" " + value, b"ON")
            wildcard_key = native.init_wildcard(key, text)
            cursor_failed = False
            try:
                while True:
                    entry = native.next_wildcard(wildcard_key, capacity)
                    if entry.status == HNOOBJ:
                        break
                    if entry.status == HTRUNC:
                        raise NameTruncatedError(entry.returned_length, capacity)
                    if entry.status != HSUCC:
                        check_status(entry.status, operation="fame_get_next_wildcard")
                    if entry.returned_length < 0 or entry.returned_length > capacity:
                        raise NameTruncatedError(entry.returned_length, capacity)
                    info = ObjectInfo(
                        entry.name,
                        entry.class_code,
                        entry.type_code,
                        entry.frequency,
                        entry.first_index,
                        entry.last_index,
                    )
                    if info.class_code == ObjectClass.SCALAR:
                        info = query_info(native, key, entry.name)
                    results.append(info)
            except BaseException as error:
                cursor_failed = True
                if isinstance(error, FameError):
                    database.session._attach_extended_error(native, error)
                raise
            finally:
                try:
                    native.free_wildcard(wildcard_key)
                except Exception:
                    if not cursor_failed:
                        raise
        except BaseException as error:
            failed = True
            if isinstance(error, FameError):
                database.session._attach_extended_error(native, error)
            raise
        finally:
            cleanup_failures = _normalize_options(native)
            if cleanup_failures and not failed:
                raise cleanup_failures[0]
    return results
