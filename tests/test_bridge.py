# SPDX-License-Identifier: MIT
import math

import numpy as np
import pytest
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
    bridge.write_tseries(db, "ts", ts)
    assert np.array_equal(ts.values, original, equal_nan=True)
    raw = famepy.read_object(db, "ts")
    codes = famepy.classify_by_sentinel(raw.values, "precision", db.session.sentinels)
    assert codes.tolist() == [0, 1, 0]
    back = bridge.read_tseries(db, "ts")
    assert back.firstdate == mm(2020, 1) and back.frequency == Monthly()
    assert np.array_equal(back.values, original, equal_nan=True)
    with pytest.raises(bridge.MissingValueError):
        bridge.read_tseries(db, "ts", missing="strict")
    with pytest.raises(ValueError):
        bridge.read_tseries(db, "ts", missing="drop")


def test_all_missing_categories_collapse_to_nan_by_default(db):
    values = np.array([SENTINELS.precision_nc, SENTINELS.precision_na, SENTINELS.precision_nd, 2.0])
    famepy.write_object(db, "m", famepy.series("precision", FREQUENCY_MONTHLY, 24240, values))
    back = bridge.read_tseries(db, "m")
    assert np.isnan(back.values[:3]).all() and back.values[3] == 2.0
    assert back.firstdate == mm(2020, 1)


def test_empty_contracts(db):
    empty = TSeries(mm(2020, 3), np.empty(0))
    bridge.write_tseries(db, "e", empty)
    info = famepy.quick_info(db, "e")
    assert info.is_empty(SENTINELS.index_nc)
    with pytest.raises(bridge.EmptySeriesError):
        bridge.read_tseries(db, "e")
    back = bridge.read_tseries(db, "e", empty_firstdate=mm(2020, 3))
    assert len(back) == 0 and back.firstdate == mm(2020, 3)
    with pytest.raises(DataValidationError):
        bridge.read_tseries(db, "e", empty_firstdate=qq(2020, 3))

    bridge.write_tseries(db, "r", empty, empty="reference")
    raw = famepy.read_object(db, "r")
    assert len(raw) == 1 and raw.first_index == 2020 * 12 + 2
    assert famepy.classify_by_sentinel(raw.values, "precision", db.session.sentinels).tolist() == [
        2
    ]
    preserved = bridge.read_tseries(db, "r")
    assert len(preserved) == 1 and math.isnan(preserved.values[0])
    collapsed = bridge.read_tseries(db, "r", empty="reference")
    assert len(collapsed) == 0 and collapsed.firstdate == mm(2020, 3)
    one_nan = TSeries(mm(2020, 3), [np.nan])
    bridge.write_tseries(db, "one", one_nan)
    assert len(bridge.read_tseries(db, "one", empty="reference")) == 0
    with pytest.raises(ValueError):
        bridge.write_tseries(db, "bad", empty, empty="other")


def test_integer_and_dtype_policy(db):
    exact = TSeries(mm(2020, 1), np.array([1, 2, 3], dtype=np.int64))
    bridge.write_tseries(db, "i", exact)
    assert bridge.read_tseries(db, "i").values.tolist() == [1.0, 2.0, 3.0]
    large = TSeries(mm(2020, 1), np.array([2**60, -(2**60), 2**53], dtype=np.int64))
    bridge.write_tseries(db, "large", large)
    assert bridge.read_tseries(db, "large").values.tolist() == [2.0**60, -(2.0**60), 2.0**53]
    unsigned = TSeries(mm(2020, 1), np.array([2**64 - 1], dtype=np.uint64))
    with pytest.raises(DataValidationError):
        bridge.write_tseries(db, "u", unsigned)
    inexact = TSeries(mm(2020, 1), np.array([2**53 + 1], dtype=np.int64))
    with pytest.raises(DataValidationError):
        bridge.write_tseries(db, "j", inexact)
    bridge.write_tseries(db, "f", TSeries(mm(2020, 1), np.array([1.0], dtype=np.float32)))
    assert famepy.quick_info(db, "f").kind == "numeric"
    bridge.write_tseries(db, "b", TSeries(mm(2020, 1), np.array([True])))
    assert famepy.quick_info(db, "b").kind == "boolean"
    with pytest.raises(DataValidationError):
        bridge.write_tseries(db, "c", TSeries(mm(2020, 1), np.array([1 + 2j])))
    bridge.write_tseries(db, "q", TSeries(qq(2020, 1), [1.0]))
    assert famepy.quick_info(db, "q").frequency == 162
    with pytest.raises(TypeError):
        bridge.from_tseries([1.0], database=db)


