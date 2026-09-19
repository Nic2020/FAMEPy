# SPDX-License-Identifier: MIT
"""Frequency maps, year/period conventions and index conversions of the bridge."""

import datetime

import pytest
from fake_native import CALENDAR_OFFSET
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
    mm,
    qq,
    weekly,
    yy,
)

import famepy
from famepy import DataValidationError, bridge
from famepy._constants import FREQUENCIES, FREQUENCY_CASE, FREQUENCY_MONTHLY

ANCHORED = [
    *[
        (Weekly(day), f"weekly_{name}")
        for day, name in enumerate(
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), start=1
        )
    ],
    (Quarterly(1), "quarterly_october"),
    (Quarterly(2), "quarterly_november"),
    (Quarterly(3), "quarterly_december"),
    *[
        (HalfYearly(a), f"semiannual_{m}")
        for a, m in enumerate(
            ("july", "august", "september", "october", "november", "december"), start=1
        )
    ],
    *[
        (Yearly(m), f"annual_{name}")
        for m, name in enumerate(
            (
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
            ),
            start=1,
        )
    ],
    (Monthly(), "monthly"),
    (Daily(), "daily"),
    (BDaily(), "business"),
    (Unit(), "case"),
]
UNSUPPORTED = [
    name
    for name in FREQUENCIES
    if name not in {label for _, label in ANCHORED} and name != "undefined"
]


def test_every_reference_anchor_maps_both_ways():
    assert len(ANCHORED) == 7 + 3 + 6 + 12 + 4
    for frequency, name in ANCHORED:
        code = FREQUENCIES[name]
        assert bridge.fame_frequency(frequency) == code
        assert bridge.tsecon_frequency(code) is frequency
        assert bridge.tsecon_frequency(name) is frequency
        assert bridge.is_supported_frequency(code)
    assert bridge.SUPPORTED_FREQUENCY_CODES == {FREQUENCIES[n] for _, n in ANCHORED}


def test_unsupported_families_are_refused_not_remapped():
    assert {"tenday", "twicemonthly", "ypp", "ppy", "secondly", "weekly_pattern"} <= set(
        UNSUPPORTED
    )
    assert len(UNSUPPORTED) == 25
    for name in UNSUPPORTED:
        with pytest.raises(bridge.UnsupportedFrequencyError, match=name):
            bridge.tsecon_frequency(FREQUENCIES[name])
        assert not bridge.is_supported_frequency(FREQUENCIES[name])
    with pytest.raises(bridge.UnsupportedFrequencyError):
        bridge.tsecon_frequency(0)
    with pytest.raises(bridge.UnsupportedFrequencyError):
        bridge.tsecon_frequency("no such frequency")
    with pytest.raises(TypeError):
        bridge.fame_frequency(Monthly)  # a class, not an instance
    with pytest.raises(TypeError):
        bridge.fame_frequency("monthly")


# -- year/period conventions ------------------------------------------------


def test_yp_frequencies_decompose_directly():
    assert bridge.mit_to_year_period(mm(2020, 1)) == (2020, 1)
    assert bridge.mit_to_year_period(qq(2019, 4)) == (2019, 4)
    assert bridge.mit_to_year_period(MIT.from_yp(Quarterly(1), 2020, 1)) == (2020, 1)
    assert bridge.mit_to_year_period(MIT.from_yp(HalfYearly(2), 2020, 2)) == (2020, 2)
    assert bridge.mit_to_year_period(yy(1999)) == (1999, 1)
    assert bridge.year_period_to_mit(Quarterly(2), 2020, 3) == MIT.from_yp(Quarterly(2), 2020, 3)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Quarterly(), 2020, 5)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Monthly(), 2020, 0)


def test_fiscal_anchor_boundaries_follow_tsecon_calendar():
    # Quarterly(1): quarters end in January, April, July, October. The first
    # quarter of fiscal 2020 ends 31 January 2020 (the year is the ending year).
    from tsecon import mit_to_date

    q1 = bridge.year_period_to_mit(Quarterly(1), 2020, 1)
    assert mit_to_date(q1) == datetime.date(2020, 1, 31)
    assert mit_to_date(q1, ref="begin") == datetime.date(2019, 11, 1)
    y = bridge.year_period_to_mit(Yearly(3), 2021, 1)
    assert mit_to_date(y) == datetime.date(2021, 3, 31)
    assert mit_to_date(y, ref="begin") == datetime.date(2020, 4, 1)
    h = bridge.year_period_to_mit(HalfYearly(1), 2020, 2)
    assert mit_to_date(h) == datetime.date(2020, 7, 31)
    assert bridge.mit_to_year_period(h) == (2020, 2)


