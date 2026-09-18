# SPDX-License-Identifier: MIT
"""Binding mechanics against the independent C shim (not FAME evidence)."""

import ctypes as ct
import os
from pathlib import Path

import numpy as np
import pytest
from tsecon import TSeries, mm

import famepy
from famepy import FameError, SymbolNotFoundError, bridge, diagnose
from famepy._abi import C, FameRange, layout
from famepy._binding import Binding
from famepy._constants import FREQUENCY_MONTHLY, NAME_CAPACITY
from famepy._native import CtypesNative, RangeSpec
from famepy._runtime import ExtendedErrorRetrieval, Session

pytestmark = pytest.mark.native_shim


@pytest.fixture(scope="module")
def library():
    configured = os.environ.get("FAMEPY_TEST_SHIM")
    if not configured:
        if os.environ.get("FAMEPY_REQUIRE_SHIM") == "1":
            pytest.fail("Required C shim is not configured.")
        pytest.skip("Set FAMEPY_TEST_SHIM to run independent C-library tests.")
    path = Path(configured)
    assert path.is_absolute() and path.is_file(), "Configured C shim is missing or not absolute"
    return ct.CDLL(str(path))


def helper(library, name, restype=ct.c_int32, argtypes=()):
    function = getattr(library, name)
    function.argtypes = list(argtypes)
    function.restype = restype
    return function


@pytest.fixture
def native(library):
    helper(library, "shim_reset", None)()
    return CtypesNative(library)


@pytest.fixture
def session(native):
    owner = Session(native=native).initialize()
    yield owner
    if owner.state in ("initialized", "broken"):
        owner.finalize()


@pytest.fixture
def db(session):
    database = famepy.open_database("synthetic-shim.db", "create", session=session)
    yield database
    database.close()


def test_layout_against_compiler(library):
    values = layout()
    for name, key in [
        ("shim_range_size", "range_bytes"),
        ("shim_start_offset", "start_offset"),
        ("shim_end_offset", "end_offset"),
    ]:
        assert helper(library, name)() == values[key]


def test_status_pointer_and_return(native, library):
    binding = native.binding
    key = ct.c_int32()
    with pytest.raises(FameError) as error:
        binding.call("cfmopdb", ct.byref(key), b"synthetic", 1)
    assert error.value.status == 901
    binding.call("cfmini")
    version = ct.c_float()
    binding.call("cfmver", ct.byref(version))
    assert version.value == 4.25
    binding.call("cfmopdb", ct.byref(key), b"synthetic", 2)
    assert key.value >= 0
    with pytest.raises(FameError) as error:
        binding.call("cfmopdb", ct.byref(key), b"synthetic", 9)
    assert error.value.status == 910
    assert binding.call_status("cfmfin") == 0


def test_index_output_really_64bit(native):
    class Guarded(ct.Structure):
        _fields_ = [("before", ct.c_int64), ("value", ct.c_int64), ("after", ct.c_int64)]

    output = Guarded(91, 0, 92)
    pointer = ct.cast(ct.byref(output, Guarded.value.offset), ct.POINTER(ct.c_int64))
    native.binding.call("fame_year_period_to_index", 3, pointer, 2020, 3)
    assert output.value == 2020 * 1000 + 3
    assert (output.before, output.after) == (91, 92)
    assert native.year_period_to_index(FREQUENCY_MONTHLY, 2020, 3) == 2020 * 12 + 2
    assert native.index_to_year_period(FREQUENCY_MONTHLY, 2020 * 12 + 2) == (2020, 3)


def test_lifetime_counters_and_fault_injection(native, library):
    session = Session(native=native).initialize()
    assert helper(library, "shim_initialized")() == 1
    assert session.version() == 4.25
    helper(library, "shim_fail_next", None, [ct.c_int32])(77)
    with pytest.raises(FameError) as error:
        famepy.work_database(session=session)
    assert error.value.status == 77
    work = famepy.work_database(session=session)
    assert helper(library, "shim_open_databases")() == 1
    session.finalize()
    assert helper(library, "shim_initialized")() == 0
    assert helper(library, "shim_open_databases")() == 0
    assert not work.is_open
    assert helper(library, "shim_fin_count")() == 1


