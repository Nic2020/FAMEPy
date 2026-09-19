# SPDX-License-Identifier: MIT
"""Manifest helpers shared by the validation groups (cross-process evidence)."""

from __future__ import annotations

from typing import Any

import numpy as np

import famepy
from famepy._data import namelist_members

_DTYPES = {"precision": np.float64, "numeric": np.float32, "boolean": np.int32, "date": np.int64}


def manifest_object(
    name: str,
    kind: str,
    values: Any,
    *,
    class_name: str,
    type_code: int,
    frequency: int | None = None,
    first_index: int | None = None,
) -> dict[str, Any]:
    """Describe one object completely: class, type, frequency, range and bits."""
    entry: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "class": class_name,
        "type_code": type_code,
        "frequency": frequency,
        "values": _manifest_value(kind, values),
    }
    if class_name == "series":
        count = len(values)
        entry["first_index"] = first_index if count else None
        entry["last_index"] = first_index + count - 1 if count and first_index is not None else None
    return entry


def _manifest_value(kind: str, value: Any) -> Any:
    """Encode values losslessly: floats by bit pattern, strings as bytes in hex.

    String values are bytes, not text: the library's own string sentinels are
    not ASCII, so a text decoding would fail or alter them. A namelist is
    described by its ordered members (each in hex), because the library
    documents no fixed layout for the list text.
    """
    if kind in ("precision", "numeric"):
        return [np.array(v, dtype=_DTYPES[kind]).tobytes().hex() for v in value]
    if kind in ("boolean", "date"):
        return [int(v) for v in value]
    if kind == "namelist":
        return [[m.hex() for m in namelist_members(bytes(v))] for v in value]
    return [bytes(v).hex() for v in value]


def _decode_manifest(kind: str, value: list[Any]) -> Any:
    if kind in ("precision", "numeric"):
        dtype = _DTYPES[kind]
        return np.array(
            [np.frombuffer(bytes.fromhex(v), dtype=dtype)[0] for v in value], dtype=dtype
        )
    if kind == "boolean":
        return np.array(value, dtype=np.int32)
    if kind == "date":
        return np.array(value, dtype=np.int64)
    if kind == "namelist":
        return [[bytes.fromhex(m) for m in members] for members in value]
    return [bytes.fromhex(v) for v in value]


def verify_case_ids(case_id: str, names: list[str]) -> tuple[str, ...]:
    """The verification cases a cross-process check must report."""
    ids: list[str] = []
    for name in names:
        ids.extend(
            (f"{case_id}:reopen:{name}", f"{case_id}:meta:{name}", f"{case_id}:values:{name}")
        )
    return tuple(ids)


def _read(database: famepy.Database, name: Any, **kwargs: Any) -> Any:
    """Read an object as Any so groups can inspect scalar and series fields."""
    return famepy.read_object(database, name, **kwargs)