def test_daily_convention_with_leap_days_and_year_boundaries():
    assert bridge.mit_to_year_period(daily("2020-02-29")) == (2020, 60)
    assert bridge.mit_to_year_period(daily("2020-12-31")) == (2020, 366)
    assert bridge.mit_to_year_period(daily("2021-12-31")) == (2021, 365)
    assert bridge.year_period_to_mit(Daily(), 2020, 60) == daily("2020-02-29")
    assert bridge.year_period_to_mit(Daily(), 2021, 60) == daily("2021-03-01")
    assert bridge.year_period_to_mit(Daily(), 2021, 1) == daily("2021-01-01")
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Daily(), 2021, 366)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Daily(), 2020, 367)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Daily(), 2020, 0)


def test_business_convention_skips_weekends():
    # 2022 starts on a Saturday: the first business day is Monday 3 January.
    assert bridge.mit_to_year_period(bdaily("2022-01-03")) == (2022, 1)
    assert bridge.year_period_to_mit(BDaily(), 2022, 1) == bdaily("2022-01-03")
    assert bridge.mit_to_year_period(bdaily("2022-01-10")) == (2022, 6)
    assert bridge.year_period_to_mit(BDaily(), 2022, 6) == bdaily("2022-01-10")
    # 2020 (leap, starting on a Wednesday) has 262 business days.
    assert bridge.mit_to_year_period(bdaily("2020-12-31")) == (2020, 262)
    assert bridge.year_period_to_mit(BDaily(), 2020, 262) == bdaily("2020-12-31")
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(BDaily(), 2020, 263)
    assert bridge.mit_to_year_period(bdaily("2021-01-01")) == (2021, 1)


@pytest.mark.parametrize("end_day", range(1, 8))
def test_weekly_convention_week_numbers_and_53(end_day):
    # Week p is the week ending in days 7(p-1)+1 .. 7p of the year.
    first_end = next(
        datetime.date(2021, 1, d)
        for d in range(1, 8)
        if datetime.date(2021, 1, d).isoweekday() == end_day
    )
    w1 = weekly(first_end, end_day)
    assert bridge.mit_to_year_period(w1) == (2021, 1)
    assert bridge.year_period_to_mit(Weekly(end_day), 2021, 1) == w1
    w2 = w1 + 1
    assert bridge.mit_to_year_period(w2) == (2021, 2)
    assert bridge.year_period_to_mit(Weekly(end_day), 2021, 2) == w2
    # A year has week 53 exactly when a week ends on its last one (two, in a
    # leap year) days.
    last = weekly(datetime.date(2021, 12, 31), end_day)
    year, period = bridge.mit_to_year_period(last)
    if year == 2021:
        assert period == 53 if datetime.date(2021, 12, 31).isoweekday() == end_day else 52
        assert bridge.year_period_to_mit(Weekly(end_day), 2021, period) == last
    else:
        assert (year, period) == (2022, 1)
        with pytest.raises(DataValidationError):
            bridge.year_period_to_mit(Weekly(end_day), 2021, 53)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Weekly(end_day), 2021, 54)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Weekly(end_day), 2021, 0)


def test_week_53_exists_for_specific_years():
    # 31 December 2023 is a Sunday: week 53 of 2023 for Sunday-ending weeks.
    assert bridge.mit_to_year_period(weekly("2023-12-31", 7)) == (2023, 53)
    assert bridge.year_period_to_mit(Weekly(7), 2023, 53) == weekly("2023-12-31", 7)
    # 2020 (leap) ends on a Thursday: day 366 is Thursday, day 365 Wednesday.
    assert bridge.mit_to_year_period(weekly("2020-12-31", 4)) == (2020, 53)
    assert bridge.mit_to_year_period(weekly("2020-12-30", 3)) == (2020, 53)
    assert bridge.mit_to_year_period(weekly("2020-12-29", 2)) == (2020, 52)
    # The week ending Sunday 3 January 2021 is week 1 of 2021, not week 53 of 2020.
    assert bridge.mit_to_year_period(weekly("2020-12-31", 7)) == (2021, 1)


def test_case_moments_have_no_year_period():
    with pytest.raises(DataValidationError):
        bridge.mit_to_year_period(MIT(Unit(), 3))
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Unit(), 0, 3)
    with pytest.raises(TypeError):
        bridge.mit_to_year_period(3)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Daily(), 2020, True)
    with pytest.raises(DataValidationError):
        bridge.year_period_to_mit(Daily(), 0, 1)
    assert bridge.year_period_to_mit(Monthly(), 0, 1) == mm(0, 1)


