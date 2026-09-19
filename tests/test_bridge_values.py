# SPDX-License-Identifier: MIT
"""Value conversions: every kind, missing category, empty convention and carrier."""

import math

import numpy as np
import pytest
from fake_native import FINITE_SENTINELS, SENTINELS, make_fake
from tsecon import MIT, BDaily, Daily, Monthly, Quarterly, TSeries, Unit, Weekly, daily, mm, qq

import famepy
from famepy import DataValidationError, TextEncodingError, bridge
from famepy._constants import FREQUENCY_CASE, FREQUENCY_MONTHLY
from famepy.bridge import DateSeries, NameList, StringSeries, Text


def _codes(db, name):
    raw = famepy.read_object(db, name)
    return famepy.classify_by_sentinel(raw.values, raw.kind, db.session.sentinels).tolist()


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
        "dc": (MIT(Unit(), 4), "date", MIT(Unit(), 4)),
        "s": ("hello", "string", "hello"),
        "sb": (b"bytes", "string", "bytes"),
        "t": (Text("{not,a,list}"), "string", "{not,a,list}"),
        "nl": ("{a,b}", "namelist", NameList(["A", "B"])),
        "nl2": (NameList(["x", "y"]), "namelist", NameList(["X", "Y"])),
        "nle": ("{}", "namelist", NameList()),
    }
    for name, (value, kind, _) in cases.items():
        assert bridge.raw_kind(value) == kind
        bridge.write_value(db, name, value)
        assert famepy.quick_info(db, name).kind == kind
    for name, (_, _kind, expected) in cases.items():
        back = bridge.read_value(db, name)
        assert back == expected, name
        assert type(back) is type(expected), name
        assert bridge.read_scalar(db, name) == expected
    assert famepy.quick_info(db, "d").date_frequency == 162
    assert famepy.quick_info(db, "dc").date_frequency == FREQUENCY_CASE
    assert famepy.read_object(db, "nl").value == b"{A,B}"


def test_integer_scalars_are_exact_precision_not_float32(db):
    # Deliberate difference: the reference promotes integers to float32 numeric.
    bridge.write_value(db, "i", 2**53)
    assert famepy.quick_info(db, "i").kind == "precision"
    assert bridge.read_value(db, "i") == float(2**53)
    with pytest.raises(DataValidationError):
        bridge.write_value(db, "j", 2**53 + 1)
    with pytest.raises(DataValidationError):
        bridge.write_value(db, "k", 10**400)
    assert bridge.raw_kind(np.int64(3)) == "precision"
    assert bridge.raw_kind(np.float64(3)) == "precision"
    assert bridge.raw_kind(np.float16(3)) == "precision"


def test_nan_scalars_write_as_nc_and_read_by_policy(db):
    bridge.write_value(db, "p", math.nan)
    bridge.write_value(db, "n", np.float32("nan"))
    assert famepy.missing_type(db, "precision", famepy.read_object(db, "p").value) == 1
    assert famepy.missing_type(db, "numeric", famepy.read_object(db, "n").value) == 1
    assert math.isnan(bridge.read_value(db, "p"))
    back = bridge.read_value(db, "n")
    assert isinstance(back, np.float32) and np.isnan(back)
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "p", missing="strict")
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "n", missing="strict")


def test_missing_scalars_of_other_kinds(db):
    s = db.session.sentinels
    famepy.write_object(db, "b", famepy.scalar("boolean", s.boolean_na))
    famepy.write_object(db, "d", famepy.scalar("date", s.index_nd, date_frequency="monthly"))
    famepy.write_object(db, "s", famepy.scalar("string", s.string_nc))
    # A missing Boolean is never True (the reference reads it as true).
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "b")
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "b", missing="strict")
    # A missing date is None, never a plausible integer moment.
    assert bridge.read_value(db, "d") is None
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "d", missing="strict")
    assert bridge.read_value(db, "s") is None
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "s", missing="strict")
    famepy.write_object(db, "two", famepy.scalar("boolean", 2))
    assert bridge.read_value(db, "two") is True


def test_namelist_detection_and_literal_escape(db):
    assert bridge.raw_kind("{a}") == "namelist"
    assert bridge.raw_kind("{}") == "namelist"
    assert bridge.raw_kind("{") == "string"
    assert bridge.raw_kind("a{b}") == "string"
    assert bridge.raw_kind(Text("{a}")) == "string"
    assert bridge.raw_kind(b"{a}") == "string"  # bytes are always literal
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
        bridge.write_value(db, "bad", "{a b}")
    assert str(NameList("{ a , b }")) == "{a,b}"
    assert len(NameList("{a,b,c}")) == 3
    with pytest.raises(TypeError):
        Text(b"x")


