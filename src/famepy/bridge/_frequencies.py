# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Frequency maps adapted from FAME.jl (Bridge.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""Frequency maps and calendar conversions between TimeSeriesEconPy and FAME.

Two notions of time are kept apart: the *index frequency* of a series (the
frequency of its observations' positions) and the *value frequency* of a
date-valued observation. Both use the same maps.

Supported index and value frequencies: ``Unit`` (case), ``Daily``,
``BDaily`` (business), the seven weekly endings, ``Monthly``, the three
quarterly anchors, the six half-yearly anchors and the twelve annual anchors.
Every other frequency in the library's table (ten-day, biweekly, twice
monthly, bimonthly, year-per-period, period-per-year, intraday and
user-defined weekly patterns) is refused with ``UnsupportedFrequencyError``;
nothing is remapped to an ordinary calendar.

Indices are converted through the library's own year/period functions. The
year/period *conventions* (which year and period number a moment carries)
are the reference's, computed here from public TimeSeriesEconPy calendar
functions: year-period frequencies decompose directly; daily and business
moments use the year and the day (business day) number within the year;
weekly moments use the year of the week's ending day and the week number
``ceil(day_of_year / 7)`` of that day. Case moments are not calendar dates:
their index is the moment's integer value and never touches the library.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from typing import Any

from tsecon import (
    MIT,
    BDaily,
    Daily,
    HalfYearly,
    Monthly,
    Quarterly,
    Unit,
    Weekly,
    Yearly,
    bdaily,
    daily,
    mit2yp,
    mit_to_date,
    weekly,
)
from tsecon.frequencies import Frequency, YPFrequency

from .._constants import FREQUENCIES, FREQUENCY_CASE, FREQUENCY_NAMES, frequency_code
from .._database import Database
from .._errors import DataValidationError, UnsupportedOperationError
from .._native import _int32
from .._runtime import Session, current_session

__all__ = [
    "SUPPORTED_FREQUENCY_CODES",
    "UnsupportedFrequencyError",
    "fame_frequency",
    "index_to_mit",
    "is_supported_frequency",
    "mit_to_index",
    "mit_to_year_period",
    "tsecon_frequency",
    "year_period_to_mit",
]


class UnsupportedFrequencyError(UnsupportedOperationError):
    """The frequency is outside the bridge's implemented set."""


_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Reference maps. Quarterly anchors: the library names a quarterly frequency
# by one of the three equivalent ending months (october/november/december);
# the reference maps ending month ``m`` to quarterly anchor ``((m - 1) % 3) + 1``.
# Half-yearly anchors likewise use the six equivalent later months
# (july..december) and annual anchors name the ending month directly.
_WEEKLY_CODES: dict[int, int] = {
    day: FREQUENCIES[f"weekly_{name}"] for day, name in enumerate(_DAYS, start=1)
}
_QUARTERLY_CODES: dict[int, int] = {
    1: FREQUENCIES["quarterly_october"],
    2: FREQUENCIES["quarterly_november"],
    3: FREQUENCIES["quarterly_december"],
}
_HALFYEARLY_CODES: dict[int, int] = {
    anchor: FREQUENCIES[f"semiannual_{name}"] for anchor, name in enumerate(_MONTHS[6:], start=1)
}
_YEARLY_CODES: dict[int, int] = {
    month: FREQUENCIES[f"annual_{name}"] for month, name in enumerate(_MONTHS, start=1)
}
_PLAIN_CODES: dict[type[Frequency], int] = {
    Unit: FREQUENCY_CASE,
    Daily: FREQUENCIES["daily"],
    BDaily: FREQUENCIES["business"],
    Monthly: FREQUENCIES["monthly"],
}


def _build_to_tsecon() -> dict[int, Frequency]:
    table: dict[int, Frequency] = {}
    for cls, code in _PLAIN_CODES.items():
        table[code] = cls()
    for day, code in _WEEKLY_CODES.items():
        table[code] = Weekly(day)
    for anchor, code in _QUARTERLY_CODES.items():
        table[code] = Quarterly(anchor)
    for anchor, code in _HALFYEARLY_CODES.items():
        table[code] = HalfYearly(anchor)
    for month, code in _YEARLY_CODES.items():
        table[code] = Yearly(month)
    return table


