# SPDX-License-Identifier: MIT
import numpy as np
import pytest
from canonical import read, scalar_object, series_object
from fake_native import S_EXISTS, S_TYPE_MISMATCH, SENTINELS

import famepy
from famepy import (
    DataValidationError,
    HLIError,
    UnsupportedOperationError,
    classify_by_sentinel,
    do_write,
)
from famepy._constants import FREQUENCY_MONTHLY, ObjectClass, ObjectType
from famepy._data import RawScalar, RawSeries

MONTHLY = FREQUENCY_MONTHLY
FIRST = 2020 * 12


def test_precision_series_round_trip_preserves_missing_codes(db):
    values = np.array([1.0, SENTINELS.precision_nc, SENTINELS.precision_na, SENTINELS.precision_nd])
    original = values.copy()
    do_write(series_object("ps", "precision", MONTHLY, FIRST, values), db)
    assert np.array_equal(values.view(np.uint64), original.view(np.uint64))
    info = famepy.quick_info(db, "ps")
    assert (info.class_code, info.type_code, info.frequency) == (1, ObjectType.PRECISION, MONTHLY)
    assert (info.first_index, info.last_index) == (FIRST, FIRST + 3)
    assert str(info) == "ps: series,precision,monthly,24240:24243"
    raw = read(db, "ps")
    assert raw.is_series and raw.kind == "precision"
    assert raw.data.dtype == np.float64
    assert np.array_equal(raw.data.view(np.uint64), original.view(np.uint64))
    assert raw.data is not values
    codes = classify_by_sentinel(raw.data, "precision", db.session.sentinels)
    assert codes.tolist() == [0, 1, 2, 3]
    for value, code in zip(raw.data, codes, strict=True):
        assert famepy.missing_type(db, "precision", value) == code


def test_numeric_boolean_date_string_series(db):
    numeric = np.array([1.5, SENTINELS.numeric_na], dtype=np.float32)
    do_write(series_object("nu", "numeric", MONTHLY, FIRST, numeric), db)
    boolean = np.array([1, 0, SENTINELS.boolean_nc], dtype=np.int32)
    do_write(series_object("bo", "boolean", MONTHLY, FIRST, boolean), db)
    dates = np.array([FIRST + 5, SENTINELS.index_nd], dtype=np.int64)
    do_write(series_object("da", "date", MONTHLY, FIRST, dates, date_frequency="monthly"), db)
    strings = [b"alpha", b"", SENTINELS.string_nc]
    do_write(series_object("st", "string", "case", 1, strings), db)

    got = read(db, "nu")
    assert got.data.dtype == np.float32
    assert classify_by_sentinel(got.data, "numeric", db.session.sentinels).tolist() == [0, 2]
    got = read(db, "bo")
    assert got.data.dtype == np.int32 and got.data.tolist() == boolean.tolist()
    assert classify_by_sentinel(got.data, "boolean", db.session.sentinels).tolist() == [0, 0, 1]
    got = read(db, "da")
    assert got.kind == "date" and got.date_frequency == MONTHLY
    assert got.data.dtype == np.int64 and got.data.tolist() == dates.tolist()
    assert famepy.quick_info(db, "da").kind == "date"
    assert famepy.quick_info(db, "da").type_label == "date:monthly"
    got = read(db, "st")
    assert got.data == strings and got.frequency == 232
    assert classify_by_sentinel(got.data, "string", db.session.sentinels).tolist() == [0, 0, 1]


def test_scalars_of_every_kind(db):
    do_write(scalar_object("p", "precision", 2.5), db)
    do_write(scalar_object("n", "numeric", 1.25), db)
    do_write(scalar_object("b", "boolean", 1), db)
    do_write(scalar_object("d", "date", FIRST, date_frequency=MONTHLY), db)
    do_write(scalar_object("s", "string", b"hello"), db)
    do_write(scalar_object("nl", "namelist", b"{A,B}"), db)
    assert (read(db, "p").kind, read(db, "p").data) == ("precision", 2.5)
    assert isinstance(read(db, "p").data, np.float64)
    assert (read(db, "n").kind, read(db, "n").data) == ("numeric", 1.25)
    assert isinstance(read(db, "n").data, np.float32)
    assert isinstance(read(db, "b").data, np.int32)
    assert isinstance(read(db, "d").data, np.int64)
    assert (read(db, "b").kind, read(db, "b").data) == ("boolean", 1)
    date = read(db, "d")
    assert (date.kind, date.date_frequency, date.data) == ("date", MONTHLY, FIRST)
    assert (read(db, "s").kind, read(db, "s").data) == ("string", b"hello")
    assert (read(db, "nl").kind, read(db, "nl").data) == ("namelist", b"{A,B}")
    for name in ("p", "n", "b", "d", "s", "nl"):
        obj = read(db, name)
        assert obj.is_scalar and obj.frequency == 0 and str(obj).endswith("undefined,0:0")
    info = famepy.quick_info(db, "nl")
    assert info.is_scalar and info.kind == "namelist" and info.length(SENTINELS.index_nc) == 0
    assert famepy.index_to_period(info.frequency, 0, database=db) == famepy.Period(0, 0)


