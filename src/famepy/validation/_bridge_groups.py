# SPDX-License-Identifier: MIT
"""Bridge validation groups: frequencies, value kinds and workspaces.

Expected values are independent fixtures computed from the public
TimeSeriesEconPy calendar, fixed value lists and the explicitly loaded
missing-value sentinels, never a readback of the library: every raw
expectation (values, bits, categories, first index, type and frequency) and
every cross-process manifest is built from the source fixture, and the raw
stored categories are asserted in addition to the lossy bridge values.
Calendar cases assert structural facts of the library's own index space
(inverse conversions, adjacency across leap days, weekends and year
boundaries, period counts per year) rather than an assumed epoch. Every
frequency anchor of the reference is exercised once; date-value/index
frequency combinations are covered by a small representative set.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import numpy as np

import famepy
from famepy import bridge
from famepy._constants import FREQUENCIES, FREQUENCY_NAMES
from famepy._data import classify_by_sentinel

from ._manifest import _read, manifest_object, verify_case_ids

if TYPE_CHECKING:
    from ._groups import Context


def _tsecon() -> Any:
    import tsecon

    return tsecon


# -- fixtures ------------------------------------------------------------------

CALENDAR_CODES: tuple[int, ...] = tuple(
    sorted(code for code in bridge.SUPPORTED_FREQUENCY_CODES if code != FREQUENCIES["case"])
)
SERIES_VALUES = np.array([1.0, np.nan, 2.5])


def _label(code: int) -> str:
    return FREQUENCY_NAMES[code]


def _ppy(frequency: Any) -> int:
    return int(_tsecon().ppy(frequency))


def anchor_moment(code: int) -> Any:
    """A fixed moment per frequency, chosen next to a leap day and a weekend."""
    ts = _tsecon()
    frequency = bridge.tsecon_frequency(code)
    if isinstance(frequency, ts.Daily):
        return ts.daily("2020-02-28")
    if isinstance(frequency, ts.BDaily):
        return ts.bdaily("2020-02-28")  # a Friday
    if isinstance(frequency, ts.Weekly):
        return ts.weekly("2020-02-28", frequency.end_day)
    return bridge.year_period_to_mit(frequency, 2020, 1)


def last_moment_of_2020(code: int) -> Any:
    ts = _tsecon()
    frequency = bridge.tsecon_frequency(code)
    if isinstance(frequency, ts.Daily):
        return ts.daily("2020-12-31")
    if isinstance(frequency, ts.BDaily):
        return ts.bdaily("2020-12-31")  # a Thursday
    if isinstance(frequency, ts.Weekly):
        last = ts.weekly("2020-12-31", frequency.end_day)
        return last if bridge.mit_to_year_period(last)[0] == 2020 else last - 1
    return bridge.year_period_to_mit(frequency, 2020, _ppy(frequency))


def periods_in_2020(code: int) -> int:
    ts = _tsecon()
    frequency = bridge.tsecon_frequency(code)
    if isinstance(frequency, ts.Daily):
        return 366
    if isinstance(frequency, ts.BDaily):
        return 262
    if isinstance(frequency, ts.Weekly):
        return bridge.mit_to_year_period(last_moment_of_2020(code))[1]
    return _ppy(frequency)


def date_value_combinations() -> list[tuple[str, Any]]:
    """Representative index-frequency / value-frequency combinations."""
    ts = _tsecon()
    monthly_of_daily = bridge.DateSeries(
        ts.mm(2020, 1), [ts.daily("2020-02-29"), None, ts.daily("2021-01-01")]
    )
    daily_of_quarterly = bridge.DateSeries(ts.daily("2020-02-28"), [ts.qq(2020, 1), ts.qq(2020, 4)])
    weekly_of_annual = bridge.DateSeries(
        ts.weekly("2020-02-28", 5), [ts.MIT.from_yp(ts.Yearly(6), 2020, 1)]
    )
    case_of_monthly = bridge.DateSeries(ts.MIT(ts.Unit(), 1), [ts.mm(2020, 1), ts.mm(2020, 2)])
    business_of_case = bridge.DateSeries(
        ts.bdaily("2020-02-28"), [ts.MIT(ts.Unit(), 7), ts.MIT(ts.Unit(), -3)]
    )
    return [
        ("dv_monthly_of_daily", monthly_of_daily),
        ("dv_daily_of_quarterly", daily_of_quarterly),
        ("dv_weekly_of_annual", weekly_of_annual),
        ("dv_case_of_monthly", case_of_monthly),
        ("dv_business_of_case", business_of_case),
    ]


# -- frequencies group -----------------------------------------------------------


def _frequency_case_ids() -> tuple[str, ...]:
    ids: list[str] = []
    for code in CALENDAR_CODES:
        label = _label(code)
        ids.extend(
            [
                f"inverse:{label}",
                f"adjacent:{label}",
                f"year_boundary:{label}",
                f"periods_in_year:{label}",
                f"series_round_trip:{label}",
                f"series_raw_categories:{label}",
                f"date_scalar_round_trip:{label}",
                f"date_scalar_raw:{label}",
            ]
        )
    for name, _ in date_value_combinations():
        ids.extend([f"date_values:{name}", f"date_values_raw:{name}"])
    return tuple(ids)


FREQUENCIES_REQUIRED = (
    "fixtures",
    "write_frequencies",
    "read_frequencies",
    *_frequency_case_ids(),
    "unsupported_frequency_refused",
    "case_index_passthrough",
    *verify_case_ids(
        "cross_process_frequencies",
        [f"f_{_label(code)}" for code in CALENDAR_CODES]
        + [f"d_{_label(code)}" for code in CALENDAR_CODES]
        + [name for name, _ in date_value_combinations()],
    ),
    "finalize",
)


def expected_precision_values(sentinels: Any) -> np.ndarray:
    """The fixture series as the library must store it: NaN written as NC."""
    values = SERIES_VALUES.copy()
    values[np.isnan(values)] = sentinels.precision_nc
    return values


def expected_date_indices(value: bridge.DateSeries, session: Any) -> np.ndarray:
    """Date observations as indices through the verified calendar conversion."""
    nc = session.sentinels.index_nc
    return np.array(
        [
            nc if item is None else bridge.mit_to_index(item, session=session)
            for item in value.values
        ],
        dtype=np.int64,
    )


def group_frequencies(ctx: Context) -> None:
    ts = _tsecon()
    session, r = ctx.session, ctx.recorder
    session.initialize()
    path = ctx.path("frequencies.db")

    def fixtures() -> Any:
        workspace = ts.Workspace()
        for code in CALENDAR_CODES:
            label = _label(code)
            workspace[f"f_{label}"] = ts.TSeries(anchor_moment(code), SERIES_VALUES.copy())
            workspace[f"d_{label}"] = anchor_moment(code)
        for name, value in date_value_combinations():
            workspace[name] = value
        return workspace

    workspace = r.check("fixtures", fixtures)
    if workspace is None:
        r.check("finalize", session.finalize)
        return

    # Calendar structure, one case per fact and frequency.
    for code in CALENDAR_CODES:
        label = _label(code)
        anchor = anchor_moment(code)

        def index(moment: Any) -> int:
            return bridge.mit_to_index(moment, session=session)

        def inverse(a: Any = anchor, c: int = code) -> int:
            return int(bridge.index_to_mit(index(a), c, session=session))

        def adjacent(a: Any = anchor) -> list[int]:
            return [index(a + 1) - index(a), index(a + 2) - index(a + 1)]

        last = last_moment_of_2020(code)

        def boundary(m: Any = last) -> int:
            return index(m + 1) - index(m)

        first_2020 = bridge.year_period_to_mit(bridge.tsecon_frequency(code), 2020, 1)
        first_2021 = bridge.year_period_to_mit(bridge.tsecon_frequency(code), 2021, 1)

        def count(a: Any = first_2020, b: Any = first_2021) -> int:
            return index(b) - index(a)

        r.expect(f"inverse:{label}", inverse, int(anchor))
        r.expect(f"adjacent:{label}", adjacent, [1, 1])
        r.expect(f"year_boundary:{label}", boundary, 1)
        r.expect(f"periods_in_year:{label}", count, periods_in_2020(code))
        if isinstance(bridge.tsecon_frequency(code), ts.Weekly):
            # What the library reports for the last week of the year (an
            # observation: the week-53 convention is reference-derived).
            year, period = bridge.mit_to_year_period(last)
            with session.operation("index_to_year_period") as native:
                r.fact(
                    f"library_last_week_2020:{label}",
                    list(native.index_to_year_period(code, index(last))),
                    note=f"python convention year:period {year}:{period}",
                )

    r.check(
        "write_frequencies",
        lambda: bridge.write_workspace(path, workspace, mode="create"),
    )

    sentinels = session.sentinels
    stored = expected_precision_values(sentinels)

    def read_all() -> None:
        back = bridge.read_workspace(path)
        with famepy.open_database(path, "readonly", session=session) as database:
            for code in CALENDAR_CODES:
                label = _label(code)
                series = back.get(f"f_{label}")
                expected = workspace[f"f_{label}"]
                r.equal(
                    f"series_round_trip:{label}",
                    None
                    if series is None
                    else [
                        int(series.firstdate),
                        bridge.fame_frequency(series.frequency),
                        series.values,
                    ],
                    [int(expected.firstdate), code, expected.values],
                )
                # Raw identity: the stored bits and missing categories, never
                # the NaN the bridge reads back.
                r.expect(
                    f"series_raw_categories:{label}",
                    partial(_raw_series_record, database, f"f_{label}", sentinels),
                    [
                        code,
                        bridge.mit_to_index(anchor_moment(code), session=session),
                        stored,
                        [0, 1, 0],
                    ],
                )
                r.equal(
                    f"date_scalar_round_trip:{label}",
                    _mit_record(back.get(f"d_{label}")),
                    _mit_record(workspace[f"d_{label}"]),
                )
                r.expect(
                    f"date_scalar_raw:{label}",
                    partial(_raw_scalar_record, database, f"d_{label}"),
                    [code, bridge.mit_to_index(anchor_moment(code), session=session)],
                )
            for name, value in date_value_combinations():
                r.equal(f"date_values:{name}", _dates_record(back.get(name)), _dates_record(value))
                r.expect(
                    f"date_values_raw:{name}",
                    partial(_raw_series_record, database, name, sentinels),
                    [
                        bridge.fame_frequency(value.frequency),
                        bridge.mit_to_index(value.firstdate, session=session),
                        expected_date_indices(value, session),
                        [0 if item is not None else 1 for item in value.values],
                    ],
                )

    r.check("read_frequencies", read_all)

    def unsupported() -> None:
        with famepy.open_database(path, "update", session=session) as database:
            famepy.write_object(
                database, "tenday", famepy.series("precision", "tenday", 5, np.zeros(2))
            )
            database.post()
        bridge.read_value(path, "tenday")

    r.expect_error(
        "unsupported_frequency_refused", unsupported, (bridge.UnsupportedFrequencyError,)
    )
    r.expect(
        "case_index_passthrough",
        lambda: [
            bridge.mit_to_index(ts.MIT(ts.Unit(), v), session=session) for v in (1, 0, -5, 2**40)
        ],
        [1, 0, -5, 2**40],
    )
    # Cross-process manifests come from the fixtures and the verified calendar
    # conversion, never from what was read back.
    objects = []
    for code in CALENDAR_CODES:
        label = _label(code)
        first = bridge.mit_to_index(anchor_moment(code), session=session)
        objects.append(
            manifest_object(
                f"f_{label}",
                "precision",
                stored,
                class_name="series",
                type_code=int(famepy.ObjectType.PRECISION),
                frequency=code,
                first_index=first,
            )
        )
        objects.append(
            manifest_object(f"d_{label}", "date", [first], class_name="scalar", type_code=code)
        )
    for name, value in date_value_combinations():
        objects.append(
            manifest_object(
                name,
                "date",
                expected_date_indices(value, session),
                class_name="series",
                type_code=bridge.fame_frequency(value.value_frequency),
                frequency=bridge.fame_frequency(value.frequency),
                first_index=bridge.mit_to_index(value.firstdate, session=session),
            )
        )
    ctx.verify_in_new_process(
        "cross_process_frequencies", {"database": str(path), "objects": objects}
    )
    r.check("finalize", session.finalize)


def _raw_series_record(database: Any, name: str, sentinels: Any) -> Any:
    raw = _read(database, name)
    codes = classify_by_sentinel(raw.values, raw.kind, sentinels).tolist()
    return [raw.frequency, raw.first_index, raw.values, codes]


def _raw_series_tail(database: Any, name: str, sentinels: Any) -> Any:
    return _raw_series_record(database, name, sentinels)[1:]


def _raw_scalar_record(database: Any, name: str) -> Any:
    raw = _read(database, name)
    return [raw.type_code, int(raw.value)]


def _mit_record(value: Any) -> Any:
    ts = _tsecon()
    if not isinstance(value, ts.MIT):
        return None
    return [int(value), bridge.fame_frequency(value.frequency)]


def _dates_record(value: Any) -> Any:
    if not isinstance(value, bridge.DateSeries):
        return None
    return [
        _mit_record(value.firstdate),
        bridge.fame_frequency(value.value_frequency),
        [None if item is None else int(item) for item in value.values],
    ]


# -- bridge value cases (run inside the bridge group) ---------------------------

BRIDGE_VALUE_CASES = (
    "write_kinds",
    "read_kinds",
    *[
        f"kind:{name}"
        for name in (
            "numeric_scalar",
            "integer_scalar",
            "boolean_scalar",
            "date_scalar",
            "string_scalar",
            "literal_brace_string",
            "namelist",
            "string_vector",
            "numeric_series",
            "boolean_series",
            "date_series",
            "string_series",
        )
    ],
    "write_missing_matrix",
    *[
        f"{prefix}:{kind}:{category}"
        for prefix in ("missing", "missing_raw")
        for kind in ("precision", "numeric", "boolean", "date", "string")
        for category in ("nc", "na", "nd")
    ],
    "kinds_raw_nan_as_nc",
    "missing_strict_refused",
    "boolean_missing_never_true",
    "write_empty_matrix",
    *[f"empty:{name}" for name in ("t1", "t2", "t3", "t4", "t5", "t6")],
    "empty_preserve_needs_firstdate",
)

_CATEGORY = {"nc": 1, "na": 2, "nd": 3}


def bridge_value_cases(ctx: Context, path: Any) -> None:
    """Every value kind, missing category and empty convention through the bridge."""
    ts = _tsecon()
    session, r = ctx.session, ctx.recorder
    s = session.sentinels
    r.permit(s.string_nc, s.string_na, s.string_nd)
    kinds = ts.Workspace(
        numeric_scalar=np.float32(1.5),
        integer_scalar=7,
        boolean_scalar=True,
        date_scalar=ts.qq(2021, 3),
        string_scalar="Hello World",
        literal_brace_string=bridge.Text("{not,a,list}"),
        namelist="{A,HELLO,B,WORLD}",
        string_vector=["qmazing", "qmazing", "p", "phantastic"],
        numeric_series=ts.TSeries(ts.mm(2020, 1), np.array([1.5, np.nan, -2.0], dtype=np.float32)),
        boolean_series=ts.TSeries(ts.qq(2020, 1), np.array([True, False, True])),
        date_series=bridge.DateSeries(ts.qq(2020, 1), [ts.yy(2022), ts.yy(2023)]),
        string_series=bridge.StringSeries(ts.mm(2020, 1), ["a", "bb", "ccc"]),
    )
    expected: dict[str, Any] = {
        "numeric_scalar": np.float32(1.5),
        "integer_scalar": 7.0,
        "boolean_scalar": True,
        "date_scalar": _mit_record(ts.qq(2021, 3)),
        "string_scalar": "Hello World",
        "literal_brace_string": "{not,a,list}",
        "namelist": ["A", "HELLO", "B", "WORLD"],
        "string_vector": [[1, FREQUENCIES["case"]], ["qmazing", "qmazing", "p", "phantastic"]],
        "numeric_series": [
            int(ts.mm(2020, 1)),
            np.array([1.5, np.nan, -2.0], dtype=np.float32),
        ],
        "boolean_series": [int(ts.qq(2020, 1)), [True, False, True]],
        "date_series": [
            _mit_record(ts.qq(2020, 1)),
            FREQUENCIES["annual_december"],
            [int(ts.yy(2022)), int(ts.yy(2023))],
        ],
        "string_series": [int(ts.mm(2020, 1)), ["a", "bb", "ccc"]],
    }

    def record(name: str, value: Any) -> Any:
        if isinstance(value, ts.MIT):
            return _mit_record(value)
        if isinstance(value, bridge.NameList):
            return list(value.members)
        if isinstance(value, bridge.DateSeries):
            return _dates_record(value)
        if isinstance(value, bridge.StringSeries):
            return [
                [int(value.firstdate), bridge.fame_frequency(value.frequency)]
                if name == "string_vector"
                else int(value.firstdate),
                list(value.values),
            ]
        if isinstance(value, ts.TSeries):
            return [
                int(value.firstdate),
                value.values if value.values.dtype.kind == "f" else value.values.tolist(),
            ]
        return value

    r.check("write_kinds", lambda: bridge.write_workspace(path, kinds, mode="update"))

    def read_kinds() -> None:
        back = bridge.read_workspace(path, *kinds.keys())
        for name in kinds:
            r.equal(f"kind:{name}", record(name, back.get(name)), expected[name])

    r.check("read_kinds", read_kinds)

    # Missing categories per kind, written raw and read through the bridge.
    first = ctx.first
    raw_missing: dict[str, Any] = {}
    for kind in ("precision", "numeric", "boolean", "date", "string"):
        for category, code in _CATEGORY.items():
            sentinel = famepy._data.sentinel_value(kind, code, s)
            name = f"m_{kind}_{category}"
            if kind == "string":
                values: Any = [b"x", sentinel, b"y"]
            elif kind == "date":
                values = np.array([first, sentinel, first], dtype=np.int64)
            elif kind == "boolean":
                values = np.array([1, sentinel, 0], dtype=np.int32)
            else:
                dtype = np.float64 if kind == "precision" else np.float32
                values = np.array([1.0, sentinel, 2.0], dtype=dtype)
            raw_missing[name] = famepy.series(
                kind, "monthly", first, values, date_frequency="monthly" if kind == "date" else None
            )

    def write_missing() -> None:
        with famepy.open_database(path, "update", session=session) as database:
            for name, raw in raw_missing.items():
                famepy.write_object(database, name, raw, replace=True)
            database.post()

    r.check("write_missing_matrix", write_missing)
    with famepy.open_database(path, "readonly", session=session) as database:
        for name, raw in raw_missing.items():
            kind_name, category_name = name[2:].rsplit("_", 1)
            code = _CATEGORY[category_name]
            expected_values: Any = raw.values if raw.kind != "string" else list(raw.values)
            r.expect(
                f"missing_raw:{kind_name}:{category_name}",
                partial(_raw_series_tail, database, name, s),
                [first, expected_values, [0, code, 0]],
            )
        # The numeric kind written through the bridge stores NC bits, not NaN.
        numeric_expected = np.array([1.5, s.numeric_nc, -2.0], dtype=np.float32)
        r.expect(
            "kinds_raw_nan_as_nc",
            lambda: _raw_series_record(database, "numeric_series", s)[2:],
            [numeric_expected, [0, 1, 0]],
        )
    for kind in ("precision", "numeric", "boolean", "date", "string"):
        for category in _CATEGORY:
            name = f"m_{kind}_{category}"

            def read(name: str = name, kind: str = kind) -> Any:
                value = bridge.read_value(path, name)
                if kind in ("precision", "numeric"):
                    return [bool(np.isnan(value.values[1])), float(value.values[0])]
                if kind == "date":
                    return [value.values[1] is None, int(value.values[0])]
                return [value.values[1] is None, value.values[0]]

            if kind == "boolean":
                r.expect_error(f"missing:{kind}:{category}", read, (bridge.MissingValueError,))
            elif kind == "date":
                r.expect(f"missing:{kind}:{category}", read, [True, int(ts.mm(2020, 1))])
            elif kind == "string":
                r.expect(f"missing:{kind}:{category}", read, [True, "x"])
            else:
                r.expect(f"missing:{kind}:{category}", read, [True, 1.0])
    r.expect_error(
        "missing_strict_refused",
        lambda: bridge.read_value(path, "m_precision_na", missing="strict"),
        (bridge.MissingValueError,),
    )

    def never_true() -> Any:
        with famepy.open_database(path, "readonly", session=session) as database:
            raw = _read(database, "m_boolean_nc")
            return classify_by_sentinel(raw.values, "boolean", s).tolist()

    r.expect("boolean_missing_never_true", never_true, [0, 1, 0])

    # The reference's empty-series cases under the reference convention.
    empties = ts.Workspace(
        t1=ts.TSeries(ts.qq(1995, 1), np.empty(0)),
        t2=ts.TSeries(ts.qq(1993, 3), np.empty(0, dtype=np.float32)),
        t3=ts.TSeries(ts.qq(1996, 2), np.empty(0, dtype=np.bool_)),
        t4=ts.TSeries(ts.qq(1996, 2), np.array([True, False, True])),
        t5=bridge.DateSeries(ts.qq(1998, 3), (), ts.Yearly(12)),
        t6=bridge.DateSeries(ts.qq(1997, 1), [ts.yy(2022), ts.yy(2023)]),
    )

    def write_empties() -> None:
        bridge.write_workspace(path, empties, mode="update", empty="reference")
        bridge.write_value(path, "t0", ts.TSeries(ts.qq(1995, 1), np.empty(0)), mode="update")

    r.check("write_empty_matrix", write_empties)
    expected_empty = {
        "t1": [int(ts.qq(1995, 1)), 0, "float64"],
        "t2": [int(ts.qq(1993, 3)), 0, "float32"],
        "t3": [int(ts.qq(1996, 2)), 0, "bool"],
        "t4": [int(ts.qq(1996, 2)), 3, "bool"],
        "t5": [int(ts.qq(1998, 3)), 0, FREQUENCIES["annual_december"]],
        "t6": [int(ts.qq(1997, 1)), 2, FREQUENCIES["annual_december"]],
    }
    for name in empties:

        def read_empty(name: str = name) -> Any:
            value = bridge.read_value(path, name, empty="reference")
            if isinstance(value, bridge.DateSeries):
                return [
                    int(value.firstdate),
                    len(value),
                    bridge.fame_frequency(value.value_frequency),
                ]
            return [int(value.firstdate), len(value), str(value.values.dtype)]

        r.expect(f"empty:{name}", read_empty, expected_empty[name])
    r.expect_error(
        "empty_preserve_needs_firstdate",
        lambda: bridge.read_value(path, "t0"),
        (bridge.EmptySeriesError,),
    )


# -- workspace group -------------------------------------------------------------

WORKSPACE_REQUIRED = (
    "write_reference",
    "listing_count",
    "read_all",
    "read_all_values",
    "b_raw_categories",
    "read_explicit_order",
    "read_wildcard",
    "read_wildcard_prefix",
    "read_collect",
    "read_nested_collect",
    "read_prefix_all",
    "collision_refused",
    "branch_collision_refused",
    "absent_name_status",
    "report_failures",
    "report_raw_fallback",
    "write_report_complete",
    "write_multiple_inputs",
    "nan_written_as_nc_series",
    *verify_case_ids("cross_process_workspace", ["A", "B", "C_N_S", "S_P"]),
    "finalize",
)


def reference_b_values() -> np.ndarray:
    """The quarterly fixture: ten values with one missing observation."""
    values = np.arange(10, dtype=np.float64)
    values[3] = np.nan
    return values


def reference_workspace() -> Any:
    ts = _tsecon()
    return ts.Workspace(
        a=1,
        b=ts.TSeries(ts.qq(2020, 1), reference_b_values()),
        s=ts.MVTSeries(ts.mm(2020, 1), ("q", "p"), np.arange(48, dtype=np.float64).reshape(24, 2)),
        c=ts.Workspace(alpha=0.1, beta=0.8, n=ts.Workspace(s="Hello World")),
    )


def group_workspace(ctx: Context) -> None:
    ts = _tsecon()
    session, r = ctx.session, ctx.recorder
    session.initialize()
    path = ctx.path("workspace.db")
    reference = reference_workspace()

    r.check("write_reference", lambda: bridge.write_workspace(path, reference, mode="create"))
    r.expect("listing_count", lambda: len(_upper_names(path, session)), 7)

    def keys(*names: Any, **options: Any) -> Any:
        return lambda: list(bridge.read_workspace(path, *names, **options))

    r.expect("read_all", keys(), ["a", "b", "c_alpha", "c_beta", "c_n_s", "s_p", "s_q"])

    def all_values() -> Any:
        w = bridge.read_workspace(path)
        return [
            w.a,
            int(w.b.firstdate),
            w.b.values,
            w.c_alpha,
            w.c_n_s,
            int(w.s_p.firstdate),
            w.s_p.values,
        ]

    r.expect(
        "read_all_values",
        all_values,
        [
            1.0,
            int(ts.qq(2020, 1)),
            reference_b_values(),
            0.1,
            "Hello World",
            int(ts.mm(2020, 1)),
            np.arange(48, dtype=np.float64).reshape(24, 2)[:, 1].copy(),
        ],
    )
    stored_b = reference_b_values()
    stored_b[3] = session.sentinels.precision_nc
    first_b = bridge.mit_to_index(ts.qq(2020, 1), session=session)

    def b_raw() -> Any:
        with famepy.open_database(path, "readonly", session=session) as database:
            return _raw_series_record(database, "B", session.sentinels)

    r.expect(
        "b_raw_categories",
        b_raw,
        [FREQUENCIES["quarterly_december"], first_b, stored_b, [0, 0, 0, 1, 0, 0, 0, 0, 0, 0]],
    )
    r.expect("read_explicit_order", keys("b", "A", "a"), ["b", "a"])
    r.expect("read_wildcard", keys("s?"), ["s_p", "s_q"])
    r.expect("read_wildcard_prefix", keys("s?", prefix="s"), ["p", "q"])

    def collect_one() -> Any:
        w = bridge.read_workspace(path, "c?", collect="c")
        return [list(w), list(w.c)]

    r.expect("read_collect", collect_one, [["c"], ["alpha", "beta", "n_s"]])

    def nested() -> Any:
        w = bridge.read_workspace(path, collect=[("c", ["n"]), "s"])
        return [list(w), list(w.c), list(w.c.n), list(w.s), w.c.n.s]

    r.expect(
        "read_nested_collect",
        nested,
        [["a", "b", "c", "s"], ["alpha", "beta", "n"], ["s"], ["p", "q"], "Hello World"],
    )
    r.expect("read_prefix_all", keys(prefix="c"), ["a", "b", "alpha", "beta", "n_s", "s_p", "s_q"])

    def add_colliders() -> None:
        bridge.write_workspace(path, ts.Workspace(c_x=1.0, x=2.0, c=3.0), mode="update")

    r.check("write_colliders", add_colliders)
    r.expect_error(
        "collision_refused",
        lambda: bridge.read_workspace(path, "c_x", "x", prefix="c"),
        (bridge.NameCollisionError,),
    )
    r.expect_error(
        "branch_collision_refused",
        lambda: bridge.read_workspace(path, "c", "c_alpha", collect="c"),
        (bridge.NameCollisionError,),
    )
    r.expect_error(
        "absent_name_status",
        lambda: bridge.read_workspace(path, "no_such_object"),
        (famepy.FameError,),
    )

    def add_unconvertible() -> None:
        with famepy.open_database(path, "update", session=session) as database:
            famepy.write_object(
                database, "u_tenday", famepy.series("precision", "tenday", 3, np.zeros(2))
            )
            famepy.write_object(
                database, "u_bool", famepy.scalar("boolean", session.sentinels.boolean_nc)
            )
            database.post()

    r.check("write_unconvertible", add_unconvertible)

    def report() -> Any:
        rep = bridge.read_workspace_report(path, "u?")
        return [list(rep.workspace), [str(f) for f in rep.failures], rep.complete]

    r.expect(
        "report_failures",
        report,
        [[], ["u_bool: MissingValueError", "u_tenday: UnsupportedFrequencyError"], False],
    )

    def fallback() -> Any:
        rep = bridge.read_workspace_report(path, "u?", raw_fallback=True)
        return [
            list(rep.workspace),
            list(rep.raw),
            rep.complete,
            rep.workspace.u_tenday.frequency,
            type(rep.workspace.u_bool).__name__,
        ]

    r.expect(
        "report_raw_fallback",
        fallback,
        [["u_bool", "u_tenday"], ["U_BOOL", "U_TENDAY"], True, FREQUENCIES["tenday"], "RawScalar"],
    )

    def write_report() -> Any:
        rep = bridge.write_workspace_report(
            path, ts.Workspace(r1=1.0, r2=ts.TSeries(ts.mm(2021, 1), [np.nan])), mode="update"
        )
        return [list(rep.written), len(rep.failures), rep.posted, rep.complete]

    r.expect("write_report_complete", write_report, [["r1", "r2"], 0, True, True])

    def multiple() -> Any:
        m = ts.MVTSeries(ts.mm(2020, 1), ("x", "y"), np.ones((2, 2)))
        bridge.write_workspace(
            path, ts.Workspace(a=1.0), m, {"d": ts.qq(2020, 1)}, mode="update", prefix="in"
        )
        return list(bridge.read_workspace(path, "in_?"))

    r.expect("write_multiple_inputs", multiple, ["in_a", "in_d", "in_x", "in_y"])

    def nan_codes() -> Any:
        with famepy.open_database(path, "readonly", session=session) as database:
            raw = _read(database, "r2")
            return classify_by_sentinel(raw.values, "precision", session.sentinels).tolist()

    r.expect("nan_written_as_nc_series", nan_codes, [1])

    # Manifests from the source fixture, not from the database.
    objects = [
        manifest_object(
            "A",
            "precision",
            [np.float64(1.0)],
            class_name="scalar",
            type_code=int(famepy.ObjectType.PRECISION),
        ),
        manifest_object(
            "B",
            "precision",
            stored_b,
            class_name="series",
            type_code=int(famepy.ObjectType.PRECISION),
            frequency=FREQUENCIES["quarterly_december"],
            first_index=first_b,
        ),
        manifest_object(
            "C_N_S",
            "string",
            [b"Hello World"],
            class_name="scalar",
            type_code=int(famepy.ObjectType.STRING),
        ),
        manifest_object(
            "S_P",
            "precision",
            np.arange(48, dtype=np.float64).reshape(24, 2)[:, 1].copy(),
            class_name="series",
            type_code=int(famepy.ObjectType.PRECISION),
            frequency=FREQUENCIES["monthly"],
            first_index=bridge.mit_to_index(ts.mm(2020, 1), session=session),
        ),
    ]
    ctx.verify_in_new_process(
        "cross_process_workspace", {"database": str(path), "objects": objects}
    )
    r.check("finalize", session.finalize)


def _upper_names(path: Any, session: Any) -> set[str]:
    with famepy.open_database(path, "readonly", session=session) as database:
        return {info.name_text.upper() for info in famepy.list_objects(database)}