def test_scalar_round_trip(db):
    bridge.write_scalar(db, "s", 2.5)
    bridge.write_scalar(db, "n", math.nan)
    bridge.write_scalar(db, "i", 7)
    assert bridge.read_scalar(db, "s") == 2.5
    assert math.isnan(bridge.read_scalar(db, "n"))
    assert bridge.read_scalar(db, "i") == 7.0
    assert famepy.missing_type(db, "precision", famepy.read_object(db, "n").value) == 1
    with pytest.raises(bridge.MissingValueError):
        bridge.read_scalar(db, "n", missing="strict")
    famepy.write_object(db, "str", famepy.scalar("string", b"x"))
    assert bridge.read_scalar(db, "str") == "x"
    with pytest.raises(DataValidationError):
        bridge.read_tseries(db, "s")
    with pytest.raises(TypeError):
        bridge.write_scalar(db, "t", TSeries(mm(2020, 1), [1.0]))
    with pytest.raises(TypeError):
        bridge.write_scalar(db, "t", object())
    bridge.write_scalar(db, "big", 2**60)
    assert bridge.read_scalar(db, "big") == float(2**60)
    with pytest.raises(DataValidationError):
        bridge.write_scalar(db, "odd", 2**53 + 1)
    with pytest.raises(DataValidationError):
        bridge.write_scalar(db, "huge", 10**400)
    with pytest.raises(ValueError):
        bridge.read_scalar(db, "s", missing="drop")
    with pytest.raises(famepy.FameError):
        bridge.write_scalar(db, "s", 1.0)
    bridge.write_scalar(db, "s", 1.0, replace=True)
    assert bridge.read_scalar(db, "s") == 1.0


def test_path_forms_post_and_close(session, tmp_path):
    path = tmp_path / "bridge.db"
    ts = TSeries(mm(2020, 1), [1.0, 2.0])
    with pytest.raises(ValueError, match="mode"):
        bridge.write_tseries(path, "ts", ts)
    bridge.write_tseries(path, "ts", ts, mode="create")
    bridge.write_scalar(path, "sc", 4.0, mode="update")
    assert session.open_databases == ()
    back = bridge.read_tseries(path, "ts")
    assert back.equals(ts)
    assert bridge.read_scalar(path, "sc") == 4.0
    assert session.open_databases == ()
    with pytest.raises(TypeError):
        bridge.read_tseries(3, "ts")
    with famepy.open_database(path, "update", session=session) as database:
        with pytest.raises(ValueError):
            bridge.write_tseries(database, "x", ts, mode="update")


def test_conversion_functions_do_not_alias(db):
    ts = TSeries(mm(2020, 1), np.array([np.nan, 1.0]))
    raw = bridge.from_tseries(ts, database=db)
    assert raw.values is not ts.values and np.isnan(ts.values[0])
    back = bridge.to_tseries(raw, database=db)
    assert back.values is not raw.values
    with pytest.raises(TypeError):
        bridge.to_tseries(ts, database=db)
    numeric = bridge.to_tseries(
        famepy.series("numeric", FREQUENCY_MONTHLY, 0, np.zeros(1, np.float32)), database=db
    )
    assert numeric.values.dtype == np.float32
    with pytest.raises(DataValidationError):
        bridge.to_tseries(famepy.series("string", FREQUENCY_MONTHLY, 0, [b"a"]), database=db)
    assert isinstance(MIT.from_yp(Monthly(), 2020, 1), MIT)


def test_invalid_inputs_never_open_or_truncate_the_database(session, tmp_path):
    path = tmp_path / "keep.db"
    bridge.write_scalar(path, "saved", 3.0, mode="create")
    fake = session._native.fake
    fake.calls.clear()
    with pytest.raises(DataValidationError):
        bridge.write_scalar(path, "x", 2**53 + 1, mode="overwrite")
    with pytest.raises(DataValidationError):
        bridge.write_tseries(
            path, "f", TSeries(mm(2020, 1), np.array([1 + 1j], dtype=complex)), mode="overwrite"
        )
    with pytest.raises(TypeError):
        bridge.write_value(path, "q", object(), mode="overwrite")
    with pytest.raises(ValueError):
        bridge.write_tseries(
            path, "e", TSeries(mm(2020, 1), np.empty(0)), mode="overwrite", empty="other"
        )
    with pytest.raises(TypeError):
        bridge.write_tseries(path, "t", [1.0], mode="overwrite")
    with pytest.raises(famepy.TextEncodingError):
        bridge.write_scalar(path, "caf\u00e9", 1.0, mode="overwrite")
    with pytest.raises(ValueError):
        bridge.read_scalar(path, "saved", missing="drop")
    with pytest.raises(ValueError):
        bridge.read_tseries(path, "saved", empty="other")
    assert fake.calls == []
    assert bridge.read_scalar(path, "saved") == 3.0
