# SPDX-License-Identifier: MIT
"""Binding mechanics against the independent C shim (not FAME evidence)."""

import ctypes as ct
import os
from pathlib import Path

import numpy as np
import pytest
from canonical import read, scalar_object, series_object, value, write
from tsecon import TSeries, mm

import famepy
from famepy import HLIError, SymbolNotFoundError, diagnose
from famepy._abi import C, RangeStruct, layout
from famepy._binding import Binding
from famepy._constants import FREQUENCY_MONTHLY, NAME_CAPACITY
from famepy._native import CtypesNative, FameRange
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
    database = famepy.opendb("synthetic-shim.db", "create", session=session)
    yield database
    famepy.closedb(database)


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
    with pytest.raises(HLIError) as error:
        binding.call("cfmopdb", ct.byref(key), ct.create_string_buffer(b"synthetic"), 1)
    assert error.value.status == 901
    binding.call("cfmini")
    version = ct.c_float()
    binding.call("cfmver", ct.byref(version))
    assert version.value == 4.25
    binding.call("cfmopdb", ct.byref(key), ct.create_string_buffer(b"synthetic"), 2)
    assert key.value >= 0
    with pytest.raises(HLIError) as error:
        binding.call("cfmopdb", ct.byref(key), ct.create_string_buffer(b"synthetic"), 9)
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
    with pytest.raises(HLIError) as error:
        famepy.workdb(session=session)
    assert error.value.status == 77
    work = famepy.workdb(session=session)
    assert helper(library, "shim_open_databases")() == 1
    session.finalize()
    assert helper(library, "shim_initialized")() == 0
    assert helper(library, "shim_open_databases")() == 0
    assert not work.is_open
    assert helper(library, "shim_fin_count")() == 1


def test_globals_are_read_with_declared_types(session):
    sentinels = session.sentinels
    assert sentinels.string_nc == b"\xfe\x01" and sentinels.string_nd == b"\xfe\x03"
    assert not sentinels.string_nc.isascii()
    assert sentinels.index_nc != sentinels.index_na != sentinels.index_nd
    assert np.isnan(sentinels.precision_nc) and np.isnan(sentinels.numeric_na)
    assert np.array(sentinels.precision_nc).view(np.uint64) != np.array(
        sentinels.precision_na
    ).view(np.uint64)
    assert sentinels.boolean_nc == -2147483647


def test_bulk_numpy_buffers_and_scalar_range(db, native):
    values = np.array([1.5, 2.5, 3.5, 4.5])
    famepy.do_write(series_object("s", "precision", FREQUENCY_MONTHLY, 100, values), db)
    out = np.full(6, -99.0)
    view = out[1:5]
    native.get_precisions(db.key, b"S", FameRange(FREQUENCY_MONTHLY, 100, 103), view)
    assert out.tolist() == [-99, 1.5, 2.5, 3.5, 4.5, -99]
    raw = read(db, "s", first_index=101, last_index=102)
    assert raw.data.tolist() == [2.5, 3.5]
    famepy.do_write(scalar_object("sc", "precision", 12.5), db)
    scalar = np.empty(1)
    native.get_precisions(db.key, b"SC", None, scalar)
    assert scalar[0] == 12.5
    with pytest.raises(HLIError) as error:
        native.get_precisions(db.key, b"S", FameRange(FREQUENCY_MONTHLY, 90, 91), np.empty(2))
    assert error.value.status == 908
    with pytest.raises(famepy.DataValidationError):
        native.get_precisions(db.key, b"S", FameRange(FREQUENCY_MONTHLY, 100, 103), np.empty(3))
    with pytest.raises(famepy.DataValidationError):
        native.get_precisions(
            db.key, b"S", FameRange(FREQUENCY_MONTHLY, 100, 103), np.empty(4, dtype=np.float32)
        )


