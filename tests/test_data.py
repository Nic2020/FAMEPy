# SPDX-License-Identifier: MIT
import numpy as np
import pytest
from fake_native import S_EXISTS, S_TYPE_MISMATCH, SENTINELS

import famepy
from famepy import (
    DataValidationError,
    FameError,
    RawScalar,
    RawSeries,
    UnsupportedOperationError,
    classify_by_sentinel,
    read_object,
    scalar,
    series,
    write_object,
)
from famepy._constants import FREQUENCY_MONTHLY, ObjectClass, ObjectType

MONTHLY = FREQUENCY_MONTHLY
FIRST = 2020 * 12


def test_precision_series_round_trip_preserves_missing_codes(db):
    values = np.array([1.0, SENTINELS.precision_nc, SENTINELS.precision_na, SENTINELS.precision_nd])
    original = values.copy()
    write_object(db, "ps", series("precision", MONTHLY, FIRST, values))
    assert np.array_equal(values.view(np.uint64), original.view(np.uint64))
    info = famepy.quick_info(db, "ps")
    assert (info.class_code, info.type_code, info.frequency) == (1, ObjectType.PRECISION, MONTHLY)
    assert (info.first_index, info.last_index) == (FIRST, FIRST + 3)
    assert str(info) == "ps: series,precision,monthly,24240:24243"
    raw = read_object(db, "ps")
    assert isinstance(raw, RawSeries) and raw.kind == "precision"
    assert raw.values.dtype == np.float64
    assert np.array_equal(raw.values.view(np.uint64), original.view(np.uint64))
    assert raw.values is not values
    codes = classify_by_sentinel(raw.values, "precision", db.session.sentinels)
    assert codes.tolist() == [0, 1, 2, 3]
    for value, code in zip(raw.values, codes, strict=True):
        assert famepy.missing_type(db, "precision", value) == code


def test_numeric_boolean_date_string_series(db):
    numeric = np.array([1.5, SENTINELS.numeric_na], dtype=np.float32)
    write_object(db, "nu", series("numeric", MONTHLY, FIRST, numeric))
    boolean = np.array([1, 0, SENTINELS.boolean_nc], dtype=np.int32)
    write_object(db, "bo", series("boolean", MONTHLY, FIRST, boolean))
    dates = np.array([FIRST + 5, SENTINELS.index_nd], dtype=np.int64)
    write_object(db, "da", series("date", MONTHLY, FIRST, dates, date_frequency="monthly"))
    strings = [b"alpha", b"", SENTINELS.string_nc]
    write_object(db, "st", series("string", "case", 1, strings))

    got = read_object(db, "nu")
    assert got.values.dtype == np.float32
    assert classify_by_sentinel(got.values, "numeric", db.session.sentinels).tolist() == [0, 2]
    got = read_object(db, "bo")
    assert got.values.dtype == np.int32 and got.values.tolist() == boolean.tolist()
    assert classify_by_sentinel(got.values, "boolean", db.session.sentinels).tolist() == [0, 0, 1]
    got = read_object(db, "da")
    assert got.kind == "date" and got.date_frequency == MONTHLY
    assert got.values.dtype == np.int64 and got.values.tolist() == dates.tolist()
    assert famepy.quick_info(db, "da").kind == "date"
    assert famepy.quick_info(db, "da").type_label == "date:monthly"
    got = read_object(db, "st")
    assert got.values == strings and got.frequency == 232
    assert classify_by_sentinel(got.values, "string", db.session.sentinels).tolist() == [0, 0, 1]


def test_scalars_of_every_kind(db):
    write_object(db, "p", scalar("precision", 2.5))
    write_object(db, "n", scalar("numeric", 1.25))
    write_object(db, "b", scalar("boolean", 1))
    write_object(db, "d", scalar("date", FIRST, date_frequency=MONTHLY))
    write_object(db, "s", scalar("string", b"hello"))
    write_object(db, "nl", scalar("namelist", b"{A,B}"))
    assert read_object(db, "p") == RawScalar("precision", 2.5)
    assert isinstance(read_object(db, "p").value, np.float64)
    assert read_object(db, "n") == RawScalar("numeric", 1.25)
    assert isinstance(read_object(db, "n").value, np.float32)
    assert isinstance(read_object(db, "b").value, np.int32)
    assert isinstance(read_object(db, "d").value, np.int64)
    assert read_object(db, "b") == RawScalar("boolean", 1)
    assert read_object(db, "d") == RawScalar("date", FIRST, MONTHLY)
    assert read_object(db, "s") == RawScalar("string", b"hello")
    assert read_object(db, "nl") == RawScalar("namelist", b"{A,B}")
    info = famepy.quick_info(db, "nl")
    assert info.is_scalar and info.kind == "namelist" and info.length(SENTINELS.index_nc) == 0
    assert famepy.index_to_period(info.frequency, 0, database=db) == famepy.Period(0, 0)