# -- index conversions through the library -------------------------------------


@pytest.mark.parametrize(("frequency", "name"), [a for a in ANCHORED if a[1] != "case"])
def test_index_round_trip_uses_the_library(session, frequency, name):
    if isinstance(frequency, Weekly):
        moment = weekly("2020-06-17", frequency.end_day)
    elif isinstance(frequency, Daily):
        moment = daily("2020-02-29")
    elif isinstance(frequency, BDaily):
        moment = bdaily("2020-03-02")
    else:
        moment = bridge.year_period_to_mit(frequency, 2020, 1)
    fake = session._native.fake
    fake.calls.clear()
    index = bridge.mit_to_index(moment, session=session)
    assert "fame_year_period_to_index" in fake.calls
    if name != "monthly":
        assert index >= CALENDAR_OFFSET  # not the tsecon value
    assert bridge.index_to_mit(index, FREQUENCIES[name], session=session) == moment
    assert bridge.index_to_mit(index, name, session=session) == moment
    assert "fame_index_to_year_period" in fake.calls


def test_case_indices_never_call_the_library(session):
    fake = session._native.fake
    fake.calls.clear()
    for value in (1, 0, -5, 2**62, -(2**63)):
        assert bridge.mit_to_index(MIT(Unit(), value), session=session) == value
        assert bridge.index_to_mit(value, FREQUENCY_CASE, session=session) == MIT(Unit(), value)
    assert fake.calls == []
    with pytest.raises(DataValidationError):
        bridge.mit_to_index(MIT(Unit(), 2**63), session=session)


def test_index_conversion_argument_checks(db):
    assert bridge.mit_to_index(mm(2021, 7), database=db) == 2021 * 12 + 6
    with pytest.raises(bridge.UnsupportedFrequencyError):
        bridge.index_to_mit(5, FREQUENCIES["tenday"], database=db)
    with pytest.raises(DataValidationError):
        bridge.index_to_mit(True, FREQUENCY_MONTHLY, database=db)
    with pytest.raises(DataValidationError):
        bridge.index_to_mit(2**63, FREQUENCIES["daily"], database=db)
    with pytest.raises(TypeError):
        bridge.mit_to_index(3, database=db)
    other = famepy._runtime.Session(native=db.session._native)
    with pytest.raises(ValueError):
        bridge.mit_to_index(mm(2021, 7), database=db, session=other)
    with pytest.raises(DataValidationError):
        bridge.mit_to_index(MIT(Daily(), 2**40), session=db.session)


def test_library_calendar_errors_surface(session):
    with pytest.raises(famepy.FameError) as info:
        bridge.mit_to_index(daily("0099-06-01"), session=session)
    assert info.value.status == 914
    # A library answer outside the year (the fake shifted) is refused, never normalized.
    session._native.fake.shift_periods = True
    with pytest.raises(DataValidationError):
        bridge.index_to_mit(
            bridge.mit_to_index(daily("2021-12-31"), session=session),
            FREQUENCIES["daily"],
            session=session,
        )


def test_current_session_is_the_default(session):
    assert bridge.mit_to_index(mm(2020, 1)) == 2020 * 12


# -- Regression: the case fast path shares the 64-bit range check -------------


@pytest.mark.parametrize("form", [FREQUENCY_CASE, "case", "CASE"])
def test_case_index_bounds_apply_before_the_fast_path(session, form):
    fake = session._native.fake
    fake.calls.clear()
    low, high = -(2**63), 2**63 - 1
    for value in (low, high, 0, -1, 1):
        moment = bridge.index_to_mit(value, form, session=session)
        assert moment == MIT(Unit(), value)
        assert bridge.mit_to_index(moment, session=session) == value
    for value in (low - 1, high + 1, 2**100, -(2**100)):
        with pytest.raises(DataValidationError):
            bridge.index_to_mit(value, form, session=session)
        with pytest.raises(DataValidationError):
            bridge.mit_to_index(MIT(Unit(), value), session=session)
    with pytest.raises(DataValidationError):
        bridge.index_to_mit(True, form, session=session)
    with pytest.raises(DataValidationError):
        bridge.index_to_mit(1.0, form, session=session)
    assert fake.calls == []
    with pytest.raises(DataValidationError):
        bridge.index_to_mit(2**63, "daily", session=session)
    assert fake.calls == []
