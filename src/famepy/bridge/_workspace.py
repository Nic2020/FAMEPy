# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Workspace semantics adapted from FAME.jl (Bridge.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""Workspace reads and writes (the reference's readfame and writefame).

Reading: positional names are explicit object names or wildcard patterns
(``?`` any run, ``^`` one character). A wildcard is expanded with
``list_objects`` and the listing filters; an explicit name is looked up
whatever its class, type or frequency. Names are transformed in this order:
the ``prefix`` (joined by ``glue``) is stripped from the start when present,
``collect`` entries nest matching names into sub-workspaces, and finally
``namecase`` (``str.lower`` by default) produces the key. Every destination
is computed before any object is read, so two different objects that would
land on the same key, or a key that would be both a value and a nested
workspace, are refused (``NameCollisionError``) instead of one silently
overwriting the other. The same object matched twice is read once. Explicit
names keep their argument order; wildcard matches are ordered by name bytes
(a package rule; the library's cursor order is not relied upon).

The strict functions raise at the first failure. The ``*_report`` variants
contain failures per object and return the partial result together with a
record of every failure (error class and numeric status, never library
text) so that a partial result is never mistaken for a complete one.

Writing: workspaces, mappings and multivariate series are flattened
recursively by joining names with ``glue`` (an optional ``prefix`` is
prepended to every top-level name; ``prefix=""`` still adds the glue).
Every flattened name is validated, checked for collisions under the
library's case-insensitive naming, and every value is validated and
converted before the first create, replace or delete. Existing objects are
replaced by default, as the reference does. With a path target the database
is opened in the given mode, posted after a fully successful write and
always closed; with a database handle nothing is posted. No rollback is
promised: a failure after some objects were replaced leaves them replaced.
Multivariate series are written as one series per column and read back as
separate series; the reference does not reconstruct them and neither does
this package (use ``collect`` to nest the columns under the original name).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsecon import MVTSeries, Workspace

from .._constants import access_mode
from .._data import RawObject, attribute_codes, read_object, write_object
from .._database import Database, open_database
from .._errors import DataValidationError, FameError, UnsupportedOperationError
from .._objects import ObjectInfo, quick_info
from .._text import TextEncodingError, object_name
from .._wildcard import is_wildcard, list_objects
from ._frequencies import UnsupportedFrequencyError, owner_session
from ._values import (
    EmptySeriesError,
    MissingValueError,
    check_policies,
    from_fame,
    to_fame,
    validate_value,
)

__all__ = [
    "NameCollisionError",
    "ObjectFailure",
    "ReadReport",
    "WorkspaceCycleError",
    "WriteReport",
    "flatten_names",
    "read_workspace",
    "read_workspace_report",
    "resolve_names",
    "write_workspace",
    "write_workspace_report",
]

CollectSpec = Any  # str | tuple[str, Sequence[CollectSpec]] | Mapping[str, Sequence] | list


class NameCollisionError(ValueError):
    """Two objects would occupy the same name after transformation."""


class WorkspaceCycleError(ValueError):
    """A workspace or mapping contains itself, directly or indirectly."""


# Errors that mean "this object cannot be represented", as opposed to native
# failures; the raw carrier is a lossless fallback for them.
CONVERSION_ERRORS: tuple[type[Exception], ...] = (
    UnsupportedFrequencyError,
    MissingValueError,
    EmptySeriesError,
    TextEncodingError,
    DataValidationError,
    UnsupportedOperationError,
)


@dataclass(frozen=True)
class ObjectFailure:
    """One object that could not be read, converted or written."""

    name: str
    key: tuple[str, ...]
    error: BaseException

    @property
    def error_type(self) -> str:
        return type(self.error).__name__

    @property
    def status(self) -> int | None:
        status = getattr(self.error, "status", None)
        return status if isinstance(status, int) and not isinstance(status, bool) else None

    def __str__(self) -> str:
        where = ".".join(self.key) if self.key else self.name
        suffix = "" if self.status is None else f" (status {self.status})"
        return f"{where}: {self.error_type}{suffix}"


@dataclass(frozen=True)
class ReadReport:
    """The partial workspace of a contained read plus every failure."""

    workspace: Workspace
    failures: tuple[ObjectFailure, ...] = ()
    raw: tuple[str, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class WriteReport:
    """The names written by a contained write plus every failure."""

    written: tuple[str, ...]
    failures: tuple[ObjectFailure, ...] = ()
    posted: bool = False

    @property
    def complete(self) -> bool:
        return not self.failures


# -- name transformation --------------------------------------------------------


def _normalize_collect(
    collect: CollectSpec, _stack: tuple[int, ...] = ()
) -> list[tuple[str, list[Any]]]:
    """Return ``[(prefix, nested), ...]`` from the accepted collect spellings.

    A name, a mapping ``{name: nested}``, or a list/tuple of entries. An
    entry is a name, a mapping, or a ``(name, nested)`` pair whose second
    element is a list, tuple or mapping; ``("c", "s")`` is two names. A
    specification that contains itself is refused.
    """
    if collect is None:
        return []
    if isinstance(collect, str):
        entries: list[Any] = [collect]
    elif isinstance(collect, Mapping):
        entries = [(key, value) for key, value in collect.items()]
    elif isinstance(collect, (list, tuple)):
        entries = list(collect)
    else:
        raise TypeError("collect must be a name, a mapping or a list of entries.")
    if not isinstance(collect, str):
        if id(collect) in _stack:
            raise ValueError("The collect specification contains itself.")
        _stack = (*_stack, id(collect))
    result: list[tuple[str, list[Any]]] = []
    for item in entries:
        if isinstance(item, str):
            if not item:
                raise ValueError("A collect name cannot be empty.")
            result.append((item, []))
        elif isinstance(item, Mapping):
            result.extend(_normalize_collect(item, _stack))
        elif (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], (list, tuple, Mapping))
        ):
            if not item[0]:
                raise ValueError("A collect name cannot be empty.")
            result.append((item[0], _normalize_collect(item[1], _stack)))
        else:
            raise TypeError("collect entries are names, mappings or (name, nested) pairs.")
    return result