_TO_TSECON: Mapping[int, Frequency] = _build_to_tsecon()
SUPPORTED_FREQUENCY_CODES: frozenset[int] = frozenset(_TO_TSECON)


def is_supported_frequency(code: int) -> bool:
    """True when the library frequency code has a bridge mapping."""
    return code in _TO_TSECON


def fame_frequency(frequency: Frequency) -> int:
    """The library frequency code of a TimeSeriesEconPy frequency."""
    if not isinstance(frequency, Frequency):
        raise TypeError("fame_frequency expects a tsecon Frequency instance.")
    if type(frequency) in _PLAIN_CODES:
        return _PLAIN_CODES[type(frequency)]
    if isinstance(frequency, Weekly):
        return _WEEKLY_CODES[frequency.end_day]
    if isinstance(frequency, Quarterly):
        return _QUARTERLY_CODES[frequency.end_month]
    if isinstance(frequency, HalfYearly):
        return _HALFYEARLY_CODES[frequency.end_month]
    if isinstance(frequency, Yearly):
        return _YEARLY_CODES[frequency.end_month]
    raise UnsupportedFrequencyError(
        f"The bridge has no FAME frequency for {type(frequency).__name__}."
    )


def tsecon_frequency(code: Any) -> Frequency:
    """The TimeSeriesEconPy frequency of a library code or name.

    Frequencies outside the bridge set (ten-day, biweekly, twice monthly,
    bimonthly, ypp, ppy, intraday, weekly pattern, undefined) raise
    ``UnsupportedFrequencyError`` naming the library frequency.
    """
    try:
        value = frequency_code(code)
    except (TypeError, ValueError):
        raise UnsupportedFrequencyError("Unknown FAME frequency.") from None
    try:
        return _TO_TSECON[value]
    except KeyError:
        raise UnsupportedFrequencyError(
            f"FAME frequency {FREQUENCY_NAMES[value]} has no TimeSeriesEconPy equivalent "
            "in this bridge."
        ) from None


# -- year/period conventions (pure Python, public tsecon calendar only) ------


def _check_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataValidationError(f"{what} must be an integer.")
    return value


def _year_bounds(year: int) -> None:
    # Python's calendar covers years 1..9999; the library documents 100..9999.
    if not 1 <= year <= 9999:
        raise DataValidationError("The year is outside the calendar's range.")


def _first_business_day(year: int) -> _dt.date:
    first = _dt.date(year, 1, 1)
    weekday = first.isoweekday()
    return first + _dt.timedelta(days=8 - weekday if weekday > 5 else 0)


def _business_days_in_year(year: int) -> int:
    start = bdaily(_first_business_day(year))
    end = bdaily(_dt.date(year, 12, 31), bias="previous")
    return end.value - start.value + 1


def _days_in_year(year: int) -> int:
    return (_dt.date(year, 12, 31) - _dt.date(year, 1, 1)).days + 1


