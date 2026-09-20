# SPDX-License-Identifier: MIT
"""Manifest helpers shared by the validation groups (cross-process evidence)."""

from __future__ import annotations

from typing import Any

import numpy as np

import famepy
import famepy.bridge
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


def _read(
    database: famepy.FameDatabase,
    name: Any,
    *,
    first_index: int | None = None,
    last_index: int | None = None,
) -> Any:
    """Read an object through the canonical path (quick_info, then do_read).

    An explicit endpoint selects a subrange, as a caller of the reference
    does by setting the object's range before ``do_read``. Returned as Any
    so groups can inspect scalar and series data alike.
    """
    obj = famepy.quick_info(database, name)
    if first_index is not None:
        obj.first_index = first_index
    if last_index is not None:
        obj.last_index = last_index
    return famepy.do_read(obj, database)


def _target(target: Any, mode: Any) -> tuple[famepy.FameDatabase, bool]:
    if isinstance(target, famepy.FameDatabase):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return target, False
    return famepy.opendb(target, "readonly" if mode is None else mode), True


def _value(target: Any, name: Any, **policies: Any) -> Any:
    """Read one object as a Python value: quick_info, do_read, unfame.

    ``target`` is a handle or a path (opened read-only and closed); the
    keywords are the ``unfame`` policies. Groups use this so that every
    single-object read goes through the canonical calls.
    """
    database, owned = _target(target, None)
    try:
        return famepy.unfame(_read(database, name), database=database, **policies)
    finally:
        if owned:
            famepy.closedb(database)


def _write(
    target: Any,
    name: Any,
    value: Any,
    *,
    mode: Any = None,
    replace: bool = False,
    empty: str = "preserve",
    text: str = "ascii",
    basis: Any = None,
    observed: Any = None,
) -> None:
    """Write one value: refame then do_write; a path is opened in ``mode``, posted, closed.

    Everything that does not need the database (policies, attributes, the
    conversion itself) runs before a path is opened, so an invalid value
    never creates or truncates a file.
    """
    if not isinstance(target, famepy.FameDatabase) and mode is None:
        raise ValueError("Writing to a path needs an explicit mode.")
    if mode is not None:
        famepy.AccessMode(famepy._constants.access_mode(mode))
    famepy._data.attribute_codes(basis, observed)
    session = famepy.bridge.owner_session(
        target if isinstance(target, famepy.FameDatabase) else None
    )
    obj = famepy.refame(name, value, session=session, empty=empty, text=text)
    database, owned = _target(target, mode)
    try:
        famepy.do_write(obj, database, replace=replace, basis=basis, observed=observed)
        if owned:
            famepy.postdb(database)
    finally:
        if owned:
            famepy.closedb(database)