def test_every_kind_round_trips_through_the_shim(db, session):
    sentinels = session.sentinels
    precision = np.array(
        [1.0, sentinels.precision_nc, sentinels.precision_na, sentinels.precision_nd]
    )
    numeric = np.array([2.0, sentinels.numeric_nc], dtype=np.float32)
    boolean = np.array([1, 0, sentinels.boolean_nd], dtype=np.int32)
    dates = np.array([24240, sentinels.index_na], dtype=np.int64)
    strings = [b"alpha", b"", session.sentinels.string_nc]
    famepy.do_write(series_object("p", "precision", "monthly", 10, precision), db)
    famepy.do_write(series_object("n", "numeric", "monthly", 10, numeric), db)
    famepy.do_write(series_object("b", "boolean", "monthly", 10, boolean), db)
    famepy.do_write(series_object("d", "date", "monthly", 10, dates, date_frequency=129), db)
    famepy.do_write(series_object("s", "string", "case", 1, strings), db)
    famepy.do_write(scalar_object("nl", "namelist", b"{A,B}"), db)
    famepy.do_write(scalar_object("ss", "string", b"hello world"), db)
    famepy.do_write(scalar_object("ds", "date", 5, date_frequency=129), db)
    famepy.do_write(series_object("e", "precision", "monthly", 0, np.empty(0)), db)
    famepy.postdb(db)
    famepy.closedb(db)
    with famepy.opendb("synthetic-shim.db", "readonly", session=session) as reopened:
        got = read(reopened, "p")
        assert np.array_equal(got.data.view(np.uint64), precision.view(np.uint64))
        assert famepy.classify_by_sentinel(got.data, "precision", sentinels).tolist() == [
            0,
            1,
            2,
            3,
        ]
        assert [famepy.missing_type(reopened, "precision", v) for v in got.data] == [0, 1, 2, 3]
        assert read(reopened, "n").data.view(np.uint32).tolist() == numeric.view(np.uint32).tolist()
        assert read(reopened, "b").data.tolist() == boolean.tolist()
        assert read(reopened, "d").data.tolist() == dates.tolist()
        assert famepy.missing_type(reopened, "date", int(dates[1])) == 2
        assert read(reopened, "s").data == strings
        assert famepy.missing_type(reopened, "string", session.sentinels.string_nc) == 1
        assert famepy.missing_type(reopened, "string", b"NC") == 0
        assert read(reopened, "nl").data == b"{A,B}"
        assert read(reopened, "ss").data == b"hello world"
        ds = read(reopened, "ds")
        assert (ds.kind, ds.type_code, int(ds.data)) == ("date", 129, 5)
        empty = read(reopened, "e")
        assert empty.is_empty(sentinels.index_nc)
        assert famepy.quick_info(reopened, "e").is_empty(sentinels.index_nc)
        with pytest.raises(famepy.DataValidationError):
            famepy.do_write(scalar_object("x", "precision", 1.0), reopened)


def test_close_without_post_discards_in_shim(session):
    database = famepy.opendb("discard.db", "create", session=session)
    famepy.do_write(scalar_object("kept", "precision", 1.0), database)
    famepy.postdb(database)
    famepy.do_write(scalar_object("lost", "precision", 2.0), database)
    famepy.closedb(database)
    with famepy.opendb("discard.db", session=session) as reopened:
        assert [i.name_text for i in famepy.listdb(reopened)] == ["KEPT"]


def test_writable_string_pointer_array(db, native):
    famepy.do_write(series_object("s", "string", "case", 1, [b"abc", b"xy"]), db)
    left, right = ct.create_string_buffer(4), ct.create_string_buffer(3)
    pointers = (C * 2)(ct.cast(left, C), ct.cast(right, C))
    lengths = (ct.c_int32 * 2)(3, 2)
    range_ = RangeStruct(232, 1, 2)
    native.binding.call("fame_get_strings", db.key, b"S", ct.byref(range_), pointers, lengths, None)
    assert (left.value, right.value) == (b"abc", b"xy")
    assert list(lengths) == [3, 2]
    assert native.get_strings(db.key, b"S", FameRange(232, 1, 2), 2) == [b"abc", b"xy"]
    with pytest.raises(famepy.DataValidationError):
        native.write_strings(db.key, b"S", FameRange(232, 1, 2), [b"a\0b", b"c"])


