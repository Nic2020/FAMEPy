# SPDX-License-Identifier: MIT
"""Value conversions: every kind, missing category, empty convention and carrier."""

import math

import numpy as np
import pytest
from canonical import read, scalar_object, series_object, value, write
from fake_native import FINITE_SENTINELS, SENTINELS, make_fake
from tsecon import MIT, BDaily, Daily, Monthly, Quarterly, TSeries, Unit, Weekly, daily, mm, qq

import famepy
from famepy import DataValidationError, TextEncodingError, bridge
from famepy._constants import FREQUENCY_CASE, FREQUENCY_MONTHLY
from famepy.bridge import DateSeries, NameList, StringSeries, Text


def _codes(db, name):
    raw = read(db, name)
    return famepy.classify_by_sentinel(raw.data, raw.kind, db.session.sentinels).tolist()


# -- scalars ------------------------------------------------------------------


def test_scalar_kinds_round_trip(db):
    cases = {
        "p": (2.5, "precision", 2.5),
        "pi": (7, "precision", 7.0),
        "n": (np.float32(1.5), "numeric", np.float32(1.5)),
        "b": (True, "boolean", True),
        "bf": (np.bool_(False), "boolean", False),
        "d": (qq(2021, 3), "date", qq(2021, 3)),
        "dd": (daily("2020-02-29"), "date", daily("2020-02-29")),
        "s": ("hello", "string", "hello"),
        "sb": (b"bytes", "string", "bytes"),
        "t": (Text("{not,a,list}"), "string", "{not,a,list}"),
        "nl": ("{a,b}", "namelist", NameList(["A", "B"])),
        "nl2": (NameList(["x", "y"]), "namelist", NameList(["X", "Y"])),
        "nle": ("{}", "namelist", NameList()),
    }
    for name, (item, kind, _) in cases.items():
        assert famepy.refame(name, item, database=db).kind == kind
        write(db, name, item)
        assert famepy.quick_info(db, name).kind == kind
    for name, (_, _kind, expected) in cases.items():
        back = value(db, name)
        assert back == expected, name
        assert type(back) is type(expected), name
        assert value(db, name) == expected
    assert famepy.quick_info(db, "d").date_frequency == 162
    assert read(db, "nl").data == b"{A,B}"


# -- the case frequency indexes series but is never a date value ---------------


def _case_value_attempts(target, **options):
    case = MIT(Unit(), 4)
    return {
        "scalar": lambda: write(target, "cd", case, **options),
        "write_scalar": lambda: write(target, "cd", case, **options),
        "series": lambda: write(
            target, "cd", DateSeries(daily("2020-02-28"), [case, MIT(Unit(), -3)]), **options
        ),
        "series_with_missing": lambda: write(
            target, "cd", DateSeries(mm(2020, 1), [None, case]), **options
        ),
        "empty_series": lambda: write(
            target, "cd", DateSeries(qq(2020, 1), (), Unit()), empty="reference", **options
        ),
        "all_missing_series": lambda: write(
            target, "cd", DateSeries(qq(2020, 1), [None, None], Unit()), **options
        ),
        "workspace": lambda: famepy.writefame(target, {"cd": case}, **options),
        "tseries_helper": lambda: write(target, "cd", DateSeries(qq(2020, 1), [case]), **options),
    }


def test_case_moments_are_refused_as_date_values_before_any_native_call(db):
    fake = db.session._native.fake
    write(db, "kept", 1.0)
    fake.calls.clear()
    for label, attempt in _case_value_attempts(db).items():
        with pytest.raises(DataValidationError, match="case frequency"):
            attempt()
        assert fake.calls == [], label
    with pytest.raises(DataValidationError, match="case frequency"):
        famepy.refame("cd", MIT(Unit(), 4), database=db)
    with pytest.raises(DataValidationError):
        famepy.refame("cd", MIT(Unit(), 4), session=db.session)
    with pytest.raises(DataValidationError):
        bridge.validate_value(MIT(Unit(), 4))
    assert fake.handles[db.key].objects.keys() == {"KEPT"}
    # A case-indexed series of calendar dates stays valid.
    write(db, "ok", DateSeries(MIT(Unit(), 1), [qq(2020, 1), None]))
    assert value(db, "ok") == DateSeries(MIT(Unit(), 1), [qq(2020, 1), None])
    assert famepy.quick_info(db, "ok").frequency == FREQUENCY_CASE
    assert famepy.quick_info(db, "ok").date_frequency == 162