def test_empty_series_round_trip(db):
    do_write(series_object("e", "precision", MONTHLY, 0, np.empty(0)), db)
    info = famepy.quick_info(db, "e")
    assert info.is_empty(SENTINELS.index_nc) and info.length(SENTINELS.index_nc) == 0
    assert info.range(SENTINELS.index_nc) is None
    raw = read(db, "e")
    assert raw.is_empty(SENTINELS.index_nc) and raw.last_index == SENTINELS.index_nc
    assert raw.first_index == SENTINELS.index_nc and len(raw.data) == 0
    # An object built with an unrelated range is read whole once its metadata agrees.
    assert len(famepy.do_read(famepy.FameObject("e", "series", "precision", MONTHLY), db).data) == 0
    with pytest.raises(DataValidationError):
        read(db, "e", first_index=0, last_index=0)


def test_subrange_reads(db):
    do_write(series_object("s", "precision", MONTHLY, FIRST, np.arange(6.0)), db)
    raw = read(db, "s", first_index=FIRST + 2, last_index=FIRST + 3)
    assert raw.data.tolist() == [2.0, 3.0] and raw.first_index == FIRST + 2
    with pytest.raises(DataValidationError):
        read(db, "s", first_index=FIRST - 1)
    with pytest.raises(DataValidationError):
        read(db, "s", last_index=FIRST + 6)
    with pytest.raises(DataValidationError):
        read(db, "s", first_index=FIRST + 3, last_index=FIRST + 2)


def test_replace_semantics(db):
    do_write(scalar_object("r", "precision", 1.0), db)
    with pytest.raises(HLIError) as error:
        do_write(scalar_object("r", "precision", 2.0), db, replace=False)
    assert error.value.status == S_EXISTS
    assert read(db, "r").data == 1.0
    do_write(scalar_object("r", "precision", 2.0), db)
    assert read(db, "r").data == 2.0
    do_write(scalar_object("new", "precision", 3.0), db, replace=True)
    assert read(db, "new").data == 3.0


def test_replace_failure_scope_is_documented_destructive(db):
    do_write(scalar_object("r", "precision", 1.0), db)
    db.session._native.fake.fail_next["cfmnwob"] = 999
    with pytest.raises(HLIError):
        do_write(scalar_object("r", "precision", 2.0), db)
    with pytest.raises(HLIError) as error:
        famepy.quick_info(db, "r")
    assert error.value.status == 13


def test_delete_object(db):
    do_write(scalar_object("d", "precision", 1.0), db)
    famepy.delete_object(db, "d")
    with pytest.raises(HLIError):
        famepy.delete_object(db, "d")
    famepy.delete_object(db, "d", missing_ok=True)