def test_wildcards_truncation_and_cursor_cleanup(db, library):
    long_name = "L" * NAME_CAPACITY
    famepy.do_write(scalar_object(long_name, "precision", 1.0), db)
    famepy.do_write(series_object("sales_a", "precision", "monthly", 0, np.zeros(1)), db)
    famepy.do_write(scalar_object("sales_b", "numeric", 1.0), db)
    names = [i.name_text for i in famepy.listdb(db)]
    assert names == [long_name, "SALES_A", "SALES_B"]
    assert [i.name_text for i in famepy.listdb(db, "sales?")] == ["SALES_A", "SALES_B"]
    assert [i.name_text for i in famepy.listdb(db, "sales_^")] == ["SALES_A", "SALES_B"]
    assert [i.name_text for i in famepy.listdb(db, class_="series")] == ["SALES_A"]
    assert [i.name_text for i in famepy.listdb(db, type="numeric")] == ["SALES_B"]
    assert [i.name_text for i in famepy.listdb(db, freq="monthly")] == ["SALES_A"]
    scalar_info = famepy.listdb(db, "sales_b")[0]
    assert (scalar_info.first_index, scalar_info.last_index) == (0, 0)
    with pytest.raises(famepy.NameTruncatedError) as error:
        famepy.listdb(db, capacity=8)
    assert error.value.returned_length == NAME_CAPACITY
    assert helper(library, "shim_active_cursors")() == 0


def test_commands_output_and_cleanup(session, library, tmp_path):
    output = famepy.fame("display 1", session=session, temp_dir=tmp_path)
    assert output == b"echo: display 1\n"
    assert famepy.fame("display 2+2", session=session, temp_dir=tmp_path) == b"4\n"
    assert helper(library, "shim_output_redirected")() == 0
    with pytest.raises(famepy.CommandError) as error:
        famepy.fame("fail 513", session=session, temp_dir=tmp_path)
    assert error.value.status == 513
    assert error.value.output == b"partial output before failure\n"
    assert helper(library, "shim_output_redirected")() == 0
    assert list(tmp_path.iterdir()) == []


def test_extended_error_mechanics_with_shim_declared_lengths(session, library, tmp_path):
    """The shim declares cfmlerr with the recorded convention; mechanics only."""
    with pytest.raises(famepy.UnsupportedOperationError):
        session.extended_error_text()
    retrieval = session.enable_extended_errors()
    assert isinstance(retrieval, ExtendedErrorRetrieval)
    with pytest.raises(famepy.RuntimeStateError):
        session.extended_error_text()
    with pytest.raises(famepy.CommandError) as error:
        famepy.fame("fail", session=session, temp_dir=tmp_path)
    assert error.value.extended_text == b"synthetic failure for fail"
    assert "synthetic" not in str(error.value)
    assert session.extended_error_text() == b"synthetic failure for fail"
    assert helper(library, "shim_output_redirected")() == 0
    # The declared length sizes the buffer exactly: a longer text is cut by
    # the library at the buffer, a shorter one ends at its terminator.
    assert session._native.extended_error_length() == len(b"synthetic failure for fail")
    buffer = ct.create_string_buffer(b" " * 9, 10)
    session._native.extended_error_fetch(buffer)
    assert buffer.value == b"synthetic"
    with pytest.raises(TypeError):
        session._native.extended_error_fetch(b"immutable")


def test_bridge_round_trip_through_shim(session):
    ts = TSeries(mm(2020, 1), [1.0, np.nan, 3.0])
    write("bridge.db", "ts", ts, mode="create")
    back = value("bridge.db", "ts")
    assert back.firstdate == mm(2020, 1)
    assert np.array_equal(back.values, ts.values, equal_nan=True)
    write("bridge.db", "sc", 2.0, mode="update")
    assert value("bridge.db", "sc") == 2.0
    assert session.open_databases == ()


def test_probe_reports_presence_only_symbol(library):
    report = diagnose(os.environ["FAMEPY_TEST_SHIM"], probe=True)
    assert report["status"] == "symbols_found"
    assert report["presence_only"] == {}
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
    famepy.do_write(series_object("array", "numeric", "case", 1, values), db)
    famepy.do_write(scalar_object("scalar", "numeric", values[0]), db)
    assert int(read(db, "array").data.view(np.uint32)[0]) == 0x7F800101
    raw = read(db, "scalar")
    assert isinstance(raw.data, np.float32)
    assert int(np.array([raw.data]).view(np.uint32)[0]) == 0x7F800101
    assert famepy.missing_type(db, "numeric", raw.data) == 0
    assert famepy.missing_type(db, "numeric", session.sentinels.numeric_na) == 2
    assert isinstance(session.sentinels.numeric_nc, np.float32)