@pytest.mark.parametrize("mode", ["overwrite", "update", "create"])
def test_case_moment_path_writes_leave_the_destination_untouched(session, tmp_path, mode):
    path = tmp_path / "kept.db"
    write(path, "kept", 42.0, mode="create")
    before = path.read_bytes()
    fake = session._native.fake
    for label, attempt in _case_value_attempts(path, mode=mode).items():
        fake.calls.clear()
        with pytest.raises(DataValidationError, match="case frequency"):
            attempt()
        assert fake.calls == [] and path.read_bytes() == before, label
    fake.calls.clear()
    report = bridge.writefame_report(path, {"cd": MIT(Unit(), 4)}, mode=mode)
    assert report.written == () and not report.posted
    assert [f.error_type for f in report.failures] == ["DataValidationError"]
    assert fake.calls == [] and path.read_bytes() == before
    assert value(path, "kept") == 42.0


def test_date_series_carrier_refuses_the_case_value_frequency():
    case = MIT(Unit(), 4)
    with pytest.raises(DataValidationError, match="case frequency"):
        DateSeries(qq(2020, 1), [case])
    with pytest.raises(DataValidationError, match="case frequency"):
        DateSeries(qq(2020, 1), [None, case, None])
    with pytest.raises(DataValidationError, match="case frequency"):
        DateSeries(qq(2020, 1), (), Unit())
    with pytest.raises(DataValidationError, match="case frequency"):
        DateSeries(qq(2020, 1), [None], value_frequency=Unit())
    # Mixed frequencies are still the first error when a calendar value precedes.
    with pytest.raises(DataValidationError):
        DateSeries(qq(2020, 1), [qq(2020, 1), case])


def test_raw_layer_refuses_the_case_frequency_as_a_date_type(db):
    fake = db.session._native.fake
    fake.calls.clear()
    with pytest.raises(DataValidationError, match="case frequency"):
        scalar_object("cd", "date", 4, date_frequency="case")
    with pytest.raises(DataValidationError, match="case frequency"):
        scalar_object("cd", "date", 4, date_frequency=FREQUENCY_CASE)
    with pytest.raises(DataValidationError, match="case frequency"):
        series_object(
            "cd", "date", "monthly", 5, np.array([4], dtype=np.int64), date_frequency="case"
        )
    with pytest.raises(DataValidationError, match="case frequency"):
        famepy._data.RawSeries(
            "date", FREQUENCY_MONTHLY, 5, np.empty(0, dtype=np.int64), FREQUENCY_CASE
        )
    with pytest.raises(ValueError, match="case frequency"):
        famepy._constants.type_code("case")
    with pytest.raises(ValueError, match="case frequency"):
        famepy._constants.type_code(FREQUENCY_CASE)
    assert famepy._constants.type_code("monthly") == FREQUENCY_MONTHLY
    assert not famepy._constants.is_date_type(FREQUENCY_CASE)
    assert famepy._constants.is_date_type(FREQUENCY_MONTHLY)
    assert fake.calls == []
    # A case-indexed raw date series with a calendar value type is valid.
    raw = series_object(
        "cdates", "date", "case", 1, np.array([5, 6], dtype=np.int64), date_frequency="monthly"
    )
    famepy.do_write(raw, db)
    assert read(db, "cdates").frequency == FREQUENCY_CASE


def test_fake_models_the_library_type_boundary_for_the_case_type(db):
    """The independent model refuses type 232 with the library's status, not another error."""
    from famepy._errors import HBOBJT, HLIError

    fake = db.session._native.fake
    with db.operation("new object") as native, pytest.raises(HLIError) as info:
        native.new_object(db.key, b"CASE_TYPED", 1, 9, FREQUENCY_CASE, 1, 0)
    assert info.value.status == HBOBJT == 16
    assert "CASE_TYPED" not in fake.handles[db.key].objects


def test_integer_scalars_are_exact_precision_not_float32(db):
    # Deliberate difference: the reference promotes integers to float32 numeric.
    write(db, "i", 2**53)
    assert famepy.quick_info(db, "i").kind == "precision"
    assert value(db, "i") == float(2**53)
    with pytest.raises(DataValidationError):
        write(db, "j", 2**53 + 1)
    with pytest.raises(DataValidationError):
        write(db, "k", 10**400)
    assert famepy.refame("k", np.int64(3), database=db).kind == "precision"
    assert famepy.refame("k", np.float64(3), database=db).kind == "precision"
    assert famepy.refame("k", np.float16(3), database=db).kind == "precision"