def test_writes_validate_buffers_without_conversion(db):
    fake = db.session._native.fake
    fake.calls.clear()
    with pytest.raises(DataValidationError, match="dtype"):
        do_write(
            series_object("x", "precision", MONTHLY, FIRST, np.array([1, 2], dtype=np.int64)), db
        )
    with pytest.raises(DataValidationError, match="dtype"):
        do_write(
            series_object("x", "precision", MONTHLY, FIRST, np.array([1.0], dtype=np.float32)), db
        )
    with pytest.raises(DataValidationError, match="endian"):
        do_write(series_object("x", "precision", MONTHLY, FIRST, np.array([1.0]).astype(">f8")), db)
    with pytest.raises(DataValidationError, match="contiguous"):
        do_write(series_object("x", "precision", MONTHLY, FIRST, np.arange(6.0)[::2]), db)
    with pytest.raises(DataValidationError, match="one-dimensional"):
        do_write(series_object("x", "precision", MONTHLY, FIRST, np.zeros((2, 2))), db)
    with pytest.raises(DataValidationError, match="overflow"):
        do_write(series_object("x", "precision", MONTHLY, 2**63 - 1, np.zeros(2)), db)
    with pytest.raises(DataValidationError):
        do_write(series_object("x", "boolean", MONTHLY, FIRST, np.array([True, False])), db)
    with pytest.raises(DataValidationError):
        do_write(series_object("x", "string", MONTHLY, FIRST, ["text"]), db)
    with pytest.raises(DataValidationError):
        do_write(scalar_object("x", "boolean", True), db)
    with pytest.raises(DataValidationError):
        do_write(scalar_object("x", "string", "text"), db)
    with pytest.raises(DataValidationError):
        do_write(series_object("u", "precision", "undefined", FIRST, np.zeros(1)), db)
    # The FameObject range must agree with its data, and a scalar has no frequency.
    with pytest.raises(DataValidationError, match="last index"):
        do_write(
            famepy.FameObject("x", "series", "precision", MONTHLY, FIRST, FIRST + 5, np.zeros(2)),
            db,
        )
    with pytest.raises(DataValidationError, match="first index"):
        do_write(famepy.FameObject("x", "series", "precision", MONTHLY, data=np.zeros(2)), db)
    with pytest.raises(DataValidationError, match="undefined frequency"):
        do_write(famepy.FameObject("x", "scalar", "precision", MONTHLY, data=1.0), db)
    with pytest.raises(DataValidationError, match="no data"):
        do_write(famepy.FameObject("x", "series", "precision", MONTHLY), db)
    with pytest.raises(DataValidationError, match="typed by the frequency"):
        do_write(famepy.FameObject("x", "scalar", "date", "undefined", data=5), db)
    with pytest.raises(DataValidationError):
        do_write(famepy.FameObject("x", "series", "namelist", MONTHLY, FIRST, data=[b"{A}"]), db)
    with pytest.raises(famepy.UnsupportedOperationError):
        do_write(famepy.FameObject("x", "formula", "precision", "undefined", data=1.0), db)
    with pytest.raises(TypeError):
        do_write(1.0, db)
    with pytest.raises(TypeError):
        do_write(scalar_object("x", "precision", 1.0), object())
    # The raw carriers refuse the same inputs on their own.
    with pytest.raises(DataValidationError):
        RawSeries("string", MONTHLY, FIRST, ["text"])
    with pytest.raises(DataValidationError):
        RawScalar("date", 5)
    with pytest.raises(DataValidationError):
        RawScalar("boolean", True)
    with pytest.raises(DataValidationError):
        RawScalar("string", "text")
    with pytest.raises(DataValidationError):
        RawScalar("precision", 1.0, date_frequency=MONTHLY)
    assert fake.calls == []


def test_readonly_database_refuses_writes(session, tmp_path):
    path = tmp_path / "ro.db"
    famepy.closedb(famepy.opendb(path, "create", session=session))
    with famepy.opendb(path, session=session) as database:
        with pytest.raises(DataValidationError, match="read-only"):
            do_write(scalar_object("x", "precision", 1.0), database)


def test_type_mismatch_and_unsupported_class(db):
    do_write(scalar_object("p", "precision", 1.0), db)
    db.session._native.fake.handles[db.key].objects["P"].type_code = 1
    got = read(db, "p")
    assert got.kind == "numeric"
    db.session._native.fake.handles[db.key].objects["P"].class_code = int(ObjectClass.FORMULA)
    with pytest.raises(UnsupportedOperationError):
        read(db, "p")
    db.session._native.fake.handles[db.key].objects["P"].class_code = 2
    db.session._native.fake.fail_next["fame_get_numerics"] = S_TYPE_MISMATCH
    with pytest.raises(HLIError):
        read(db, "p")


def test_names_are_ascii_only(db):
    with pytest.raises(famepy.TextEncodingError):
        do_write(scalar_object("cafÃ©", "precision", 1.0), db)
    with pytest.raises(famepy.TextEncodingError):
        famepy.quick_info(db, "a\0b")
    do_write(scalar_object(b"raw_bytes", "precision", 1.0), db)
    assert famepy.quick_info(db, b"raw_bytes").name_text == "raw_bytes"


def test_missing_classification_rejects_unknown_kinds(db):
    with pytest.raises(ValueError):
        famepy.missing_type(db, "namelist", b"x")
    with pytest.raises(ValueError):
        classify_by_sentinel(np.zeros(1), "unknown", db.session.sentinels)


def test_sentinel_substitution_helpers(db):
    from famepy._data import sentinel_value

    sentinels = db.session.sentinels
    assert sentinel_value("boolean", 1, sentinels) == sentinels.boolean_nc
    assert sentinel_value("date", 2, sentinels) == sentinels.index_na
    assert sentinel_value("string", 3, sentinels) == sentinels.string_nd != b"ND"
    with pytest.raises(ValueError):
        sentinel_value("precision", 0, sentinels)


