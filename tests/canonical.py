# SPDX-License-Identifier: MIT
"""Test scaffolding over the canonical API: every helper is a few public calls.

The helpers exist so that tests can build named objects tersely and read or
write one value through ``quick_info``, ``do_read``, ``unfame``, ``refame``
and ``do_write``; nothing here bypasses the package's public path.
"""

from __future__ import annotations

from typing import Any

import famepy


def scalar_object(
    name: str | bytes, kind: str, value: Any, *, date_frequency: Any = None
) -> famepy.FameObject:
    """A scalar ``FameObject``; a date value is typed by its frequency."""
    return famepy.FameObject(
        name, "scalar", kind if date_frequency is None else date_frequency, "undefined", data=value
    )


def series_object(
    name: str | bytes,
    kind: str,
    frequency: Any,
    first: int | None,
    values: Any,
    *,
    date_frequency: Any = None,
) -> famepy.FameObject:
    """A series ``FameObject`` from its first index and values (the last index follows)."""
    return famepy.FameObject(
        name,
        "series",
        kind if date_frequency is None else date_frequency,
        frequency,
        first,
        data=values,
    )


def read(
    db: famepy.FameDatabase,
    name: str | bytes,
    *,
    first_index: int | None = None,
    last_index: int | None = None,
) -> famepy.FameObject:
    """``quick_info`` then ``do_read``; explicit endpoints select a subrange."""
    obj = famepy.quick_info(db, name)
    if first_index is not None:
        obj.first_index = first_index
    if last_index is not None:
        obj.last_index = last_index
    return famepy.do_read(obj, db)


def _target(target: Any, mode: Any) -> tuple[famepy.FameDatabase, bool]:
    if isinstance(target, famepy.FameDatabase):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return target, False
    if not isinstance(target, (str, bytes)) and not hasattr(target, "__fspath__"):
        raise TypeError("Expected a FameDatabase or a database path.")
    return famepy.opendb(target, "readonly" if mode is None else mode), True


def value(target: Any, name: str | bytes, **policies: Any) -> Any:
    """Read one object as a value: ``quick_info``, ``do_read``, ``unfame``.

    ``target`` is a handle or a path (opened read-only and closed); the
    keywords are the ``unfame`` policies.
    """
    famepy.bridge.check_policies(
        policies.get("missing"), policies.get("empty"), policies.get("text")
    )
    database, owned = _target(target, None)
    try:
        return famepy.unfame(read(database, name), database=database, **policies)
    finally:
        if owned:
            famepy.closedb(database)


def write(
    target: Any,
    name: str | bytes,
    data: Any,
    *,
    mode: Any = None,
    replace: bool = False,
    empty: str = "preserve",
    text: str = "ascii",
    basis: Any = None,
    observed: Any = None,
) -> None:
    """Write one value: ``refame`` then ``do_write``; a path is opened, posted and closed.

    Everything that needs no database (mode, attributes, the conversion)
    runs before a path is opened, so an invalid value never creates a file.
    """
    if not isinstance(target, famepy.FameDatabase) and mode is None:
        raise ValueError("Writing to a path needs an explicit mode.")
    if mode is not None:
        famepy.AccessMode(famepy._constants.access_mode(mode))
    famepy._data.attribute_codes(basis, observed)
    session = famepy.bridge.owner_session(
        target if isinstance(target, famepy.FameDatabase) else None
    )
    obj = famepy.refame(name, data, session=session, empty=empty, text=text)
    database, owned = _target(target, mode)
    try:
        famepy.do_write(obj, database, replace=replace, basis=basis, observed=observed)
        if owned:
            famepy.postdb(database)
    finally:
        if owned:
            famepy.closedb(database)