def test_globals_are_read_with_declared_types(session):
    sentinels = session.sentinels
    assert sentinels.string_nc == b"NC" and sentinels.string_nd == b"ND"
    assert sentinels.index_nc != sentinels.index_na != sentinels.index_nd
    assert np.isnan(sentinels.precision_nc) and np.isnan(sentinels.numeric_na)
    assert np.array(sentinels.precision_nc).view(np.uint64) != np.array(
        sentinels.precision_na
    ).view(np.uint64)
    assert sentinels.boolean_nc == -2147483647


def test_bulk_numpy_buffers_and_scalar_range(db, native):
    values = np.array([1.5, 2.5, 3.5, 4.5])
    famepy.write_object(db, "s", famepy.series("precision", FREQUENCY_MONTHLY, 100, values))
    out = np.full(6, -99.0)
    view = out[1:5]
    native.get_precisions(db.key, b"S", RangeSpec(FREQUENCY_MONTHLY, 100, 103), view)
    assert out.tolist() == [-99, 1.5, 2.5, 3.5, 4.5, -99]
    raw = famepy.read_object(db, "s", first_index=101, last_index=102)
    assert raw.values.tolist() == [2.5, 3.5]
    famepy.write_object(db, "sc", famepy.scalar("precision", 12.5))
    scalar = np.empty(1)
    native.get_precisions(db.key, b"SC", None, scalar)
    assert scalar[0] == 12.5
    with pytest.raises(FameError) as error:
        native.get_precisions(db.key, b"S", RangeSpec(FREQUENCY_MONTHLY, 90, 91), np.empty(2))
    assert error.value.status == 908
    with pytest.raises(famepy.DataValidationError):
        native.get_precisions(db.key, b"S", RangeSpec(FREQUENCY_MONTHLY, 100, 103), np.empty(3))
    with pytest.raises(famepy.DataValidationError):
        native.get_precisions(
            db.key, b"S", RangeSpec(FREQUENCY_MONTHLY, 100, 103), np.empty(4, dtype=np.float32)
        )


def test_every_kind_round_trips_through_the_shim(db, session):
    sentinels = session.sentinels
    precision = np.array(
        [1.0, sentinels.precision_nc, sentinels.precision_na, sentinels.precision_nd]
    )
    numeric = np.array([2.0, sentinels.numeric_nc], dtype=np.float32)
    boolean = np.array([1, 0, sentinels.boolean_nd], dtype=np.int32)
    dates = np.array([24240, sentinels.index_na], dtype=np.int64)
    strings = [b"alpha", b"", b"NC"]
    famepy.write_object(db, "p", famepy.series("precision", "monthly", 10, precision))
    famepy.write_object(db, "n", famepy.series("numeric", "monthly", 10, numeric))
    famepy.write_object(db, "b", famepy.series("boolean", "monthly", 10, boolean))
    famepy.write_object(db, "d", famepy.series("date", "monthly", 10, dates, date_frequency=129))
    famepy.write_object(db, "s", famepy.series("string", "case", 1, strings))
    famepy.write_object(db, "nl", famepy.scalar("namelist", b"{A,B}"))
    famepy.write_object(db, "ss", famepy.scalar("string", b"hello world"))
    famepy.write_object(db, "ds", famepy.scalar("date", 5, date_frequency=129))
    famepy.write_object(db, "e", famepy.series("precision", "monthly", 0, np.empty(0)))
    db.post()
    db.close()
    with famepy.open_database("synthetic-shim.db", "readonly", session=session) as reopened:
        got = famepy.read_object(reopened, "p")
        assert np.array_equal(got.values.view(np.uint64), precision.view(np.uint64))
        assert famepy.classify_by_sentinel(got.values, "precision", sentinels).tolist() == [
            0,
            1,
            2,
            3,
        ]
        assert [famepy.missing_type(reopened, "precision", v) for v in got.values] == [0, 1, 2, 3]
        assert (
            famepy.read_object(reopened, "n").values.view(np.uint32).tolist()
            == numeric.view(np.uint32).tolist()
        )
        assert famepy.read_object(reopened, "b").values.tolist() == boolean.tolist()
        assert famepy.read_object(reopened, "d").values.tolist() == dates.tolist()
        assert famepy.missing_type(reopened, "date", int(dates[1])) == 2
        assert famepy.read_object(reopened, "s").values == strings
        assert famepy.missing_type(reopened, "string", b"NC") == 1
        assert famepy.read_object(reopened, "nl").value == b"{A,B}"
        assert famepy.read_object(reopened, "ss").value == b"hello world"
        assert famepy.read_object(reopened, "ds") == famepy.RawScalar("date", 5, 129)
        empty = famepy.read_object(reopened, "e")
        assert empty.is_empty and famepy.quick_info(reopened, "e").is_empty(sentinels.index_nc)
        with pytest.raises(famepy.DataValidationError):
            famepy.write_object(reopened, "x", famepy.scalar("precision", 1.0))