def _apply_namecase(namecase: Callable[[str], str], name: str) -> str:
    key = namecase(name)
    if not isinstance(key, str) or not key:
        raise ValueError("namecase must return a non-empty str.")
    return key


def _destination(
    name: str,
    *,
    glue: str,
    namecase: Callable[[str], str],
    prefix: str | None,
    collect: list[tuple[str, list[Any]]],
) -> tuple[str, ...]:
    """The key path of a FAME name (nested workspace keys, then the member key).

    Matching is done on the library's name (upper-cased) with the collect
    prefix followed by the glue; on a match the whole prefix and glue are
    removed and the output key is produced separately (the prefix as given,
    or ``namecase`` of the first glue-separated part for ``"?"``/``"*"``).
    The reference helper removes one split part and reuses the transformed
    key for matching; both quirks are deliberately not reproduced.
    """
    if prefix is not None:
        stripped = (prefix + glue).upper()
        if name.startswith(stripped):
            name = name[len(stripped) :]
    if collect and not glue:
        raise ValueError("collect needs a non-empty glue.")
    path: list[str] = []
    pending = collect
    while pending:
        matched = False
        for wpref, nested in pending:
            if wpref in ("?", "*"):
                first, separator, _rest = name.partition(glue)
                if not separator or not first:
                    continue
                match, key = first, _apply_namecase(namecase, first)
            else:
                match, key = wpref, wpref
            stripped = (match + glue).upper()
            if name.startswith(stripped) and len(name) > len(stripped):
                path.append(key)
                name = name[len(stripped) :]
                pending = nested
                matched = True
                break
        if not matched:
            break
    return (*path, _apply_namecase(namecase, name))


def _fame_name_text(info: ObjectInfo) -> str:
    return info.name_text.upper()