def test_namelist_layout_from_library_is_parsed(db):
    db.session._native.fake.namelist_layout = "blank_after_comma"
    bridge.write_value(db, "nl", NameList(["a", "b", "c"]))
    assert famepy.read_object(db, "nl").value == b"{A, B, C}"
    assert bridge.read_value(db, "nl") == NameList(["A", "B", "C"])


def test_text_policies(db):
    famepy.write_object(db, "s", famepy.scalar("string", b"caf\xe9"))
    with pytest.raises(TextEncodingError):
        bridge.read_value(db, "s")
    assert bridge.read_value(db, "s", text="bytes") == b"caf\xe9"
    with pytest.raises(ValueError):
        bridge.read_value(db, "s", text="latin1")
    with pytest.raises(TextEncodingError):
        bridge.write_value(db, "t", "café")
    with pytest.raises(TextEncodingError):
        bridge.write_value(db, "t", "a\0b")


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
    for name, value in {"p": p, "n": n, "b": b, "i": i, "d": d, "s": s, "v": v, "t": t}.items():
        bridge.write_value(db, name, value)
    assert _codes(db, "p") == [0, 1, 0] and _codes(db, "n") == [0, 1]
    assert _codes(db, "d") == [0, 1, 0] and _codes(db, "s") == [0, 1, 0]
    assert famepy.quick_info(db, "b").frequency == 8
    assert famepy.quick_info(db, "d").date_frequency == 162
    assert famepy.quick_info(db, "v").frequency == FREQUENCY_CASE
    assert famepy.read_object(db, "v").first_index == 1
    back = bridge.read_value(db, "p")
    assert isinstance(back, TSeries) and back.frequency == Quarterly()
    assert np.array_equal(back.values, p.values, equal_nan=True)
    back = bridge.read_value(db, "n")
    assert back.values.dtype == np.float32 and back.firstdate == mm(2020, 1)
    assert np.array_equal(back.values, n.values, equal_nan=True)
    back = bridge.read_value(db, "b")
    assert back.values.dtype == np.bool_ and back.values.tolist() == [True, False, True]
    assert back.firstdate == daily("2020-02-28")
    assert bridge.read_value(db, "i").values.tolist() == [1.0, 2.0]
    assert bridge.read_value(db, "i").frequency == Unit()
    back = bridge.read_value(db, "d")
    assert back == DateSeries(mm(2020, 1), [qq(2020, 1), None, qq(2021, 4)])
    assert back.value_frequency == Quarterly() and back.frequency == Monthly()
    assert bridge.read_value(db, "s") == StringSeries(mm(2020, 1), ["a", None, "c"])
    assert bridge.read_value(db, "v") == StringSeries(MIT(Unit(), 1), ["x", "y"])
    assert bridge.read_value(db, "t") == StringSeries(MIT(Unit(), 1), ["p", "q"])
    assert list(bridge.read_value(db, "v").values) == v
    typed = bridge.read_tseries(db, "p")
    assert typed.firstdate == qq(2020, 1) and np.array_equal(typed.values, p.values, equal_nan=True)
    with pytest.raises(DataValidationError):
        bridge.read_tseries(db, "d")
    with pytest.raises(DataValidationError):
        bridge.read_scalar(db, "p")


def test_strict_missing_for_series(db):
    bridge.write_value(db, "p", TSeries(mm(2020, 1), [np.nan]))
    bridge.write_value(db, "d", DateSeries(mm(2020, 1), [None], Monthly()))
    bridge.write_value(db, "s", StringSeries(mm(2020, 1), [None]))
    for name in ("p", "d", "s"):
        with pytest.raises(bridge.MissingValueError):
            bridge.read_value(db, name, missing="strict")
    assert bridge.read_value(db, "d").values == (None,)
    assert bridge.read_value(db, "s").values == (None,)
    assert bridge.read_value(db, "s", text="bytes").values == (None,)


def test_boolean_series_missing_is_refused_not_true(db):
    s = db.session.sentinels
    codes = np.array([1, s.boolean_nc, 0, s.boolean_na, s.boolean_nd], dtype=np.int32)
    famepy.write_object(db, "b", famepy.series("boolean", FREQUENCY_MONTHLY, 24240, codes))
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "b")
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "b", missing="strict")
    raw = famepy.read_object(db, "b")
    assert famepy.classify_by_sentinel(raw.values, "boolean", s).tolist() == [0, 1, 0, 2, 3]