def test_period_conversion_through_library(db):
    period = famepy.index_to_period(MONTHLY, FIRST + 4, database=db)
    assert period == famepy.Period(2020, 5) and str(period) == "2020:5"
    assert famepy.period_to_index(MONTHLY, period, database=db) == FIRST + 4
    assert famepy.index_to_period(232, 9, database=db) == famepy.Period(0, 9)
    assert famepy.period_to_index(232, famepy.Period(0, 9), database=db) == 9


def test_invalid_inputs_make_no_native_call_and_keep_existing_objects(db):
    fake = db.session._native.fake
    do_write(scalar_object("keep", "precision", 7.0), db)
    fake.calls.clear()
    with pytest.raises(DataValidationError):
        do_write(scalar_object("keep", "boolean", 2**40), db, replace=True)
    with pytest.raises(DataValidationError):
        do_write(scalar_object("keep", "precision", 10**400), db, replace=True)
    with pytest.raises(DataValidationError):
        do_write(scalar_object("keep", "string", b"a\0b"), db, replace=True)
    with pytest.raises(DataValidationError):
        do_write(scalar_object("keep", "namelist", b"{A\0}"), db, replace=True)
    with pytest.raises(DataValidationError):
        do_write(scalar_object("keep", "date", 2**63, date_frequency=MONTHLY), db, replace=True)
    with pytest.raises(DataValidationError):
        do_write(series_object("keep", "string", 232, 1, [b"ok", b"a\0b"]), db, replace=True)
    strings = series_object("keep", "string", 232, 1, [b"ok"])
    strings.data.append(b"a\0b")
    with pytest.raises(DataValidationError):
        do_write(strings, db, replace=True)
    numbers = series_object("keep", "precision", MONTHLY, FIRST, np.zeros(3))
    numbers.last_index = FIRST + 1  # the object disagrees with its own data
    with pytest.raises(DataValidationError, match="last index"):
        do_write(numbers, db, replace=True)
    with pytest.raises(DataValidationError):
        famepy.FameObject("keep", "scalar", "precision", "undefined", 1.5)
    with pytest.raises(ValueError):
        famepy.FameObject("", "scalar", "precision", "undefined")
    assert fake.calls == []
    assert (read(db, "keep").kind, read(db, "keep").data) == ("precision", 7.0)


def test_numeric_scalars_preserve_float32_bits(db):
    payload = np.array([0x7F800101], dtype=np.uint32).view(np.float32)[0]
    do_write(scalar_object("sn", "numeric", payload), db)
    raw = read(db, "sn")
    assert isinstance(raw.data, np.float32)
    assert int(np.array([raw.data]).view(np.uint32)[0]) == 0x7F800101
    do_write(
        series_object("ns", "numeric", MONTHLY, FIRST, np.array([payload], dtype=np.float32)), db
    )
    assert int(read(db, "ns").data.view(np.uint32)[0]) == 0x7F800101
    assert famepy.missing_type(db, "numeric", raw.data) == 0
    sentinel = db.session.sentinels.numeric_nc
    do_write(scalar_object("ncs", "numeric", sentinel), db)  # "nc" is a reserved name
    back = read(db, "ncs").data
    assert famepy.missing_type(db, "numeric", back) == 1
    assert classify_by_sentinel(np.array([back]), "numeric", db.session.sentinels).tolist() == [1]


def test_read_re_queries_metadata_inside_the_locked_read(db):
    fake = db.session._native.fake
    do_write(series_object("s", "precision", MONTHLY, FIRST, np.arange(3.0)), db)
    stale = famepy.quick_info(db, "s")
    famepy.delete_object(db, "s")
    do_write(scalar_object("s", "string", b"now a scalar"), db)
    fake.calls.clear()
    # The object changed since it was described: the read refuses instead of
    # reading a string scalar into a precision series, and leaves it untouched.
    with pytest.raises(DataValidationError, match="quick_info"):
        famepy.do_read(stale, db)
    assert fake.calls == ["fame_quick_info"]
    assert stale.data is None and stale.is_series and stale.first_index == FIRST
    fresh = famepy.do_read(famepy.quick_info(db, "s"), db)
    assert (fresh.kind, fresh.data) == ("string", b"now a scalar")
    with pytest.raises(DataValidationError):
        read(db, "s", first_index=1.5)
    with pytest.raises(TypeError):
        famepy.do_read("s", db)
    with pytest.raises(TypeError):
        famepy.do_read(fresh, "s")
