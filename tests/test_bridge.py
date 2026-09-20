# SPDX-License-Identifier: MIT
import math

import numpy as np
import pytest
from canonical import read, scalar_object, series_object, value, write
from fake_native import SENTINELS
from tsecon import MIT, Monthly, Quarterly, TSeries, mm, qq

import famepy
from famepy import DataValidationError, bridge
from famepy._constants import FREQUENCY_MONTHLY


def test_frequency_mapping():
    assert bridge.fame_frequency(Monthly()) == FREQUENCY_MONTHLY
    assert bridge.tsecon_frequency(FREQUENCY_MONTHLY) == Monthly()
    assert bridge.fame_frequency(Quarterly()) == 162
    with pytest.raises(bridge.UnsupportedFrequencyError):
        bridge.tsecon_frequency(32)


def test_index_conversion_uses_library(db):
    index = bridge.mit_to_index(mm(2021, 7), database=db)
    assert index == 2021 * 12 + 6
    assert bridge.index_to_mit(index, FREQUENCY_MONTHLY, database=db) == mm(2021, 7)
    assert "fame_year_period_to_index" in db.session._native.fake.calls
    with pytest.raises(bridge.UnsupportedFrequencyError):
        bridge.index_to_mit(index, 32, database=db)


def test_series_round_trip_with_missing(db):
    ts = TSeries(mm(2020, 1), [1.0, np.nan, 3.5])
    original = ts.values.copy()
    write(db, "ts", ts)
    assert np.array_equal(ts.values, original, equal_nan=True)
    raw = read(db, "ts")
    codes = famepy.classify_by_sentinel(raw.data, "precision", db.session.sentinels)
    assert codes.tolist() == [0, 1, 0]
    back = value(db, "ts")
    assert back.firstdate == mm(2020, 1) and back.frequency == Monthly()
    assert np.array_equal(back.values, original, equal_nan=True)
    with pytest.raises(bridge.MissingValueError):
        value(db, "ts", missing="strict")
    with pytest.raises(ValueError):
        value(db, "ts", missing="drop")


def test_all_missing_categories_collapse_to_nan_by_default(db):
    values = np.array([SENTINELS.precision_nc, SENTINELS.precision_na, SENTINELS.precision_nd, 2.0])
    famepy.do_write(series_object("m", "precision", FREQUENCY_MONTHLY, 24240, values), db)
    back = value(db, "m")
    assert np.isnan(back.values[:3]).all() and back.values[3] == 2.0
    assert back.firstdate == mm(2020, 1)


def test_empty_contracts(db):
    empty = TSeries(mm(2020, 3), np.empty(0))
    write(db, "e", empty)
    info = famepy.quick_info(db, "e")
    assert info.is_empty(SENTINELS.index_nc)
    with pytest.raises(bridge.EmptySeriesError):
        value(db, "e")
    back = value(db, "e", empty_firstdate=mm(2020, 3))
    assert len(back) == 0 and back.firstdate == mm(2020, 3)
    with pytest.raises(DataValidationError):
        value(db, "e", empty_firstdate=qq(2020, 3))

    write(db, "r", empty, empty="reference")
    raw = read(db, "r")
    assert len(raw.data) == 1 and raw.first_index == 2020 * 12 + 2
    assert famepy.classify_by_sentinel(raw.data, "precision", db.session.sentinels).tolist() == [2]
    preserved = value(db, "r")
    assert len(preserved) == 1 and math.isnan(preserved.values[0])
    collapsed = value(db, "r", empty="reference")
    assert len(collapsed) == 0 and collapsed.firstdate == mm(2020, 3)
    one_nan = TSeries(mm(2020, 3), [np.nan])
    write(db, "one", one_nan)
    assert len(value(db, "one", empty="reference")) == 0
    with pytest.raises(ValueError):
        write(db, "bad", empty, empty="other")


def test_integer_and_dtype_policy(db):
    exact = TSeries(mm(2020, 1), np.array([1, 2, 3], dtype=np.int64))
    write(db, "i", exact)
    assert value(db, "i").values.tolist() == [1.0, 2.0, 3.0]
    large = TSeries(mm(2020, 1), np.array([2**60, -(2**60), 2**53], dtype=np.int64))
    write(db, "large", large)
    assert value(db, "large").values.tolist() == [2.0**60, -(2.0**60), 2.0**53]
    unsigned = TSeries(mm(2020, 1), np.array([2**64 - 1], dtype=np.uint64))
    with pytest.raises(DataValidationError):
        write(db, "u", unsigned)
    inexact = TSeries(mm(2020, 1), np.array([2**53 + 1], dtype=np.int64))
    with pytest.raises(DataValidationError):
        write(db, "j", inexact)
    write(db, "f", TSeries(mm(2020, 1), np.array([1.0], dtype=np.float32)))
    assert famepy.quick_info(db, "f").kind == "numeric"
    write(db, "b", TSeries(mm(2020, 1), np.array([True])))
    assert famepy.quick_info(db, "b").kind == "boolean"
    with pytest.raises(DataValidationError):
        write(db, "c", TSeries(mm(2020, 1), np.array([1 + 2j])))
    write(db, "q", TSeries(qq(2020, 1), [1.0]))
    assert famepy.quick_info(db, "q").frequency == 162
    with pytest.raises(TypeError):
        famepy.refame("t", object(), database=db)