def test_nan_scalars_write_as_nc_and_read_by_policy(db):
    write(db, "p", math.nan)
    write(db, "n", np.float32("nan"))
    assert famepy.missing_type(db, "precision", read(db, "p").data) == 1
    assert famepy.missing_type(db, "numeric", read(db, "n").data) == 1
    assert math.isnan(value(db, "p"))
    back = value(db, "n")
    assert isinstance(back, np.float32) and np.isnan(back)
    with pytest.raises(bridge.MissingValueError):
        value(db, "p", missing="strict")
    with pytest.raises(bridge.MissingValueError):
        value(db, "n", missing="strict")


def test_missing_scalars_of_other_kinds(db):
    s = db.session.sentinels
    famepy.do_write(scalar_object("b", "boolean", s.boolean_na), db)
    famepy.do_write(scalar_object("d", "date", s.index_nd, date_frequency="monthly"), db)
    famepy.do_write(scalar_object("s", "string", s.string_nc), db)
    # A missing Boolean is never True (the reference reads it as true).
    with pytest.raises(bridge.MissingValueError):
        value(db, "b")
    with pytest.raises(bridge.MissingValueError):
        value(db, "b", missing="strict")
    # A missing date is None, never a plausible integer moment.
    assert value(db, "d") is None
    with pytest.raises(bridge.MissingValueError):
        value(db, "d", missing="strict")
    assert value(db, "s") is None
    with pytest.raises(bridge.MissingValueError):
        value(db, "s", missing="strict")
    famepy.do_write(scalar_object("two", "boolean", 2), db)
    assert value(db, "two") is True


def test_namelist_detection_and_literal_escape(db):
    def kind(item):
        return famepy.refame("k", item, database=db).kind

    assert kind("{a}") == "namelist"
    assert kind("{}") == "namelist"
    assert kind("{") == "string"
    assert kind("a{b}") == "string"
    assert kind(Text("{a}")) == "string"
    assert kind(b"{a}") == "string"  # bytes are always literal
    with pytest.raises(ValueError):
        NameList("{a b}")
    with pytest.raises(ValueError):
        NameList(["a,b"])
    with pytest.raises(ValueError):
        NameList("{a,,b}")
    with pytest.raises(ValueError):
        NameList("a")
    with pytest.raises(TypeError):
        NameList([1])
    with pytest.raises(ValueError):
        write(db, "bad", "{a b}")
    assert str(NameList("{ a , b }")) == "{a,b}"
    assert len(NameList("{a,b,c}")) == 3
    with pytest.raises(TypeError):
        Text(b"x")


def test_namelist_layout_from_library_is_parsed(db):
    db.session._native.fake.namelist_layout = "blank_after_comma"
    write(db, "nl", NameList(["a", "b", "c"]))
    assert read(db, "nl").data == b"{A, B, C}"
    assert value(db, "nl") == NameList(["A", "B", "C"])


def test_text_policies(db):
    famepy.do_write(scalar_object("s", "string", b"caf\xe9"), db)
    with pytest.raises(TextEncodingError):
        value(db, "s")
    assert value(db, "s", text="bytes") == b"caf\xe9"
    with pytest.raises(ValueError):
        value(db, "s", text="latin1")
    with pytest.raises(TextEncodingError):
        write(db, "t", "café")
    with pytest.raises(TextEncodingError):
        write(db, "t", "a\0b")


# -- series -------------------------------------------------------------------