def test_all_missing_categories_per_kind(db):
    s = db.session.sentinels
    first = 24240
    famepy.write_object(
        db,
        "p",
        famepy.series(
            "precision",
            "monthly",
            first,
            np.array([s.precision_nc, s.precision_na, s.precision_nd]),
        ),
    )
    famepy.write_object(
        db,
        "n",
        famepy.series(
            "numeric",
            "monthly",
            first,
            np.array([s.numeric_nc, s.numeric_na, s.numeric_nd], dtype=np.float32),
        ),
    )
    famepy.write_object(
        db,
        "d",
        famepy.series(
            "date",
            "monthly",
            first,
            np.array([s.index_nc, s.index_na, s.index_nd], dtype=np.int64),
            date_frequency="daily",
        ),
    )
    famepy.write_object(
        db, "s", famepy.series("string", "monthly", first, [s.string_nc, s.string_na, s.string_nd])
    )
    assert np.isnan(bridge.read_value(db, "p").values).all()
    assert np.isnan(bridge.read_value(db, "n").values).all()
    assert bridge.read_value(db, "d").values == (None, None, None)
    assert bridge.read_value(db, "s").values == (None, None, None)
    for name in ("p", "n", "d", "s"):
        with pytest.raises(bridge.MissingValueError):
            bridge.read_value(db, name, missing="strict")


def test_finite_sentinel_profile_is_not_nan(tmp_path):
    fake = make_fake(persist=True)
    fake.fake.profile = FINITE_SENTINELS
    session = famepy._runtime.Session(native=fake)
    session.initialize()
    try:
        db = famepy.open_database(tmp_path / "f.db", "create", session=session)
        bridge.write_value(db, "p", TSeries(mm(2020, 1), [1.0, np.nan]))
        raw = famepy.read_object(db, "p")
        assert raw.values[1] == FINITE_SENTINELS.precision_nc and not np.isnan(raw.values[1])
        assert np.isnan(bridge.read_value(db, "p").values[1])
        bridge.write_value(db, "n", np.float32("nan"))
        assert famepy.read_object(db, "n").value == FINITE_SENTINELS.numeric_nc
        assert np.isnan(bridge.read_value(db, "n"))
        # An ordinary value equal to nothing special is never mistaken for missing.
        bridge.write_value(db, "q", 1.125e301 * 0 + 5.0)
        assert bridge.read_value(db, "q") == 5.0
        db.close()
    finally:
        session.finalize()


# -- empty conventions --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "kind"),
    [
        ("p", TSeries(qq(2020, 1), np.empty(0)), "precision"),
        ("n", TSeries(qq(2020, 1), np.empty(0, dtype=np.float32)), "numeric"),
        ("b", TSeries(qq(2020, 1), np.empty(0, dtype=np.bool_)), "boolean"),
        ("d", DateSeries(qq(2020, 1), (), Daily()), "date"),
        ("s", StringSeries(qq(2020, 1), ()), "string"),
        ("v", [], "string"),
    ],
)
def test_empty_preserve_and_reference(db, name, value, kind):
    bridge.write_value(db, name, value)
    info = famepy.quick_info(db, name)
    assert info.kind == kind and info.is_empty(SENTINELS.index_nc)
    with pytest.raises(bridge.EmptySeriesError):
        bridge.read_value(db, name)
    firstdate = MIT(Unit(), 1) if name == "v" else qq(2020, 1)
    back = bridge.read_value(db, name, empty_firstdate=firstdate)
    assert len(back) == 0 and back.firstdate == firstdate
    if isinstance(back, TSeries):
        assert back.values.dtype == value.values.dtype
    with pytest.raises(DataValidationError):
        bridge.read_value(db, name, empty_firstdate=mm(2020, 1))
    bridge.write_value(db, name + "r", value, empty="reference")
    raw = famepy.read_object(db, name + "r")
    if kind == "string":
        # No reference convention for string series: truly empty either way.
        assert raw.is_empty
        return
    assert len(raw) == 1 and famepy.classify_by_sentinel(raw.values, kind, SENTINELS).tolist() == [
        2
    ]
    preserved = bridge.read_value(db, name + "r") if kind != "boolean" else None
    if kind == "boolean":
        with pytest.raises(bridge.MissingValueError):
            bridge.read_value(db, name + "r")
    else:
        assert len(preserved) == 1
    collapsed = bridge.read_value(db, name + "r", empty="reference")
    assert len(collapsed) == 0 and collapsed.firstdate == qq(2020, 1)
    if kind == "date":
        assert collapsed.value_frequency == Daily()