def resolve_names(
    database: Database,
    names: Sequence[str | bytes],
    *,
    namecase: Callable[[str], str] = str.lower,
    prefix: str | None = None,
    glue: str = "_",
    collect: CollectSpec = (),
    alias: bool = True,
    classes: Any = None,
    types: Any = None,
    frequencies: Any = None,
) -> list[tuple[str, tuple[str, ...]]]:
    """Resolve arguments to ``[(FAME name, key path), ...]`` and refuse collisions.

    Explicit names are queried with ``quick_info`` (an absent object raises
    the library's status); wildcards are listed with the filters. Duplicated
    objects are kept once, at their first position.
    """
    if not callable(namecase):
        raise TypeError("namecase must be callable.")
    if not isinstance(glue, str):
        raise TypeError("glue must be a str.")
    if prefix is not None and not isinstance(prefix, str):
        raise TypeError("prefix must be a str or None.")
    spec = _normalize_collect(collect)
    ordered: list[str] = []
    seen: set[str] = set()
    for argument in names or ("?",):
        if is_wildcard(argument):
            infos = list_objects(
                database,
                argument,
                alias=alias,
                classes=classes,
                types=types,
                frequencies=frequencies,
            )
            found = sorted(_fame_name_text(info) for info in infos)
        else:
            found = [_fame_name_text(quick_info(database, argument))]
        for text in found:
            if text not in seen:
                seen.add(text)
                ordered.append(text)
    resolved: list[tuple[str, tuple[str, ...]]] = []
    leaves: dict[tuple[str, ...], str] = {}
    branches: set[tuple[str, ...]] = set()
    for text in ordered:
        key = _destination(text, glue=glue, namecase=namecase, prefix=prefix, collect=spec)
        if key in leaves:
            raise NameCollisionError(
                f"Objects {leaves[key]} and {text} both map to {'.'.join(key)}."
            )
        for depth in range(1, len(key)):
            branches.add(key[:depth])
        leaves[key] = text
        resolved.append((text, key))
    for branch in branches:
        if branch in leaves:
            raise NameCollisionError(
                f"{'.'.join(branch)} is both an object ({leaves[branch]}) and a nested workspace."
            )
    return resolved


def _place(root: Workspace, key: tuple[str, ...], value: Any) -> None:
    node = root
    for part in key[:-1]:
        child = node.get(part)
        if child is None:
            child = Workspace()
            node[part] = child
        node = child
    node[key[-1]] = value


# -- reading --------------------------------------------------------------------


def _resolve_target(target: Any, mode: Any) -> tuple[Database, bool]:
    if isinstance(target, Database):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return target, False
    if isinstance(target, (str, bytes, os.PathLike)):
        return open_database(target, "readonly" if mode is None else mode), True
    raise TypeError("Expected a Database or a database path.")


def _check_target(target: Any, mode: Any) -> None:
    if isinstance(target, Database):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return
    if not isinstance(target, (str, bytes, os.PathLike)):
        raise TypeError("Expected a Database or a database path.")


def _read_into(
    database: Database,
    resolved: list[tuple[str, tuple[str, ...]]],
    *,
    missing: str,
    empty: str,
    text: str,
    raw_fallback: bool,
    contain: bool,
) -> ReadReport:
    workspace = Workspace()
    failures: list[ObjectFailure] = []
    raw_names: list[str] = []
    for name, key in resolved:
        try:
            raw: RawObject = read_object(database, name)
        except (FameError, UnsupportedOperationError, DataValidationError) as error:
            # A native status, an unsupported class (formula, global) or an
            # unreadable type: there is no raw carrier to fall back on.
            if not contain:
                raise
            failures.append(ObjectFailure(name, key, error))
            continue
        try:
            value = from_fame(raw, database=database, missing=missing, empty=empty, text=text)
        except CONVERSION_ERRORS as error:
            if raw_fallback:
                value = raw
                raw_names.append(name)
            elif contain:
                failures.append(ObjectFailure(name, key, error))
                continue
            else:
                raise
        _place(workspace, key, value)
    return ReadReport(workspace, tuple(failures), tuple(raw_names))


def read_workspace_report(
    target: Database | str | bytes | os.PathLike[str],
    *names: str | bytes,
    namecase: Callable[[str], str] = str.lower,
    prefix: str | None = None,
    glue: str = "_",
    collect: CollectSpec = (),
    missing: str = "nan",
    empty: str = "preserve",
    text: str = "ascii",
    raw_fallback: bool = False,
    alias: bool = True,
    classes: Any = None,
    types: Any = None,
    frequencies: Any = None,
) -> ReadReport:
    """Read objects into a workspace, containing per-object failures.

    Name resolution (including absent explicit names and collisions) is
    strict; only the read and conversion of each resolved object is
    contained. With ``raw_fallback=True`` an object the bridge cannot
    represent is stored as its ``RawScalar``/``RawSeries`` and listed in
    ``report.raw``; native read failures are always failures.
    """
    check_policies(missing, empty, text)
    _check_target(target, None)
    database, owned = _resolve_target(target, None)
    try:
        resolved = resolve_names(
            database,
            names,
            namecase=namecase,
            prefix=prefix,
            glue=glue,
            collect=collect,
            alias=alias,
            classes=classes,
            types=types,
            frequencies=frequencies,
        )
        return _read_into(
            database,
            resolved,
            missing=missing,
            empty=empty,
            text=text,
            raw_fallback=raw_fallback,
            contain=True,
        )
    finally:
        if owned:
            database.close()


