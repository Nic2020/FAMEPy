# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Conversion semantics adapted from FAME.jl (Bridge.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""First TimeSeriesEconPy bridge: monthly precision scalars and series.

Scope of this release: ``Monthly`` frequency and float64 values only. Other
frequencies, Boolean/date/string carriers, workspaces and multivariate series
are refused explicitly. FAMEPy depends on TimeSeriesEconPy through its public
API only; the dependency never runs the other way.

Missing values: FAME's NC, NA and ND observations all become NaN by default,
which is lossy; ``missing="strict"`` refuses them. NaN writes as NC, as in the
reference. Empty series: by default an empty TSeries becomes a truly empty
FAME series (its first date is not stored) and a truly empty FAME series can
only be read back with an explicit ``empty_firstdate``. The reference
convention (``empty="reference"``) stores one NA observation at the first date
and collapses a single missing observation to an empty series on read; that
encoding cannot distinguish an empty series from a one-observation missing
series, which is why it is opt-in.

Path forms validate everything that does not need the database (types,
frequency, dtype, exact integer conversion, option strings, the object name)
before the database file is opened, so an invalid input never truncates or
creates a file.
"""

from __future__ import annotations

import math
import os
from typing import Any

import numpy as np
from tsecon import MIT, Monthly, TSeries, mit2yp
from tsecon.frequencies import Frequency

from ._constants import FREQUENCY_MONTHLY, MISSING_NA, MISSING_NC, MISSING_NORMAL
from ._data import (
    RawScalar,
    RawSeries,
    classify_by_sentinel,
    read_object,
    sentinel_value,
    write_object,
)
from ._database import Database, open_database
from ._errors import DataValidationError, UnsupportedOperationError
from ._native import MAX_OBSERVATIONS, _int32
from ._objects import ObjectInfo
from ._text import object_name

_FREQUENCY_TO_FAME: dict[type[Frequency], int] = {Monthly: FREQUENCY_MONTHLY}
_FAME_TO_FREQUENCY: dict[int, Frequency] = {FREQUENCY_MONTHLY: Monthly()}
_MISSING_POLICIES = ("nan", "strict")
_EMPTY_POLICIES = ("preserve", "reference")


class UnsupportedFrequencyError(UnsupportedOperationError):
    """The frequency is outside the bridge's implemented set."""


class MissingValueError(ValueError):
    """Strict conversion met a missing observation."""


class EmptySeriesError(ValueError):
    """A truly empty FAME series has no stored first date to rebuild a TSeries."""


def fame_frequency(frequency: Frequency) -> int:
    for cls, code in _FREQUENCY_TO_FAME.items():
        if type(frequency) is cls:
            return code
    raise UnsupportedFrequencyError("Only monthly series are supported by this bridge release.")


def tsecon_frequency(code: int) -> Frequency:
    try:
        return _FAME_TO_FREQUENCY[code]
    except KeyError:
        raise UnsupportedFrequencyError(
            "Only FAME monthly objects are supported by this bridge release."
        ) from None


def mit_to_index(mit: MIT, *, database: Database) -> int:
    """Convert a monthly MIT to a FAME index through the library's calendar."""
    code = fame_frequency(mit.frequency)
    year, period = mit2yp(mit)
    with database.session.operation("year_period_to_index") as native:
        return native.year_period_to_index(code, year, period)


def index_to_mit(index: int, code: int, *, database: Database) -> MIT:
    frequency = tsecon_frequency(code)
    with database.session.operation("index_to_year_period") as native:
        year, period = native.index_to_year_period(code, index)
    return (
        MIT.from_yp(Monthly(), year, period)
        if isinstance(frequency, Monthly)
        else MIT(frequency, index)
    )


def _check_policies(missing: str | None = None, empty: str | None = None) -> None:
    if missing is not None and missing not in _MISSING_POLICIES:
        raise ValueError("missing must be 'nan' or 'strict'.")
    if empty is not None and empty not in _EMPTY_POLICIES:
        raise ValueError("empty must be 'preserve' or 'reference'.")


def _exact_float64(values: np.ndarray) -> np.ndarray:
    """Convert an integer array to float64, refusing any inexact element."""
    converted = values.astype(np.float64)
    limit = 2.0**64 if values.dtype.kind == "u" else 2.0**63
    with np.errstate(all="ignore"):
        lower = 0.0 if values.dtype.kind == "u" else -limit
        representable = (converted >= lower) & (converted < limit)
        back = np.zeros(values.shape, dtype=values.dtype)
        back[representable] = converted[representable].astype(values.dtype)
    exact = representable & (back == values)
    if not bool(np.all(exact)):
        raise DataValidationError("Some integers are not exactly representable in float64.")
    return converted