def test_empty_series_round_trip(db):
    write_object(db, "e", series("precision", MONTHLY, 0, np.empty(0)))
    info = famepy.quick_info(db, "e")
    assert info.is_empty(SENTINELS.index_nc) and info.length(SENTINELS.index_nc) == 0
    assert info.range(SENTINELS.index_nc) is None
    raw = read_object(db, "e")
    assert raw.is_empty and raw.last_index is None and raw.first_index == SENTINELS.index_nc
    empty_strings = read_object(db, "e")
    assert len(empty_strings) == 0
    with pytest.raises(DataValidationError):
        read_object(db, "e", first_index=0, last_index=0)


def test_subrange_reads(db):
    write_object(db, "s", series("precision", MONTHLY, FIRST, np.arange(6.0)))
    raw = read_object(db, "s", first_index=FIRST + 2, last_index=FIRST + 3)
    assert raw.values.tolist() == [2.0, 3.0] and raw.first_index == FIRST + 2
    with pytest.raises(DataValidationError):
        read_object(db, "s", first_index=FIRST - 1)
    with pytest.raises(DataValidationError):
        read_object(db, "s", last_index=FIRST + 6)
    with pytest.raises(DataValidationError):
        read_object(db, "s", first_index=FIRST + 3, last_index=FIRST + 2)


def test_replace_semantics(db):
    write_object(db, "r", scalar("precision", 1.0))
    with pytest.raises(FameError) as error:
        write_object(db, "r", scalar("precision", 2.0))
    assert error.value.status == S_EXISTS
    assert read_object(db, "r").value == 1.0
    write_object(db, "r", scalar("precision", 2.0), replace=True)
    assert read_object(db, "r").value == 2.0
    write_object(db, "new", scalar("precision", 3.0), replace=True)
    assert read_object(db, "new").value == 3.0


def test_replace_failure_scope_is_documented_destructive(db):
    write_object(db, "r", scalar("precision", 1.0))
    db.session._native.fake.fail_next["cfmnwob"] = 999
    with pytest.raises(FameError):
        write_object(db, "r", scalar("precision", 2.0), replace=True)
    with pytest.raises(FameError) as error:
        famepy.quick_info(db, "r")
    assert error.value.status == 13


def test_delete_object(db):
    write_object(db, "d", scalar("precision", 1.0))
    famepy.delete_object(db, "d")
    with pytest.raises(FameError):
        famepy.delete_object(db, "d")
    famepy.delete_object(db, "d", missing_ok=True)


def test_writes_validate_buffers_without_conversion(db):
    with pytest.raises(DataValidationError, match="dtype"):
        series("precision", MONTHLY, FIRST, np.array([1, 2], dtype=np.int64))
    with pytest.raises(DataValidationError, match="dtype"):
        series("precision", MONTHLY, FIRST, np.array([1.0], dtype=np.float32))
    with pytest.raises(DataValidationError, match="endian"):
        series("precision", MONTHLY, FIRST, np.array([1.0]).astype(">f8"))
    with pytest.raises(DataValidationError, match="contiguous"):
        series("precision", MONTHLY, FIRST, np.arange(6.0)[::2])
    with pytest.raises(DataValidationError, match="one-dimensional"):
        series("precision", MONTHLY, FIRST, np.zeros((2, 2)))
    with pytest.raises(DataValidationError, match="overflow"):
        series("precision", MONTHLY, 2**63 - 1, np.zeros(2))
    with pytest.raises(DataValidationError):
        series("boolean", MONTHLY, FIRST, np.array([True, False]))
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
    with pytest.raises(DataValidationError):
        write_object(db, "u", series("precision", "undefined", FIRST, np.zeros(1)))
    with pytest.raises(TypeError):
        write_object(db, "u", 1.0)