def test_series_kinds_round_trip(db):
    p = TSeries(qq(2020, 1), [1.0, np.nan, 3.0])
    n = TSeries(mm(2020, 1), np.array([1.5, np.nan], dtype=np.float32))
    b = TSeries(daily("2020-02-28"), np.array([True, False, True]))
    i = TSeries(MIT(Unit(), 1), np.array([1, 2], dtype=np.int64))
    d = DateSeries(mm(2020, 1), [qq(2020, 1), None, qq(2021, 4)])
    s = StringSeries(mm(2020, 1), ["a", None, b"c"])
    v = ["x", "y"]
    t = ("p", "q")
    for name, item in {"p": p, "n": n, "b": b, "i": i, "d": d, "s": s, "v": v, "t": t}.items():
        write(db, name, item)
    assert _codes(db, "p") == [0, 1, 0] and _codes(db, "n") == [0, 1]
    assert _codes(db, "d") == [0, 1, 0] and _codes(db, "s") == [0, 1, 0]
    assert famepy.quick_info(db, "b").frequency == 8
    assert famepy.quick_info(db, "d").date_frequency == 162
    assert famepy.quick_info(db, "v").frequency == FREQUENCY_CASE
    assert read(db, "v").first_index == 1
    back = value(db, "p")
    assert isinstance(back, TSeries) and back.frequency == Quarterly()
    assert np.array_equal(back.values, p.values, equal_nan=True)
    back = value(db, "n")
    assert back.values.dtype == np.float32 and back.firstdate == mm(2020, 1)
    assert np.array_equal(back.values, n.values, equal_nan=True)
    back = value(db, "b")
    assert back.values.dtype == np.bool_ and back.values.tolist() == [True, False, True]
    assert back.firstdate == daily("2020-02-28")
    assert value(db, "i").values.tolist() == [1.0, 2.0]
    assert value(db, "i").frequency == Unit()
    back = value(db, "d")
    assert back == DateSeries(mm(2020, 1), [qq(2020, 1), None, qq(2021, 4)])
    assert back.value_frequency == Quarterly() and back.frequency == Monthly()
    assert value(db, "s") == StringSeries(mm(2020, 1), ["a", None, "c"])
    assert value(db, "v") == StringSeries(MIT(Unit(), 1), ["x", "y"])
    assert value(db, "t") == StringSeries(MIT(Unit(), 1), ["p", "q"])
    assert list(value(db, "v").values) == v
    typed = value(db, "p")
    assert typed.firstdate == qq(2020, 1) and np.array_equal(typed.values, p.values, equal_nan=True)


def test_strict_missing_for_series(db):
    write(db, "p", TSeries(mm(2020, 1), [np.nan]))
    write(db, "d", DateSeries(mm(2020, 1), [None], Monthly()))
    write(db, "s", StringSeries(mm(2020, 1), [None]))
    for name in ("p", "d", "s"):
        with pytest.raises(bridge.MissingValueError):
            value(db, name, missing="strict")
    assert value(db, "d").values == (None,)
    assert value(db, "s").values == (None,)
    assert value(db, "s", text="bytes").values == (None,)


def test_boolean_series_missing_is_refused_not_true(db):
    s = db.session.sentinels
    codes = np.array([1, s.boolean_nc, 0, s.boolean_na, s.boolean_nd], dtype=np.int32)
    famepy.do_write(series_object("b", "boolean", FREQUENCY_MONTHLY, 24240, codes), db)
    with pytest.raises(bridge.MissingValueError):
        value(db, "b")
    with pytest.raises(bridge.MissingValueError):
        value(db, "b", missing="strict")
    raw = read(db, "b")
    assert famepy.classify_by_sentinel(raw.data, "boolean", s).tolist() == [0, 1, 0, 2, 3]


def test_all_missing_categories_per_kind(db):
    s = db.session.sentinels
    first = 24240
    famepy.do_write(
        series_object(
            "p",
            "precision",
            "monthly",
            first,
            np.array([s.precision_nc, s.precision_na, s.precision_nd]),
        ),
        db,
    )
    famepy.do_write(
        series_object(
            "n",
            "numeric",
            "monthly",
            first,
            np.array([s.numeric_nc, s.numeric_na, s.numeric_nd], dtype=np.float32),
        ),
        db,
    )
    famepy.do_write(
        series_object(
            "d",
            "date",
            "monthly",
            first,
            np.array([s.index_nc, s.index_na, s.index_nd], dtype=np.int64),
            date_frequency="daily",
        ),
        db,
    )
    famepy.do_write(
        series_object("s", "string", "monthly", first, [s.string_nc, s.string_na, s.string_nd]), db
    )
    assert np.isnan(value(db, "p").values).all()
    assert np.isnan(value(db, "n").values).all()
    assert value(db, "d").values == (None, None, None)
    assert value(db, "s").values == (None, None, None)
    for name in ("p", "n", "d", "s"):
        with pytest.raises(bridge.MissingValueError):
            value(db, name, missing="strict")