def test_one_shot_lifecycle_against_the_shim(native, library):
    """The shim, like the library, initializes once and treats cfmfin as terminal."""
    first = Session(native=native).initialize()
    first.finalize()
    assert helper(library, "shim_initialized")() == 0
    assert helper(library, "shim_fin_count")() == 1
    with pytest.raises(famepy.RuntimeStateError, match="spawned process"):
        first.initialize()
    with pytest.raises(famepy.RuntimeStateError, match="spawned process"):
        Session(native=CtypesNative(library)).initialize()
    with pytest.raises(famepy.UnsupportedOperationError):
        first.reset()
    assert helper(library, "shim_init_count")() == 1
    first.finalize()
    assert helper(library, "shim_fin_count")() == 1
    # Independently of the package guard, the shim itself refuses a restart.
    status = ct.c_int32(-1)
    library.cfmini(ct.byref(status))
    assert status.value == 3
    library.cfmfin(ct.byref(status))
    assert status.value == 3


def test_broken_owner_blocks_a_second_wrapper_on_the_same_library(native, library):
    first = Session(native=native).initialize()
    helper(library, "shim_fail_next", None, [ct.c_int32])(55)
    with pytest.raises(HLIError):
        first.finalize()
    assert first.state == "broken" and helper(library, "shim_initialized")() == 1
    with pytest.raises(famepy.RuntimeStateError):
        Session(native=CtypesNative(library)).initialize()
    assert helper(library, "shim_init_count")() == 1
    first.finalize()  # no second cfmfin without vendor evidence that it is safe
    assert helper(library, "shim_initialized")() == 1
    assert helper(library, "shim_fin_count")() == 0
    assert first.state == "broken"


def test_native_lifecycle_runner_uses_unloaded_wrapper_and_fresh_child(tmp_path, monkeypatch):
    from famepy import validation

    configured = os.environ.get("FAMEPY_TEST_SHIM")
    if not configured:
        if os.environ.get("FAMEPY_REQUIRE_SHIM") == "1":
            pytest.fail("Required C shim is not configured.")
        pytest.skip("Set FAMEPY_TEST_SHIM to run independent C-library tests.")
    monkeypatch.setenv("FAME", "synthetic")
    report = validation._run_child("lifecycle", {"library": configured, "timeout": 30}, tmp_path)
    assert report["status"] == "pass", report
    cases = {case["id"]: case for case in report["cases"]}
    assert cases["new_wrapper_untouched"]["actual"] == ["created", False, False, 0]
    assert cases["fresh_process:finalized_state"]["status"] == "pass"


def test_rewritten_text_arguments_never_touch_the_callers_bytes(session, library):
    """The shim really trims and upper-cases in/output text; the caller sees nothing."""
    name = b"kept"  # upper-cased in place by the shim; blanks are trimmed elsewhere
    option, value = b" item class ", b" on "
    namelist = b"{ a, b }"
    keyed = {name: "name", option: "option", value: "value", namelist: "list"}
    database = famepy.opendb(b" rewrite.db ", "create", session=session)
    famepy.do_write(scalar_object(name, "precision", 1.0), database)
    famepy.do_write(scalar_object(b" nl ", "namelist", namelist), database)
    famepy.postdb(database)
    with database.session.operation("options") as native:
        native.set_option(option, value)
    assert read(database, "kept").data == 1.0
    stored = read(database, "nl").data
    assert stored == b"{ A, B }"  # the shim stored its upper-cased rewrite
    assert famepy.namelist_members(stored) == (b"A", b"B")
    famepy.delete_object(database, name)
    famepy.postdb(database)
    famepy.closedb(database)
    with famepy.opendb("rewrite.db", session=session) as reopened:
        assert [i.name_text for i in famepy.listdb(reopened)] == ["NL"]
    assert name == b"kept" and option == b" item class " and value == b" on "
    assert namelist == b"{ a, b }"
    assert keyed[b"kept"] == "name" and keyed[b"{ a, b }"] == "list"
    assert hash(name) == hash(b"kept") and len(keyed) == 4