def _precision_values(values: np.ndarray, what: str) -> np.ndarray:
    """Return an owned float64 copy; integers must convert exactly."""
    if values.ndim != 1:
        raise DataValidationError(f"{what} must be one-dimensional.")
    if values.dtype == np.float64:
        return np.array(values, dtype=np.float64, copy=True)
    if np.issubdtype(values.dtype, np.integer):
        return _exact_float64(values)
    raise DataValidationError(
        f"{what} must be float64 (or exactly representable integers); got {values.dtype}."
    )


def _check_tseries(ts: TSeries, empty: str) -> tuple[int, np.ndarray | None]:
    """All database-independent validation of a TSeries to write."""
    if not isinstance(ts, TSeries):
        raise TypeError("from_tseries expects a TSeries.")
    _check_policies(empty=empty)
    code = fame_frequency(ts.frequency)
    year, period = mit2yp(ts.firstdate)
    _int32(year, "year")
    _int32(period, "period")
    if len(ts) > MAX_OBSERVATIONS:
        raise DataValidationError("Series length exceeds the supported bound.")
    values = _precision_values(ts.values, "TSeries values")
    return code, values if len(ts) else None


def _build_raw(
    code: int, values: np.ndarray | None, ts: TSeries, *, database: Database, empty: str
) -> RawSeries:
    sentinels = database.session.sentinels
    first = mit_to_index(ts.firstdate, database=database)
    if values is None:
        if empty == "reference":
            data = np.array([sentinel_value("precision", MISSING_NA, sentinels)])
            return RawSeries("precision", code, first, data)
        return RawSeries("precision", code, sentinels.index_nc, np.empty(0, dtype=np.float64))
    nan = np.isnan(values)
    if nan.any():
        values[nan] = sentinel_value("precision", MISSING_NC, sentinels)
    return RawSeries("precision", code, first, values)


def from_tseries(ts: TSeries, *, database: Database, empty: str = "preserve") -> RawSeries:
    """Convert a monthly float64 TSeries into a RawSeries with NaN encoded as NC."""
    code, values = _check_tseries(ts, empty)
    return _build_raw(code, values, ts, database=database, empty=empty)


def to_tseries(
    raw: RawSeries,
    *,
    database: Database,
    missing: str = "nan",
    empty: str = "preserve",
    empty_firstdate: MIT | None = None,
) -> TSeries:
    """Convert a monthly precision RawSeries into a TSeries."""
    if not isinstance(raw, RawSeries):
        raise TypeError("to_tseries expects a RawSeries.")
    if raw.kind != "precision":
        raise UnsupportedOperationError("Only precision series are bridged in this release.")
    _check_policies(missing, empty)
    frequency = tsecon_frequency(raw.frequency)
    sentinels = database.session.sentinels
    if raw.is_empty:
        if empty_firstdate is None:
            raise EmptySeriesError(
                "The FAME series is empty and stores no first date; pass empty_firstdate."
            )
        if empty_firstdate.frequency != frequency:
            raise DataValidationError("empty_firstdate has the wrong frequency.")
        return TSeries(empty_firstdate, np.empty(0, dtype=np.float64))
    codes = classify_by_sentinel(raw.values, "precision", sentinels)
    firstdate = index_to_mit(raw.first_index, raw.frequency, database=database)
    if empty == "reference" and len(raw) == 1 and codes[0] != MISSING_NORMAL:
        return TSeries(firstdate, np.empty(0, dtype=np.float64))
    if missing == "strict" and np.any(codes != MISSING_NORMAL):
        raise MissingValueError("The series contains missing observations.")
    values = np.array(raw.values, dtype=np.float64, copy=True)
    values[codes != MISSING_NORMAL] = np.nan
    return TSeries(firstdate, values)


def _resolve(target: Any, mode: Any) -> tuple[Database, bool]:
    if isinstance(target, Database):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return target, False
    if isinstance(target, (str, bytes, os.PathLike)):
        if mode is None:
            mode = "readonly"
        return open_database(target, mode), True
    raise TypeError("Expected a Database or a database path.")


def _check_target(target: Any, mode: Any) -> None:
    """Type/mode checks that do not open anything."""
    if isinstance(target, Database):
        if mode is not None:
            raise ValueError("mode applies only when a path is given.")
        return
    if not isinstance(target, (str, bytes, os.PathLike)):
        raise TypeError("Expected a Database or a database path.")