def test_finite_sentinel_profile_is_not_nan(tmp_path):
    fake = make_fake(persist=True)
    fake.fake.profile = FINITE_SENTINELS
    session = famepy._runtime.Session(native=fake)
    session.initialize()
    try:
        db = famepy.opendb(tmp_path / "f.db", "create", session=session)
        write(db, "p", TSeries(mm(2020, 1), [1.0, np.nan]))
        raw = read(db, "p")
        assert raw.data[1] == FINITE_SENTINELS.precision_nc and not np.isnan(raw.data[1])
        assert np.isnan(value(db, "p").values[1])
        write(db, "n", np.float32("nan"))
        assert read(db, "n").data == FINITE_SENTINELS.numeric_nc
        assert np.isnan(value(db, "n"))
        # An ordinary value equal to nothing special is never mistaken for missing.
        write(db, "q", 1.125e301 * 0 + 5.0)
        assert value(db, "q") == 5.0
        famepy.closedb(db)
    finally:
        session.finalize()


# -- empty conventions --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "item", "kind"),
    [
        ("p", TSeries(qq(2020, 1), np.empty(0)), "precision"),
        ("n", TSeries(qq(2020, 1), np.empty(0, dtype=np.float32)), "numeric"),
        ("b", TSeries(qq(2020, 1), np.empty(0, dtype=np.bool_)), "boolean"),
        ("d", DateSeries(qq(2020, 1), (), Daily()), "date"),
        ("s", StringSeries(qq(2020, 1), ()), "string"),
        ("v", [], "string"),
    ],
)
def test_empty_preserve_and_reference(db, name, item, kind):
    write(db, name, item)
    info = famepy.quick_info(db, name)
    assert info.kind == kind and info.is_empty(SENTINELS.index_nc)
    with pytest.raises(bridge.EmptySeriesError):
        value(db, name)
    firstdate = MIT(Unit(), 1) if name == "v" else qq(2020, 1)
    back = value(db, name, empty_firstdate=firstdate)
    assert len(back) == 0 and back.firstdate == firstdate
    if isinstance(back, TSeries):
        assert back.values.dtype == item.values.dtype
    with pytest.raises(DataValidationError):
        value(db, name, empty_firstdate=mm(2020, 1))
    write(db, name + "r", item, empty="reference")
    raw = read(db, name + "r")
    if kind == "string":
        # No reference convention for string series: truly empty either way.
        assert raw.is_empty(SENTINELS.index_nc)
        return
    assert len(raw.data) == 1
    assert famepy.classify_by_sentinel(raw.data, kind, SENTINELS).tolist() == [2]
    preserved = value(db, name + "r") if kind != "boolean" else None
    if kind == "boolean":
        with pytest.raises(bridge.MissingValueError):
            value(db, name + "r")
    else:
        assert len(preserved) == 1
    collapsed = value(db, name + "r", empty="reference")
    assert len(collapsed) == 0 and collapsed.firstdate == qq(2020, 1)
    if kind == "date":
        assert collapsed.value_frequency == Daily()


def test_reference_collapse_applies_to_single_missing_only(db):
    write(db, "one", TSeries(mm(2020, 3), [np.nan]))
    write(db, "two", TSeries(mm(2020, 3), [np.nan, np.nan]))
    write(db, "s", StringSeries(mm(2020, 3), [None]))
    assert len(value(db, "one", empty="reference")) == 0
    assert len(value(db, "one")) == 1
    assert len(value(db, "two", empty="reference")) == 2
    assert len(value(db, "s", empty="reference")) == 1
    with pytest.raises(ValueError):
        value(db, "one", empty="other")
    with pytest.raises(ValueError):
        write(db, "x", TSeries(mm(2020, 1), []), empty="other")


# -- carriers and argument checks ---------------------------------------------