def test_shim_rejects_connection_modes_and_undocumented_option_words(session, native):
    database = famepy.opendb("modes.db", "create", session=session)
    famepy.postdb(database)
    famepy.closedb(database)
    for mode in (6, 7):
        with pytest.raises(HLIError) as error:
            native.open_database(b"modes.db", mode)
        assert error.value.status == 5
        with pytest.raises(famepy.UnsupportedOperationError):
            famepy.opendb("modes.db", mode, session=session)
    with pytest.raises(HLIError) as error:
        native.open_database(b"absent.db", 6)
    assert error.value.status == 5
    for word in (b"ITEM FREQUENCY CASE", b"ITEM FREQUENCY QUARTERLY_DECEMBER", b"ITEM INDEX X"):
        with pytest.raises(HLIError) as error:
            native.set_option(word, b"ON")
        assert error.value.status == 67
    native.set_option(b"ITEM FREQUENCY", b"ON")
    native.set_option(b"ITEM INDEX", b"ON")


def test_shim_applies_family_and_index_selectors_to_series_only(db):
    famepy.do_write(series_object("m", "precision", "monthly", 0, np.zeros(1)), db)
    famepy.do_write(series_object("q", "precision", "quarterly_december", 0, np.zeros(1)), db)
    famepy.do_write(series_object("c", "string", "case", 1, [b"x"]), db)
    famepy.do_write(scalar_object("s", "precision", 1.0), db)
    names = lambda **f: sorted(i.name_text for i in famepy.listdb(db, **f))  # noqa: E731
    assert names(freq="monthly") == ["M"]
    assert names(freq=["monthly", "quarterly_december"]) == ["M", "Q"]
    assert names(freq="case") == ["C"]
    assert names(freq=["case", "monthly"]) == ["C", "M"]
    assert names(freq=["undefined", "monthly"]) == ["M", "S"]
    assert names() == ["C", "M", "Q", "S"]
    from famepy._wildcard import native_listing_count

    monthly_only = [(b"ITEM FREQUENCY", b"OFF"), (b"ITEM FREQUENCY MONTHLY", b"ON")]
    assert native_listing_count(db, "?", monthly_only) == 3  # M, the case series, the scalar
    case_only = [(b"ITEM INDEX", b"OFF"), (b"ITEM INDEX CASE", b"ON")]
    assert native_listing_count(db, "?", case_only) == 2  # C and the scalar
    assert names() == ["C", "M", "Q", "S"]


def test_shim_really_rewrites_in_out_text(library, native):
    """Guards the ownership test above against a shim that stopped mutating."""
    native.initialize()
    status = ct.c_int32(-1)
    option, value = ct.create_string_buffer(b" item class "), ct.create_string_buffer(b" on ")
    library.cfmsopt.argtypes = [ct.POINTER(ct.c_int32), C, C]
    library.cfmsopt(ct.byref(status), option, value)
    assert status.value == 0
    assert option.value == b"ITEM CLASS" and value.value == b"ON"
    native.finalize()


def test_shim_models_the_reserved_name_and_case_type_boundaries(db):
    """Reserved words and the case type are refused by the independent library model.

    The statuses are the library's documented ones (25 and 16), not an
    arbitrary refusal, so a fixture that uses such a name or type cannot
    pass here and fail natively again.
    """
    from famepy._constants import FREQUENCY_CASE
    from famepy._errors import HBOBJT, HNRESW

    with pytest.raises(HLIError) as info:
        famepy.do_write(scalar_object("namelist", "namelist", b"{A}"), db)
    assert info.value.status == HNRESW == 25
    with db.operation("new object") as native, pytest.raises(HLIError) as info:
        native.new_object(db.key, b"CASE_TYPED", 1, 9, FREQUENCY_CASE, 1, 0)
    assert info.value.status == HBOBJT == 16
    famepy.do_write(scalar_object("k_namelist", "namelist", b"{A}"), db)
    assert [i.name_text for i in famepy.listdb(db)] == ["K_NAMELIST"]