def mit_to_year_period(mit: MIT) -> tuple[int, int]:
    """The library's year and period of a calendar moment (reference conventions).

    Case moments have no year/period: ``DataValidationError``.
    """
    if not isinstance(mit, MIT):
        raise TypeError("mit_to_year_period expects an MIT.")
    frequency = mit.frequency
    if isinstance(frequency, Unit):
        raise DataValidationError("A case moment has no year and period.")
    fame_frequency(frequency)
    try:
        if isinstance(frequency, (YPFrequency, Daily, BDaily)):
            year, period = mit2yp(mit)
            return int(year), int(period)
        if isinstance(frequency, Weekly):
            end = mit_to_date(mit)
            return end.year, -(-end.timetuple().tm_yday // 7)
    except (OverflowError, ValueError):
        raise DataValidationError("The moment is outside the calendar's range.") from None
    raise UnsupportedFrequencyError(
        f"The bridge has no FAME frequency for {type(frequency).__name__}."
    )


def year_period_to_mit(frequency: Frequency, year: int, period: int) -> MIT:
    """The calendar moment of a library year and period (reference conventions).

    The period must exist in that year for the frequency (for example a
    business day number within the year's business days, a week number that
    ends in that year); anything else is refused rather than normalized.
    """
    if not isinstance(frequency, Frequency):
        raise TypeError("year_period_to_mit expects a tsecon Frequency instance.")
    year = _check_int(year, "year")
    period = _check_int(period, "period")
    if isinstance(frequency, Unit):
        raise DataValidationError("A case moment has no year and period.")
    fame_frequency(frequency)
    if isinstance(frequency, YPFrequency):
        # Year-period moments are plain integers; the library's own year
        # bounds apply natively, Python's calendar is not involved.
        try:
            return MIT.from_yp(frequency, year, period)
        except ValueError:
            raise DataValidationError("The period is outside the frequency's year.") from None
    _year_bounds(year)
    if isinstance(frequency, Daily):
        if not 1 <= period <= _days_in_year(year):
            raise DataValidationError("The day number is outside that year.")
        return daily(_dt.date(year, 1, 1) + _dt.timedelta(days=period - 1))
    if isinstance(frequency, BDaily):
        if not 1 <= period <= _business_days_in_year(year):
            raise DataValidationError("The business day number is outside that year.")
        moment = bdaily(_first_business_day(year)) + (period - 1)
        assert isinstance(moment, MIT)
        return moment
    if isinstance(frequency, Weekly):
        if not 1 <= period <= 53:
            raise DataValidationError("The week number is outside 1 to 53.")
        start = _dt.date(year, 1, 1) + _dt.timedelta(days=7 * (period - 1))
        moment = weekly(start, frequency.end_day)
        if mit_to_year_period(moment) != (year, period):
            raise DataValidationError("No week with that number ends in that year.")
        return moment
    raise UnsupportedFrequencyError(  # pragma: no cover - every Frequency subclass handled
        f"The bridge has no FAME frequency for {type(frequency).__name__}."
    )


# -- index conversions through the library -----------------------------------


def owner_session(database: Database | None = None, session: Session | None = None) -> Session:
    """The session that performs conversions: the database's, the given one, or the current."""
    if database is not None:
        if session is not None and session is not database.session:
            raise ValueError("database and session disagree.")
        return database.session
    if session is not None:
        return session
    return current_session()


def mit_to_index(
    mit: MIT, *, database: Database | None = None, session: Session | None = None
) -> int:
    """The library index of a moment, through the library's calendar.

    Case moments map to their integer value without a native call; every
    other supported frequency goes through the year/period conversion.
    """
    if not isinstance(mit, MIT):
        raise TypeError("mit_to_index expects an MIT.")
    code = fame_frequency(mit.frequency)
    if isinstance(mit.frequency, Unit):
        if not -(2**63) <= mit.value < 2**63:
            raise DataValidationError("A case index must fit a signed 64-bit integer.")
        return mit.value
    year, period = mit_to_year_period(mit)
    _int32(year, "year")
    _int32(period, "period")
    owner = owner_session(database, session)
    with owner.operation("year_period_to_index") as native:
        return native.year_period_to_index(code, year, period)


def index_to_mit(
    index: int,
    code: Any,
    *,
    database: Database | None = None,
    session: Session | None = None,
) -> MIT:
    """The moment of a library index at a supported frequency code.

    Missing index sentinels are not calendar positions; classify them first.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        raise DataValidationError("An index must be an integer.")
    if not -(2**63) <= index < 2**63:
        raise DataValidationError("An index must fit a signed 64-bit integer.")
    frequency = tsecon_frequency(code)
    if isinstance(frequency, Unit):
        return MIT(frequency, index)
    owner = owner_session(database, session)
    with owner.operation("index_to_year_period") as native:
        year, period = native.index_to_year_period(fame_frequency(frequency), index)
    return year_period_to_mit(frequency, int(year), int(period))