def read_tseries(
    target: Database | str | bytes | os.PathLike[str],
    name: str | bytes | ObjectInfo,
    *,
    missing: str = "nan",
    empty: str = "preserve",
    empty_firstdate: MIT | None = None,
) -> TSeries:
    """Read one monthly precision series. A path opens read-only and closes after."""
    _check_policies(missing, empty)
    _check_target(target, None)
    database, owned = _resolve(target, None)
    try:
        raw = read_object(database, name)
        if not isinstance(raw, RawSeries):
            raise DataValidationError("The object is a scalar; use read_scalar.")
        return to_tseries(
            raw, database=database, missing=missing, empty=empty, empty_firstdate=empty_firstdate
        )
    finally:
        if owned:
            database.close()


def write_tseries(
    target: Database | str | bytes | os.PathLike[str],
    name: str | bytes,
    ts: TSeries,
    *,
    mode: Any = None,
    replace: bool = False,
    empty: str = "preserve",
) -> None:
    """Write one monthly float64 series.

    Given a Database, nothing is posted. Given a path, ``mode`` is required
    (for example ``"update"``, ``"create"`` or ``"overwrite"``); the database is
    opened, written, posted on success and always closed. The series is fully
    validated before the path is opened. A failure after the object was
    replaced is not rolled back.
    """
    _check_target(target, mode)
    if not isinstance(target, Database) and mode is None:
        raise ValueError("Writing to a path needs an explicit mode.")
    object_name(name)
    code, values = _check_tseries(ts, empty)
    database, owned = _resolve(target, mode)
    try:
        raw = _build_raw(code, values, ts, database=database, empty=empty)
        write_object(database, name, raw, replace=replace)
        if owned:
            database.post()
    finally:
        if owned:
            database.close()


def read_scalar(
    target: Database | str | bytes | os.PathLike[str],
    name: str | bytes | ObjectInfo,
    *,
    missing: str = "nan",
) -> float:
    """Read one precision scalar; missing categories become NaN unless strict."""
    _check_policies(missing)
    _check_target(target, None)
    database, owned = _resolve(target, None)
    try:
        raw = read_object(database, name)
        if not isinstance(raw, RawScalar) or raw.kind != "precision":
            raise DataValidationError("The object is not a precision scalar.")
        code = int(
            classify_by_sentinel(
                np.array([raw.value], dtype=np.float64), "precision", database.session.sentinels
            )[0]
        )
        if code != MISSING_NORMAL:
            if missing == "strict":
                raise MissingValueError("The scalar is missing.")
            return math.nan
        return float(raw.value)
    finally:
        if owned:
            database.close()


def _scalar_number(value: Any) -> float:
    """Validate a real number for a precision scalar; integers must be exact."""
    if isinstance(value, bool) or not isinstance(value, (int, float, np.floating, np.integer)):
        raise TypeError("write_scalar expects a real number.")
    if isinstance(value, (int, np.integer)):
        integer = int(value)
        try:
            number = float(integer)
        except OverflowError:
            raise DataValidationError(
                "The integer cannot be represented exactly as float64."
            ) from None
        if int(number) != integer:
            raise DataValidationError("The integer cannot be represented exactly as float64.")
        return number
    return float(value)


def write_scalar(
    target: Database | str | bytes | os.PathLike[str],
    name: str | bytes,
    value: float,
    *,
    mode: Any = None,
    replace: bool = False,
) -> None:
    """Write one precision scalar; NaN writes as NC. Validation precedes opening."""
    _check_target(target, mode)
    if not isinstance(target, Database) and mode is None:
        raise ValueError("Writing to a path needs an explicit mode.")
    object_name(name)
    number = _scalar_number(value)
    database, owned = _resolve(target, mode)
    try:
        if math.isnan(number):
            number = sentinel_value("precision", MISSING_NC, database.session.sentinels)
        write_object(database, name, RawScalar("precision", number), replace=replace)
        if owned:
            database.post()
    finally:
        if owned:
            database.close()


__all__ = [
    "EmptySeriesError",
    "MissingValueError",
    "UnsupportedFrequencyError",
    "fame_frequency",
    "from_tseries",
    "index_to_mit",
    "mit_to_index",
    "read_scalar",
    "read_tseries",
    "to_tseries",
    "tsecon_frequency",
    "write_scalar",
    "write_tseries",
]
