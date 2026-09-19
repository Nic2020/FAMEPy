# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Conversion semantics adapted from FAME.jl (Bridge.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""TimeSeriesEconPy bridge: values, series, frequencies and workspaces.

FAMEPy depends on TimeSeriesEconPy through its public API only; the
dependency never runs the other way. Three layers:

* frequencies (``fame_frequency``, ``tsecon_frequency``, ``mit_to_index``,
  ``index_to_mit``): the supported calendar set and index conversions
  through the library's own year/period functions;
* values (``to_fame``, ``from_fame`` and the carriers ``NameList``, ``Text``,
  ``DateSeries``, ``StringSeries``): one Python value to and from one raw
  object, with explicit missing, empty and text policies (the text policy
  selects the encoding of string values in both directions: ``ascii`` by
  default, ``bytes``, or strict ``utf-8``);
* single objects (``read_value``, ``write_value``, ``read_tseries``,
  ``write_tseries``, ``read_scalar``, ``write_scalar``) and workspaces
  (``read_workspace``, ``write_workspace`` and their ``*_report`` variants).

Path targets validate everything that does not need the database (types,
frequencies, dtypes, exact integer conversion, policies, object names)
before the database file is opened, so an invalid input never truncates or
creates a file; they post after success and always close. Database targets
never post.
"""

from __future__ import annotations

import os
from typing import Any

from tsecon import MIT, TSeries

from .._constants import access_mode
from .._data import (
    RawObject,
    RawScalar,
    RawSeries,
    attribute_codes,
    read_object,
    write_object,
)
from .._database import Database
from .._errors import DataValidationError
from .._objects import ObjectInfo
from .._runtime import Session
from .._text import object_name
from ._frequencies import (
    SUPPORTED_FREQUENCY_CODES,
    UnsupportedFrequencyError,
    fame_frequency,
    index_to_mit,
    is_supported_frequency,
    mit_to_index,
    mit_to_year_period,
    owner_session,
    tsecon_frequency,
    year_period_to_mit,
)
from ._values import (
    EMPTY_POLICIES,
    MISSING_POLICIES,
    TEXT_POLICIES,
    DateSeries,
    EmptySeriesError,
    MissingValueError,
    NameList,
    StringSeries,
    Text,
    check_policies,
    from_fame,
    to_fame,
    validate_value,
)
from ._workspace import (
    NameCollisionError,
    ObjectFailure,
    ReadReport,
    WorkspaceCycleError,
    WriteReport,
    flatten_names,
    read_workspace,
    read_workspace_report,
    resolve_names,
    write_workspace,
    write_workspace_report,
)

Target = Database | str | bytes | os.PathLike[str]


def _resolve(target: Any, mode: Any) -> tuple[Database, bool]:
    from .._database import open_database

    if isinstance(target, Database):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return target, False
    if isinstance(target, (str, bytes, os.PathLike)):
        return open_database(target, "readonly" if mode is None else mode), True
    raise TypeError("Expected a Database or a database path.")


def _check_target(target: Any, mode: Any, *, writing: bool = False) -> None:
    """Type/mode checks that do not open anything."""
    if isinstance(target, Database):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return
    if not isinstance(target, (str, bytes, os.PathLike)):
        raise TypeError("Expected a Database or a database path.")
    if writing and mode is None:
        raise ValueError("Writing to a path needs an explicit mode.")


# -- series conversions (kept from the first bridge release) --------------------


def from_tseries(
    ts: TSeries,
    *,
    database: Database | None = None,
    session: Session | None = None,
    empty: str = "preserve",
) -> RawSeries:
    """Convert a TSeries (float64, float32, bool or exact integers) into a RawSeries."""
    if not isinstance(ts, TSeries):
        raise TypeError("from_tseries expects a TSeries.")
    raw = to_fame(ts, database=database, session=session, empty=empty)
    assert isinstance(raw, RawSeries)
    return raw


def to_tseries(
    raw: RawSeries,
    *,
    database: Database | None = None,
    session: Session | None = None,
    missing: str = "nan",
    empty: str = "preserve",
    empty_firstdate: MIT | None = None,
) -> TSeries:
    """Convert a precision, numeric or Boolean RawSeries into a TSeries."""
    if not isinstance(raw, RawSeries):
        raise TypeError("to_tseries expects a RawSeries.")
    if raw.kind not in ("precision", "numeric", "boolean"):
        raise DataValidationError(
            f"A {raw.kind} series is not a TSeries; use from_fame for its carrier."
        )
    value = from_fame(
        raw,
        database=database,
        session=session,
        missing=missing,
        empty=empty,
        empty_firstdate=empty_firstdate,
    )
    assert isinstance(value, TSeries)
    return value


# -- single objects -------------------------------------------------------------


def read_value(
    target: Target,
    name: str | bytes | ObjectInfo,
    *,
    missing: str = "nan",
    empty: str = "preserve",
    empty_firstdate: MIT | None = None,
    text: str = "ascii",
) -> Any:
    """Read one object of any supported kind. A path opens read-only and closes after."""
    check_policies(missing, empty, text)
    _check_target(target, None)
    database, owned = _resolve(target, None)
    try:
        raw = read_object(database, name)
        return from_fame(
            raw,
            database=database,
            missing=missing,
            empty=empty,
            empty_firstdate=empty_firstdate,
            text=text,
        )
    finally:
        if owned:
            database.close()


def write_value(
    target: Target,
    name: str | bytes,
    value: Any,
    *,
    mode: Any = None,
    replace: bool = False,
    empty: str = "preserve",
    basis: Any = None,
    observed: Any = None,
    text: str = "ascii",
) -> None:
    """Write one value of any supported kind.

    Given a Database, nothing is posted. Given a path, ``mode`` is required;
    the database is opened, written, posted on success and always closed.
    The value is fully validated and converted (including the string value
    encoding selected by ``text``) before the path is opened.
    """
    _check_target(target, mode, writing=True)
    if mode is not None:
        access_mode(mode)
    check_policies(empty=empty, text=text)
    object_name(name)
    attribute_codes(basis, observed)
    validate_value(value, empty, text)
    session = owner_session(target if isinstance(target, Database) else None)
    raw = to_fame(value, session=session, empty=empty, text=text)
    database, owned = _resolve(target, mode)
    try:
        write_object(database, name, raw, replace=replace, basis=basis, observed=observed)
        if owned:
            database.post()
    finally:
        if owned:
            database.close()


def read_tseries(
    target: Target,
    name: str | bytes | ObjectInfo,
    *,
    missing: str = "nan",
    empty: str = "preserve",
    empty_firstdate: MIT | None = None,
) -> TSeries:
    """Read one precision, numeric or Boolean series as a TSeries."""
    check_policies(missing, empty)
    _check_target(target, None)
    database, owned = _resolve(target, None)
    try:
        raw = read_object(database, name)
        if not isinstance(raw, RawSeries):
            raise DataValidationError("The object is a scalar; use read_scalar or read_value.")
        return to_tseries(
            raw, database=database, missing=missing, empty=empty, empty_firstdate=empty_firstdate
        )
    finally:
        if owned:
            database.close()


def write_tseries(
    target: Target,
    name: str | bytes,
    ts: TSeries,
    *,
    mode: Any = None,
    replace: bool = False,
    empty: str = "preserve",
    basis: Any = None,
    observed: Any = None,
) -> None:
    """Write one TSeries (float64, float32, bool or exact integers).

    Given a Database, nothing is posted. Given a path, ``mode`` is required
    (for example ``"update"``, ``"create"`` or ``"overwrite"``); the database is
    opened, written, posted on success and always closed. The series is fully
    validated before the path is opened. A failure after the object was
    replaced is not rolled back.
    """
    if not isinstance(ts, TSeries):
        raise TypeError("write_tseries expects a TSeries.")
    write_value(
        target,
        name,
        ts,
        mode=mode,
        replace=replace,
        empty=empty,
        basis=basis,
        observed=observed,
    )


def read_scalar(
    target: Target,
    name: str | bytes | ObjectInfo,
    *,
    missing: str = "nan",
    text: str = "ascii",
) -> Any:
    """Read one scalar of any kind (float, numpy.float32, bool, MIT, str, NameList)."""
    check_policies(missing, text=text)
    _check_target(target, None)
    database, owned = _resolve(target, None)
    try:
        raw = read_object(database, name)
        if not isinstance(raw, RawScalar):
            raise DataValidationError("The object is a series; use read_tseries or read_value.")
        return from_fame(raw, database=database, missing=missing, text=text)
    finally:
        if owned:
            database.close()


def write_scalar(
    target: Target,
    name: str | bytes,
    value: Any,
    *,
    mode: Any = None,
    replace: bool = False,
    text: str = "ascii",
) -> None:
    """Write one scalar value; NaN writes as NC. Validation precedes opening."""
    if isinstance(value, (TSeries, DateSeries, StringSeries, list, tuple)):
        raise TypeError("write_scalar expects a scalar value; use write_value for series.")
    write_value(target, name, value, mode=mode, replace=replace, text=text)


def raw_kind(value: Any) -> str:
    """The raw kind ``to_fame`` would produce for a value (validation only)."""
    from ._values import _prepare

    return _prepare(value, "preserve").kind


__all__ = [
    "EMPTY_POLICIES",
    "MISSING_POLICIES",
    "SUPPORTED_FREQUENCY_CODES",
    "TEXT_POLICIES",
    "DateSeries",
    "EmptySeriesError",
    "MissingValueError",
    "NameCollisionError",
    "NameList",
    "ObjectFailure",
    "RawObject",
    "ReadReport",
    "StringSeries",
    "Text",
    "UnsupportedFrequencyError",
    "WorkspaceCycleError",
    "WriteReport",
    "fame_frequency",
    "flatten_names",
    "from_fame",
    "from_tseries",
    "index_to_mit",
    "is_supported_frequency",
    "mit_to_index",
    "mit_to_year_period",
    "raw_kind",
    "read_scalar",
    "read_tseries",
    "read_value",
    "read_workspace",
    "read_workspace_report",
    "resolve_names",
    "to_fame",
    "to_tseries",
    "tsecon_frequency",
    "validate_value",
    "write_scalar",
    "write_tseries",
    "write_value",
    "write_workspace",
    "write_workspace_report",
    "year_period_to_mit",
]