def test_reference_collapse_applies_to_single_missing_only(db):
    bridge.write_value(db, "one", TSeries(mm(2020, 3), [np.nan]))
    bridge.write_value(db, "two", TSeries(mm(2020, 3), [np.nan, np.nan]))
    bridge.write_value(db, "s", StringSeries(mm(2020, 3), [None]))
    assert len(bridge.read_value(db, "one", empty="reference")) == 0
    assert len(bridge.read_value(db, "one")) == 1
    assert len(bridge.read_value(db, "two", empty="reference")) == 2
    assert len(bridge.read_value(db, "s", empty="reference")) == 1
    with pytest.raises(ValueError):
        bridge.read_value(db, "one", empty="other")
    with pytest.raises(ValueError):
        bridge.write_value(db, "x", TSeries(mm(2020, 1), []), empty="other")


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
    raw = bridge.to_fame(ts, database=db)
    assert raw.values is not ts.values and np.isnan(ts.values[0])
    b = TSeries(mm(2020, 1), np.array([True]))
    raw = bridge.to_fame(b, database=db)
    assert raw.values.dtype == np.int32 and b.values.dtype == np.bool_
    back = bridge.from_fame(raw, database=db)
    assert back.values is not raw.values
    with pytest.raises(TypeError):
        bridge.from_fame(ts, database=db)
    with pytest.raises(TypeError):
        bridge.to_fame(object(), database=db)


def test_unsupported_raw_frequencies_are_refused_on_read(db):
    famepy.write_object(db, "t", famepy.series("precision", "tenday", 5, np.zeros(1)))
    famepy.write_object(
        db,
        "dv",
        famepy.series(
            "date", "monthly", 24240, np.zeros(1, dtype=np.int64), date_frequency="tenday"
        ),
    )
    famepy.write_object(db, "ds", famepy.scalar("date", 5, date_frequency="ppy"))
    for name in ("t", "dv", "ds"):
        with pytest.raises(bridge.UnsupportedFrequencyError):
            bridge.read_value(db, name)
    # The raw layer still reads them.
    assert famepy.read_object(db, "t").frequency == 32


def test_write_value_path_form_validates_before_opening(session, tmp_path):
    path = tmp_path / "v.db"
    bridge.write_value(path, "keep", 1.0, mode="create")
    fake = session._native.fake
    fake.calls.clear()
    with pytest.raises(TypeError):
        bridge.write_value(path, "x", object(), mode="overwrite")
    with pytest.raises(DataValidationError):
        bridge.write_value(path, "x", 2**53 + 1, mode="overwrite")
    with pytest.raises(ValueError):
        bridge.write_value(path, "x", 1.0)
    with pytest.raises(ValueError):
        bridge.write_value(path, "x", 1.0, mode="overwrite", empty="no")
    with pytest.raises(TextEncodingError):
        bridge.write_value(path, "x", "café", mode="overwrite")
    with pytest.raises(ValueError):
        bridge.write_value(path, "", 1.0, mode="overwrite")
    assert fake.calls == []
    bridge.write_value(path, "d", qq(2020, 1), mode="update")
    assert "cfmpodb" in fake.calls
    assert bridge.read_value(path, "d") == qq(2020, 1)
    assert bridge.read_value(path, "keep") == 1.0
    assert session.open_databases == ()
    with pytest.raises(TypeError):
        bridge.read_value(3, "keep")
    with famepy.open_database(path, "update", session=session) as database:
        with pytest.raises(ValueError):
            bridge.write_value(database, "x", 1.0, mode="update")
        bridge.write_value(
            database, "obs", TSeries(mm(2020, 1), [1.0]), basis=famepy.Basis.BUSINESS
        )
        assert database.session._native.fake.handles[database.key].objects["OBS"].basis == 2


def test_bdaily_series_round_trip(db):
    from tsecon import bdaily

    ts = TSeries(bdaily("2020-12-31"), [1.0, 2.0, 3.0])
    bridge.write_value(db, "bd", ts)
    assert famepy.quick_info(db, "bd").frequency == 9
    back = bridge.read_value(db, "bd")
    assert back.firstdate == bdaily("2020-12-31") and back.frequency == BDaily()
    assert back.lastdate == bdaily("2021-01-04")