def read_workspace(
    target: Database | str | bytes | os.PathLike[str],
    *names: str | bytes,
    namecase: Callable[[str], str] = str.lower,
    prefix: str | None = None,
    glue: str = "_",
    collect: CollectSpec = (),
    missing: str = "nan",
    empty: str = "preserve",
    text: str = "ascii",
    raw_fallback: bool = False,
    alias: bool = True,
    classes: Any = None,
    types: Any = None,
    frequencies: Any = None,
) -> Workspace:
    """Read objects into a ``Workspace``; the first failure raises.

    With no names every object is read (``"?"``). A path opens read-only and
    is closed afterwards. See the module note for the name transformations.
    """
    check_policies(missing, empty, text)
    _check_target(target, None)
    database, owned = _resolve_target(target, None)
    try:
        resolved = resolve_names(
            database,
            names,
            namecase=namecase,
            prefix=prefix,
            glue=glue,
            collect=collect,
            alias=alias,
            classes=classes,
            types=types,
            frequencies=frequencies,
        )
        return _read_into(
            database,
            resolved,
            missing=missing,
            empty=empty,
            text=text,
            raw_fallback=raw_fallback,
            contain=False,
        ).workspace
    finally:
        if owned:
            database.close()


# -- writing --------------------------------------------------------------------

_CONTAINERS = (Workspace, MVTSeries, Mapping)


def _items(container: Any) -> Iterable[tuple[Any, Any]]:
    if isinstance(container, MVTSeries):
        return container.columns.items()
    if isinstance(container, Workspace):
        return container.items()
    mapping: Mapping[Any, Any] = container
    return mapping.items()


def flatten_names(
    data: Sequence[Any], *, prefix: str | None = None, glue: str = "_"
) -> list[tuple[str, Any]]:
    """Flatten containers to ``[(FAME name, value), ...]`` without touching the library.

    Refuses non-container inputs, non-string keys, cycles, invalid names and
    collisions under the library's case-insensitive naming. Values are not
    converted here.
    """
    if not isinstance(glue, str):
        raise TypeError("glue must be a str.")
    if prefix is not None and not isinstance(prefix, str):
        raise TypeError("prefix must be a str or None.")
    flat: list[tuple[str, Any]] = []
    for container in data:
        if not isinstance(container, _CONTAINERS):
            raise TypeError(
                "write_workspace expects Workspace, mapping or MVTSeries values, "
                f"not {type(container).__name__}."
            )
        _flatten_into(container, prefix, glue, flat, [])
    seen: dict[str, str] = {}
    for name, _ in flat:
        object_name(name)
        upper = name.upper()
        if upper in seen:
            raise NameCollisionError(
                f"Flattened names {seen[upper]} and {name} collide (names are case-insensitive)."
            )
        seen[upper] = name
    return flat


def _flatten_into(
    container: Any,
    prefix: str | None,
    glue: str,
    flat: list[tuple[str, Any]],
    stack: list[int],
) -> None:
    if id(container) in stack:
        raise WorkspaceCycleError("The workspace contains itself.")
    stack.append(id(container))
    try:
        for key, value in _items(container):
            if not isinstance(key, str):
                raise TypeError("Workspace and mapping keys must be str.")
            name = key if prefix is None else f"{prefix}{glue}{key}"
            if isinstance(value, _CONTAINERS):
                _flatten_into(value, name, glue, flat, stack)
            else:
                flat.append((name, value))
    finally:
        stack.pop()