def test_readonly_database_refuses_writes(session, tmp_path):
    path = tmp_path / "ro.db"
    famepy.open_database(path, "create", session=session).close()
    with famepy.open_database(path, session=session) as database:
        with pytest.raises(DataValidationError, match="read-only"):
            write_object(database, "x", scalar("precision", 1.0))


def test_type_mismatch_and_unsupported_class(db):
    write_object(db, "p", scalar("precision", 1.0))
    db.session._native.fake.handles[db.key].objects["P"].type_code = 1
    got = read_object(db, "p")
    assert got.kind == "numeric"
    db.session._native.fake.handles[db.key].objects["P"].class_code = int(ObjectClass.FORMULA)
    with pytest.raises(UnsupportedOperationError):
        read_object(db, "p")
    db.session._native.fake.handles[db.key].objects["P"].class_code = 2
    db.session._native.fake.fail_next["fame_get_numerics"] = S_TYPE_MISMATCH
    with pytest.raises(FameError):
        read_object(db, "p")


def test_names_are_ascii_only(db):
    with pytest.raises(famepy.TextEncodingError):
        write_object(db, "café", scalar("precision", 1.0))
    with pytest.raises(famepy.TextEncodingError):
        famepy.quick_info(db, "a\0b")
    write_object(db, b"raw_bytes", scalar("precision", 1.0))
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
    write_object(db, "keep", scalar("precision", 7.0))
    fake.calls.clear()
    with pytest.raises(DataValidationError):
        scalar("boolean", 2**40)
    with pytest.raises(DataValidationError):
        scalar("precision", 10**400)
    with pytest.raises(DataValidationError):
        scalar("string", b"a\0b")
    with pytest.raises(DataValidationError):
        scalar("namelist", b"{A\0}")
    with pytest.raises(DataValidationError):
        scalar("date", 2**63, date_frequency=MONTHLY)
    with pytest.raises(DataValidationError):
        RawSeries("string", 232, 1, [b"ok", b"a\0b"])
    strings = RawSeries("string", 232, 1, [b"ok"])
    strings.values.append(b"a\0b")
    with pytest.raises(DataValidationError):
        write_object(db, "keep", strings, replace=True)
    numbers = series("precision", MONTHLY, FIRST, np.zeros(3))
    numbers.values.resize(2, refcheck=False)
    with pytest.raises(DataValidationError, match="length"):
        write_object(db, "keep", numbers, replace=True)
    assert fake.calls == []
    assert read_object(db, "keep") == RawScalar("precision", 7.0)


def test_numeric_scalars_preserve_float32_bits(db):
    payload = np.array([0x7F800101], dtype=np.uint32).view(np.float32)[0]
    write_object(db, "sn", RawScalar("numeric", payload))
    raw = read_object(db, "sn")
    assert isinstance(raw.value, np.float32)
    assert int(np.array([raw.value]).view(np.uint32)[0]) == 0x7F800101
    write_object(db, "ns", series("numeric", MONTHLY, FIRST, np.array([payload], dtype=np.float32)))
    assert int(read_object(db, "ns").values.view(np.uint32)[0]) == 0x7F800101
    assert famepy.missing_type(db, "numeric", raw.value) == 0
    sentinel = db.session.sentinels.numeric_nc
    write_object(db, "ncs", RawScalar("numeric", sentinel))  # "nc" is a reserved name
    back = read_object(db, "ncs").value
    assert famepy.missing_type(db, "numeric", back) == 1
    assert classify_by_sentinel(np.array([back]), "numeric", db.session.sentinels).tolist() == [1]


def test_read_re_queries_metadata_inside_the_locked_read(db):
    fake = db.session._native.fake
    write_object(db, "s", series("precision", MONTHLY, FIRST, np.arange(3.0)))
    stale = famepy.quick_info(db, "s")
    famepy.delete_object(db, "s")
    write_object(db, "s", scalar("string", b"now a scalar"))
    fake.calls.clear()
    got = read_object(db, stale)
    assert got == RawScalar("string", b"now a scalar")
    assert fake.calls[0] == "fame_quick_info"
    with pytest.raises(DataValidationError):
        read_object(db, "s", first_index=1.5)