def test_close_without_post_discards_in_shim(session):
    database = famepy.open_database("discard.db", "create", session=session)
    famepy.write_object(database, "kept", famepy.scalar("precision", 1.0))
    database.post()
    famepy.write_object(database, "lost", famepy.scalar("precision", 2.0))
    database.close()
    with famepy.open_database("discard.db", session=session) as reopened:
        assert [i.name_text for i in famepy.list_objects(reopened)] == ["KEPT"]


def test_writable_string_pointer_array(db, native):
    famepy.write_object(db, "s", famepy.series("string", "case", 1, [b"abc", b"xy"]))
    left, right = ct.create_string_buffer(4), ct.create_string_buffer(3)
    pointers = (C * 2)(ct.cast(left, C), ct.cast(right, C))
    lengths = (ct.c_int32 * 2)(3, 2)
    range_ = FameRange(232, 1, 2)
    native.binding.call("fame_get_strings", db.key, b"S", ct.byref(range_), pointers, lengths, None)
    assert (left.value, right.value) == (b"abc", b"xy")
    assert list(lengths) == [3, 2]
    assert native.get_strings(db.key, b"S", RangeSpec(232, 1, 2), 2) == [b"abc", b"xy"]
    with pytest.raises(famepy.DataValidationError):
        native.write_strings(db.key, b"S", RangeSpec(232, 1, 2), [b"a\0b", b"c"])


def test_wildcards_truncation_and_cursor_cleanup(db, library):
    long_name = "L" * NAME_CAPACITY
    famepy.write_object(db, long_name, famepy.scalar("precision", 1.0))
    famepy.write_object(db, "sales_a", famepy.series("precision", "monthly", 0, np.zeros(1)))
    famepy.write_object(db, "sales_b", famepy.scalar("numeric", 1.0))
    names = [i.name_text for i in famepy.list_objects(db)]
    assert names == [long_name, "SALES_A", "SALES_B"]
    assert [i.name_text for i in famepy.list_objects(db, "sales?")] == ["SALES_A", "SALES_B"]
    assert [i.name_text for i in famepy.list_objects(db, "sales_^")] == ["SALES_A", "SALES_B"]
    assert [i.name_text for i in famepy.list_objects(db, classes="series")] == ["SALES_A"]
    assert [i.name_text for i in famepy.list_objects(db, types="numeric")] == ["SALES_B"]
    assert [i.name_text for i in famepy.list_objects(db, frequencies="monthly")] == ["SALES_A"]
    scalar_info = famepy.list_objects(db, "sales_b")[0]
    assert (scalar_info.first_index, scalar_info.last_index) == (0, 0)
    with pytest.raises(famepy.NameTruncatedError) as error:
        famepy.list_objects(db, capacity=8)
    assert error.value.returned_length == NAME_CAPACITY
    assert helper(library, "shim_active_cursors")() == 0


def test_commands_output_and_cleanup(session, library, tmp_path):
    output = famepy.run_command("display 1", session=session, temp_dir=tmp_path)
    assert output == b"echo: display 1\n"
    assert famepy.run_command("display 2+2", session=session, temp_dir=tmp_path) == b"4\n"
    assert helper(library, "shim_output_redirected")() == 0
    with pytest.raises(famepy.CommandError) as error:
        famepy.run_command("fail 513", session=session, temp_dir=tmp_path)
    assert error.value.status == 513
    assert error.value.output == b"partial output before failure\n"
    assert helper(library, "shim_output_redirected")() == 0
    assert list(tmp_path.iterdir()) == []