def _prepare_write(
    target: Any,
    mode: Any,
    data: Sequence[Any],
    prefix: str | None,
    glue: str,
    empty: str,
    basis: Any,
    observed: Any,
) -> list[tuple[str, Any]]:
    """Every check of a workspace write that needs no session or database.

    Target/mode form, policies, attributes, flattening (keys, cycles, names,
    collisions) are validated here; values are validated per object by the
    callers (strictly, or contained in report mode).
    """
    _check_target(target, mode)
    if not isinstance(target, Database) and mode is None:
        raise ValueError("Writing to a path needs an explicit mode.")
    if mode is not None:
        access_mode(mode)
    check_policies(empty=empty)
    attribute_codes(basis, observed)
    return flatten_names(data, prefix=prefix, glue=glue)


def _convert_all(
    flat: list[tuple[str, Any]], *, session: Any, empty: str
) -> list[tuple[str, RawObject]]:
    return [(name, to_fame(value, session=session, empty=empty)) for name, value in flat]


def write_workspace(
    target: Database | str | bytes | os.PathLike[str],
    *data: Workspace | Mapping[str, Any] | MVTSeries,
    mode: Any = None,
    prefix: str | None = None,
    glue: str = "_",
    replace: bool = True,
    empty: str = "preserve",
    basis: Any = None,
    observed: Any = None,
) -> tuple[str, ...]:
    """Write workspaces, mappings or multivariate series; the first failure raises.

    Returns the FAME names written. Given a path, ``mode`` is required (the
    reference defaults to overwrite; this package asks for the mode
    explicitly), the database is posted after every object was written and
    always closed. Given a database handle nothing is posted. Every
    validation and conversion completes before the destination is opened;
    with nothing to write (empty inputs) the destination is not opened at
    all and ``()`` is returned.
    """
    flat = _prepare_write(target, mode, data, prefix, glue, empty, basis, observed)
    for _, value in flat:
        validate_value(value, empty)
    session = owner_session(target if isinstance(target, Database) else None)
    converted = _convert_all(flat, session=session, empty=empty)
    if not converted:
        return ()
    database, owned = _resolve_target(target, mode)
    try:
        written: list[str] = []
        for name, raw in converted:
            _write_one(database, name, raw, replace, basis, observed)
            written.append(name)
        if owned:
            database.post()
        return tuple(written)
    finally:
        if owned:
            database.close()


def write_workspace_report(
    target: Database | str | bytes | os.PathLike[str],
    *data: Workspace | Mapping[str, Any] | MVTSeries,
    mode: Any = None,
    prefix: str | None = None,
    glue: str = "_",
    replace: bool = True,
    empty: str = "preserve",
    basis: Any = None,
    observed: Any = None,
) -> WriteReport:
    """Write with per-object containment: every object is attempted.

    Flattening, name validation, collisions, cycles, policies and attributes
    stay strict (nothing is written when they fail). An invalid value, a
    conversion failure or a native failure of one object is recorded and the
    others are still written. The destination is opened only when at least
    one object converted: when every object failed, or there was nothing to
    write, the report is returned without creating, truncating or opening
    anything. With a path the database is posted when at least one object
    was written (the report says so), then closed.
    """
    flat = _prepare_write(target, mode, data, prefix, glue, empty, basis, observed)
    session = owner_session(target if isinstance(target, Database) else None)
    failures: list[ObjectFailure] = []
    converted: list[tuple[str, RawObject]] = []
    for name, value in flat:
        try:
            validate_value(value, empty)
            converted.append((name, to_fame(value, session=session, empty=empty)))
        except (FameError, TypeError, ValueError, UnsupportedOperationError) as error:
            failures.append(ObjectFailure(name, (name,), error))
    if not converted:
        return WriteReport((), tuple(failures), False)
    database, owned = _resolve_target(target, mode)
    posted = False
    try:
        written: list[str] = []
        for name, raw in converted:
            try:
                _write_one(database, name, raw, replace, basis, observed)
            except (FameError, DataValidationError) as error:
                failures.append(ObjectFailure(name, (name,), error))
                continue
            written.append(name)
        if owned and written:
            database.post()
            posted = True
        return WriteReport(tuple(written), tuple(failures), posted)
    finally:
        if owned:
            database.close()


def _write_one(
    database: Database, name: str, raw: RawObject, replace: bool, basis: Any, observed: Any
) -> None:
    write_object(database, name, raw, replace=replace, basis=basis, observed=observed)