def test_carrier_validation():
    with pytest.raises(DataValidationError):
        DateSeries(mm(2020, 1), [qq(2020, 1), mm(2020, 1)])
    with pytest.raises(DataValidationError):
        DateSeries(mm(2020, 1), [])
    with pytest.raises(TypeError):
        DateSeries(mm(2020, 1), [3])
    with pytest.raises(TypeError):
        DateSeries(3, [qq(2020, 1)])
    with pytest.raises(TypeError):
        DateSeries(mm(2020, 1), [], value_frequency=Monthly)
    assert DateSeries(mm(2020, 1), [None], Daily()).value_frequency == Daily()
    assert len(DateSeries(mm(2020, 1), [None, qq(2020, 1)])) == 2
    with pytest.raises(TypeError):
        StringSeries(mm(2020, 1), [1])
    with pytest.raises(TypeError):
        StringSeries("x", ["a"])
    assert StringSeries(mm(2020, 1), ["a"]).frequency == Monthly()
    with pytest.raises(TypeError):
        bridge.validate_value(["a", 1])
    with pytest.raises(TypeError):
        bridge.validate_value(object())
    with pytest.raises(TypeError):
        bridge.validate_value({"a": 1})
    with pytest.raises(DataValidationError):
        bridge.validate_value(TSeries(mm(2020, 1), np.array([1 + 1j])))
    bridge.validate_value(TSeries(weekly_(2020), [1.0]))


def weekly_(year):
    return MIT(Weekly(5), 105000 + year)


def test_conversion_does_not_alias_or_mutate(db):
    ts = TSeries(mm(2020, 1), np.array([np.nan, 1.0]))
    obj = famepy.refame("ts", ts, database=db)
    assert obj.data is not ts.values and np.isnan(ts.values[0])
    assert obj.name == b"ts" and obj.is_series and obj.kind == "precision"
    b = TSeries(mm(2020, 1), np.array([True]))
    obj = famepy.refame("b", b, database=db)
    assert obj.data.dtype == np.int32 and b.values.dtype == np.bool_
    back = famepy.unfame(obj, database=db)
    assert back.values is not obj.data
    with pytest.raises(TypeError):
        famepy.unfame(ts, database=db)
    with pytest.raises(TypeError):
        famepy.refame("o", object(), database=db)
    with pytest.raises(ValueError):
        famepy.refame("", 1.0, database=db)
    with pytest.raises(DataValidationError, match="no data"):
        famepy.unfame(famepy.FameObject("b", "series", "boolean", "monthly"), database=db)


def test_unsupported_raw_frequencies_are_refused_on_read(db):
    famepy.do_write(series_object("t", "precision", "tenday", 5, np.zeros(1)), db)
    famepy.do_write(
        series_object(
            "dv", "date", "monthly", 24240, np.zeros(1, dtype=np.int64), date_frequency="tenday"
        ),
        db,
    )
    famepy.do_write(scalar_object("ds", "date", 5, date_frequency="ppy"), db)
    for name in ("t", "dv", "ds"):
        with pytest.raises(bridge.UnsupportedFrequencyError):
            value(db, name)
    # The raw layer still reads them.
    assert read(db, "t").frequency == 32


def test_write_value_path_form_validates_before_opening(session, tmp_path):
    path = tmp_path / "v.db"
    write(path, "keep", 1.0, mode="create")
    fake = session._native.fake
    fake.calls.clear()
    with pytest.raises(TypeError):
        write(path, "x", object(), mode="overwrite")
    with pytest.raises(DataValidationError):
        write(path, "x", 2**53 + 1, mode="overwrite")
    with pytest.raises(ValueError):
        write(path, "x", 1.0)
    with pytest.raises(ValueError):
        write(path, "x", 1.0, mode="overwrite", empty="no")
    with pytest.raises(TextEncodingError):
        write(path, "x", "café", mode="overwrite")
    with pytest.raises(ValueError):
        write(path, "", 1.0, mode="overwrite")
    assert fake.calls == []
    write(path, "d", qq(2020, 1), mode="update")
    assert "cfmpodb" in fake.calls
    assert value(path, "d") == qq(2020, 1)
    assert value(path, "keep") == 1.0
    assert session.open_databases == ()
    with pytest.raises(TypeError):
        value(3, "keep")
    with famepy.opendb(path, "update", session=session) as database:
        with pytest.raises(ValueError):
            write(database, "x", 1.0, mode="update")
        write(database, "obs", TSeries(mm(2020, 1), [1.0]), basis=famepy.Basis.BUSINESS)
        assert database.session._native.fake.handles[database.key].objects["OBS"].basis == 2


def test_bdaily_series_round_trip(db):
    from tsecon import bdaily

    ts = TSeries(bdaily("2020-12-31"), [1.0, 2.0, 3.0])
    write(db, "bd", ts)
    assert famepy.quick_info(db, "bd").frequency == 9
    back = value(db, "bd")
    assert back.firstdate == bdaily("2020-12-31") and back.frequency == BDaily()
    assert back.lastdate == bdaily("2021-01-04")