def test_scalar_round_trip(db):
    write(db, "s", 2.5)
    write(db, "n", math.nan)
    write(db, "i", 7)
    assert value(db, "s") == 2.5
    assert math.isnan(value(db, "n"))
    assert value(db, "i") == 7.0
    assert famepy.missing_type(db, "precision", read(db, "n").data) == 1
    with pytest.raises(bridge.MissingValueError):
        value(db, "n", missing="strict")
    famepy.do_write(scalar_object("str", "string", b"x"), db)
    assert value(db, "str") == "x"
    with pytest.raises(TypeError):
        write(db, "t", object())
    write(db, "big", 2**60)
    assert value(db, "big") == float(2**60)
    with pytest.raises(DataValidationError):
        write(db, "odd", 2**53 + 1)
    with pytest.raises(DataValidationError):
        write(db, "huge", 10**400)
    with pytest.raises(ValueError):
        value(db, "s", missing="drop")
    with pytest.raises(famepy.HLIError):
        write(db, "s", 1.0)
    write(db, "s", 1.0, replace=True)
    assert value(db, "s") == 1.0


def test_path_forms_post_and_close(session, tmp_path):
    path = tmp_path / "bridge.db"
    ts = TSeries(mm(2020, 1), [1.0, 2.0])
    with pytest.raises(ValueError, match="mode"):
        write(path, "ts", ts)
    write(path, "ts", ts, mode="create")
    write(path, "sc", 4.0, mode="update")
    assert session.open_databases == ()
    back = value(path, "ts")
    assert back.equals(ts)
    assert value(path, "sc") == 4.0
    assert session.open_databases == ()
    with pytest.raises(TypeError):
        value(3, "ts")
    with famepy.opendb(path, "update", session=session) as database:
        with pytest.raises(ValueError):
            write(database, "x", ts, mode="update")


def test_conversion_functions_do_not_alias(db):
    ts = TSeries(mm(2020, 1), np.array([np.nan, 1.0]))
    obj = famepy.refame("ts", ts, database=db)
    assert obj.data is not ts.values and np.isnan(ts.values[0])
    back = famepy.unfame(obj, database=db)
    assert back.values is not obj.data
    with pytest.raises(TypeError):
        famepy.unfame(ts, database=db)
    numeric = famepy.unfame(
        series_object("n", "numeric", FREQUENCY_MONTHLY, 0, np.zeros(1, np.float32)), database=db
    )
    assert numeric.values.dtype == np.float32
    strings = famepy.unfame(series_object("s", "string", FREQUENCY_MONTHLY, 0, [b"a"]), database=db)
    assert isinstance(strings, bridge.StringSeries)
    assert isinstance(MIT.from_yp(Monthly(), 2020, 1), MIT)


def test_invalid_inputs_never_open_or_truncate_the_database(session, tmp_path):
    path = tmp_path / "keep.db"
    write(path, "saved", 3.0, mode="create")
    fake = session._native.fake
    fake.calls.clear()
    with pytest.raises(DataValidationError):
        write(path, "x", 2**53 + 1, mode="overwrite")
    with pytest.raises(DataValidationError):
        write(path, "f", TSeries(mm(2020, 1), np.array([1 + 1j], dtype=complex)), mode="overwrite")
    with pytest.raises(TypeError):
        write(path, "q", object(), mode="overwrite")
    with pytest.raises(ValueError):
        write(path, "e", TSeries(mm(2020, 1), np.empty(0)), mode="overwrite", empty="other")
    with pytest.raises(TypeError):
        write(path, "t", [1.0], mode="overwrite")
    with pytest.raises(famepy.TextEncodingError):
        write(path, "caf\u00e9", 1.0, mode="overwrite")
    with pytest.raises(ValueError):
        value(path, "saved", missing="drop")
    with pytest.raises(ValueError):
        value(path, "saved", empty="other")
    assert fake.calls == []
    assert value(path, "saved") == 3.0