def test_extended_error_mechanics_with_shim_declared_lengths(session, library, tmp_path):
    """The shim declares its own cfmlerr; this checks retrieval mechanics only."""
    length_function = helper(library, "cfmlerr", None, [ct.POINTER(ct.c_int32)] * 2)

    def query_length(native):
        status, length = ct.c_int32(-1), ct.c_int32(-1)
        length_function(ct.byref(status), ct.byref(length))
        famepy.check_status(status.value)
        return length.value

    def fetch(native, buffer):
        native.binding.call("cfmferr", ct.cast(buffer, C))

    session.extended_error_retrieval = ExtendedErrorRetrieval(query_length, fetch)
    with pytest.raises(famepy.RuntimeStateError):
        session.extended_error_text()
    with pytest.raises(famepy.CommandError) as error:
        famepy.run_command("fail", session=session, temp_dir=tmp_path)
    assert error.value.extended_text == b"synthetic failure for fail"
    assert session.extended_error_text() == b"synthetic failure for fail"
    assert helper(library, "shim_output_redirected")() == 0


def test_bridge_round_trip_through_shim(session):
    ts = TSeries(mm(2020, 1), [1.0, np.nan, 3.0])
    bridge.write_tseries("bridge.db", "ts", ts, mode="create")
    back = bridge.read_tseries("bridge.db", "ts")
    assert back.firstdate == mm(2020, 1)
    assert np.array_equal(back.values, ts.values, equal_nan=True)
    bridge.write_scalar("bridge.db", "sc", 2.0, mode="update")
    assert bridge.read_scalar("bridge.db", "sc") == 2.0
    assert session.open_databases == ()


def test_probe_reports_presence_only_symbol(library):
    report = diagnose(os.environ["FAMEPY_TEST_SHIM"], probe=True)
    assert report["status"] == "symbols_found"
    assert report["presence_only"] == {"cfmlerr": True}
    assert report["globals"]["FSTRND"] is True
    assert report["abi_verified"] is False
    assert report["native_calls_executed"] is False


def test_missing_function_is_package_error(library, monkeypatch):
    class Partial:
        def __getattr__(self, name):
            if name == "cfmini":
                raise AttributeError(name)
            return getattr(library, name)

    with pytest.raises(SymbolNotFoundError) as error:
        Binding(Partial()).call("cfmini")
    assert error.value.symbol == "cfmini"
    assert os.environ["FAMEPY_TEST_SHIM"] not in str(error.value)
    assert error.value.__suppress_context__


def test_numeric_scalar_bits_survive_the_shim(db, session):
    values = np.array([0x7F800101], dtype=np.uint32).view(np.float32)
    famepy.write_object(db, "array", famepy.series("numeric", "case", 1, values))
    famepy.write_object(db, "scalar", famepy.RawScalar("numeric", values[0]))
    assert int(famepy.read_object(db, "array").values.view(np.uint32)[0]) == 0x7F800101
    raw = famepy.read_object(db, "scalar")
    assert isinstance(raw.value, np.float32)
    assert int(np.array([raw.value]).view(np.uint32)[0]) == 0x7F800101
    assert famepy.missing_type(db, "numeric", raw.value) == 0
    assert famepy.missing_type(db, "numeric", session.sentinels.numeric_na) == 2
    assert isinstance(session.sentinels.numeric_nc, np.float32)


def test_broken_owner_blocks_a_second_wrapper_on_the_same_library(native, library):
    first = Session(native=native).initialize()
    helper(library, "shim_fail_next", None, [ct.c_int32])(55)
    with pytest.raises(FameError):
        first.finalize()
    assert first.state == "broken" and helper(library, "shim_initialized")() == 1
    with pytest.raises(famepy.RuntimeStateError):
        Session(native=CtypesNative(library)).initialize()
    assert helper(library, "shim_init_count")() == 1
    first.finalize()
    assert helper(library, "shim_initialized")() == 0
