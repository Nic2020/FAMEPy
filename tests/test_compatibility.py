# SPDX-License-Identifier: MIT
"""Documented-contract regressions: local access modes, listing selectors,
namelist member semantics, discovery containment and the ownership of text
buffers the library may rewrite."""

import ctypes as ct
import json
import os
from pathlib import Path

import numpy as np
import pytest
from canonical import read, scalar_object, series_object
from fake_native import FakeStatus, make_fake

import famepy
from famepy import validation
from famepy._abi import SIGNATURES, WRITABLE_TEXT, C, S
from famepy._constants import FREQUENCIES, FREQUENCY_FAMILIES, AccessMode
from famepy._errors import HBMODE
from famepy._native import CtypesNative
from famepy._wildcard import NORMALIZED_OPTIONS, native_selectors
from famepy.validation import _groups

TESTS = Path(__file__).resolve().parent


@pytest.fixture
def child_env(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TESTS) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.delenv("FAME", raising=False)


def _options(tmp_path, backend, groups):
    return {
        "scratch": str(tmp_path / "s"),
        "native": True,
        "timeout": 60.0,
        "backend": f"fake_native:{backend}",
        "groups": groups,
    }


def _cases(record):
    return {case["id"]: case for case in record["cases"]}


# -- 1. access modes ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["write", "direct_write", 6, 7, AccessMode.WRITE])
def test_connection_modes_are_refused_before_any_native_call(session, tmp_path, mode):
    path = tmp_path / "local.db"
    with famepy.opendb(path, "create", session=session) as database:
        famepy.postdb(database)
    fake = session._native.fake
    fake.calls.clear()
    with pytest.raises(famepy.UnsupportedOperationError) as error:
        famepy.opendb(path, mode, session=session)
    assert fake.calls == []
    assert str(path) not in str(error.value)
    assert "server connection" in str(error.value)
    # The constants stay: parity with the reference table is kept.
    assert [m.value for m in AccessMode] == [1, 2, 3, 4, 5, 6, 7]


def test_five_local_modes_still_open(session, tmp_path):
    path = tmp_path / "modes.db"
    with famepy.opendb(path, "create", session=session) as database:
        famepy.postdb(database)
    for mode in ("readonly", "update", "shared", "overwrite"):
        with famepy.opendb(path, mode, session=session) as database:
            assert database.mode.name.lower() == mode


def test_bad_mode_status_has_a_message():
    error = famepy.HLIError(HBMODE, operation="cfmopdb")
    assert HBMODE == 5 and "access mode" in str(error)


def test_database_group_asserts_documented_mode_rejection(tmp_path, child_env):
    report = validation.run(_options(tmp_path, "make_validation_backend", ["database"]))
    assert report["result"] == "PASS", json.dumps(report["groups"]["database"])[:3000]
    cases = _cases(report["groups"]["database"])
    for mode in ("write", "direct_write"):
        assert cases[f"mode_{mode}_refused"]["error_type"] == "UnsupportedOperationError"
        assert cases[f"mode_{mode}_native_status"]["actual"] == HBMODE
        assert cases[f"mode_{mode}_fixture_unchanged"]["actual"] == ["BASE"]
        assert cases[f"mode_{mode}_new_path_status"]["actual"] == HBMODE
        assert cases[f"mode_{mode}_new_path_absent"]["actual"] is False
        for suffix in ("refused", "native_status", "new_path_status", "new_path_absent"):
            assert cases[f"mode_{mode}_{suffix}"].get("observation") is not True
    assert not any(case_id.startswith("mode_write_persisted") for case_id in cases)


@pytest.mark.parametrize(
    "factory,failing",
    [
        ("make_mode_accepting_backend", "mode_write_native_status"),
        ("make_mode_side_effect_backend", "mode_write_new_path_absent"),
    ],
)
def test_undocumented_mode_behavior_fails_the_group(tmp_path, child_env, factory, failing):
    report = validation.run(_options(tmp_path, factory, ["database"]))
    assert report["result"] == "FAIL"
    record = report["groups"]["database"]
    cases = _cases(record)
    assert cases[failing]["status"] == "fail"
    assert failing in record["required_not_passed"]
    # The refusal by the package is independent of what the library does.
    assert cases["mode_write_refused"]["status"] == "pass"
    assert cases["mode_update"]["status"] == "pass"
    assert record["counts"]["unsupported"] == 0


# -- 2. listing selectors -------------------------------------------------------------


@pytest.fixture
def mixed(session, tmp_path):
    database = famepy.opendb(tmp_path / "mixed.db", "create", session=session)
    first = 24240
    famepy.do_write(series_object("m_a", "precision", "monthly", first, np.zeros(2)), database)
    famepy.do_write(
        series_object("m_b", "numeric", "monthly", first, np.zeros(1, np.float32)), database
    )
    famepy.do_write(
        series_object("q_a", "precision", "quarterly_december", 100, np.zeros(2)), database
    )
    famepy.do_write(series_object("c_s", "string", "case", 1, [b"x"]), database)
    famepy.do_write(scalar_object("sc", "precision", 1.0), database)
    famepy.do_write(scalar_object("st", "string", b"t"), database)
    yield database
    famepy.closedb(database)


def _names(database, pattern="?", **filters):
    return sorted(info.name_text for info in famepy.listdb(database, pattern, **filters))


def test_every_family_selector_is_a_documented_word():
    assert sorted(set(FREQUENCY_FAMILIES.values())) == [
        "ANNUAL",
        "BIMONTHLY",
        "BIWEEKLY",
        "BUSINESS",
        "DAILY",
        "HOURLY",
        "MILLISECONDLY",
        "MINUTELY",
        "MONTHLY",
        "PPY",
        "QUARTERLY",
        "SECONDLY",
        "SEMIANNUAL",
        "TENDAY",
        "TWICEMONTHLY",
        "USERDEFINED",
        "WEEKLY",
        "YPP",
    ]
    assert set(FREQUENCY_FAMILIES) == set(FREQUENCIES.values()) - {0, 232}
    assert FREQUENCY_FAMILIES[233] == "USERDEFINED"
    assert FREQUENCY_FAMILIES[16] == FREQUENCY_FAMILIES[22] == "WEEKLY"


def test_native_selectors_never_exclude_a_requested_object():
    assert native_selectors(None) == {"FREQUENCY": [], "INDEX": []}
    assert native_selectors({129}) == {"FREQUENCY": [b"MONTHLY"], "INDEX": []}
    assert native_selectors({129, 162, 16}) == {
        "FREQUENCY": [b"MONTHLY", b"QUARTERLY", b"WEEKLY"],
        "INDEX": [],
    }
    assert native_selectors({232}) == {"FREQUENCY": [], "INDEX": [b"CASE"]}
    # Mixed case/date or any scalar request stays broad: no native narrowing.
    assert native_selectors({232, 129}) == {"FREQUENCY": [], "INDEX": []}
    assert native_selectors({0, 129}) == {"FREQUENCY": [], "INDEX": []}
    assert native_selectors({0}) == {"FREQUENCY": [], "INDEX": []}


def test_listing_sends_documented_words_only(mixed):
    fake = mixed.session._native.fake
    original = fake.set_option
    sent = []

    def spy(name, value):
        sent.append((name, value))
        return original(name, value)

    fake.set_option = spy
    try:
        assert _names(mixed, freq="case") == ["C_S"]
        assert (b"ITEM INDEX", b"OFF") in sent and (b"ITEM INDEX CASE", b"ON") in sent
        assert not any(name.startswith(b"ITEM FREQUENCY ") for name, _ in sent)
        sent.clear()
        assert _names(mixed, freq=["monthly", "quarterly_december"]) == ["M_A", "M_B", "Q_A"]
        assert (b"ITEM FREQUENCY", b"OFF") in sent
        assert (b"ITEM FREQUENCY MONTHLY", b"ON") in sent
        assert (b"ITEM FREQUENCY QUARTERLY", b"ON") in sent
        assert not any(name.startswith(b"ITEM INDEX ") for name, _ in sent)
        sent.clear()
        assert _names(mixed, freq=["monthly", "case"]) == ["C_S", "M_A", "M_B"]
        assert not any(name.startswith((b"ITEM FREQUENCY ", b"ITEM INDEX ")) for name, _ in sent)
        sent.clear()
        assert _names(mixed, freq=["undefined", "monthly"]) == ["M_A", "M_B", "SC", "ST"]
        assert not any(name.startswith((b"ITEM FREQUENCY ", b"ITEM INDEX ")) for name, _ in sent)
        # No guessed token is ever sent, whatever the request.
        for request in ("case", "monthly", "weekly_sunday", "biweekly_bfriday", "weekly_pattern"):
            sent.clear()
            famepy.listdb(mixed, freq=request)
            for name, _ in sent:
                words = name.split(b" ")
                if len(words) == 3 and words[1] == b"FREQUENCY":
                    assert words[2].decode() in set(FREQUENCY_FAMILIES.values())
                if len(words) == 3 and words[1] == b"INDEX":
                    assert words[2] in (b"CASE", b"DATE")
    finally:
        fake.set_option = original
    # Every option, including INDEX, is back to ON afterwards.
    for name, value in NORMALIZED_OPTIONS:
        assert fake.options[name] == value
    assert not any(k.startswith((b"ITEM FREQUENCY ", b"ITEM INDEX ")) for k in fake.options)


def test_fake_rejects_undocumented_option_words(mixed):
    fake = mixed.session._native.fake
    for name in (b"ITEM FREQUENCY CASE", b"ITEM FREQUENCY QUARTERLY_DECEMBER", b"ITEM INDEX CAS"):
        with pytest.raises(FakeStatus) as error:
            fake.set_option(name, b"ON")
        assert error.value.status == 67
    fake.set_option(b"ITEM INDEX DATE", b"ON")
    fake.set_option(b"ITEM INDEX", b"ON")


def test_family_and_index_option_errors_surface_and_normalize(mixed):
    fake = mixed.session._native.fake
    fake.refuse_options = {b"ITEM FREQUENCY MONTHLY"}
    with pytest.raises(famepy.HLIError) as error:
        famepy.listdb(mixed, freq="monthly")
    assert error.value.status == 67
    assert fake.options[b"ITEM FREQUENCY"] == b"ON" and fake.cursors == {}
    # A request the narrowing leaves broad is unaffected by that refusal.
    assert _names(mixed, freq=["monthly", "case"]) == ["C_S", "M_A", "M_B"]
    fake.refuse_options = {b"ITEM INDEX CASE"}
    with pytest.raises(famepy.HLIError):
        famepy.listdb(mixed, freq="case")
    assert fake.options[b"ITEM INDEX"] == b"ON"
    assert _names(mixed, freq="monthly") == ["M_A", "M_B"]


def test_exact_sets_with_native_narrowing_in_fake_and_family_refusal(mixed):
    assert _names(mixed, freq="monthly") == ["M_A", "M_B"]
    assert _names(mixed, freq="quarterly_december") == ["Q_A"]
    assert _names(mixed, freq="case") == ["C_S"]
    assert _names(mixed, freq="undefined") == ["SC", "ST"]
    assert _names(mixed, freq=[129, "case", "undefined"]) == [
        "C_S",
        "M_A",
        "M_B",
        "SC",
        "ST",
    ]
    assert _names(mixed, freq="monthly", class_="scalar") == []
    assert _names(mixed, freq="daily") == []
    for bad in ("quarterly", "annual", "weekly", "monthly;drop", True, 7):
        with pytest.raises(ValueError):
            famepy.listdb(mixed, freq=bad)


# -- 3. discovery containment ----------------------------------------------------


@pytest.mark.parametrize(
    "factory,failing",
    [
        ("make_family_option_refusing_backend", "filter_frequency_monthly"),
        ("make_index_option_refusing_backend", "filter_frequency_case"),
    ],
)
def test_one_listing_failure_does_not_hide_the_other_discovery_cases(
    tmp_path, child_env, factory, failing
):
    report = validation.run(_options(tmp_path, factory, ["discovery"]))
    assert report["result"] == "FAIL"
    record = report["groups"]["discovery"]
    cases = _cases(record)
    assert cases[failing]["status"] == "fail" and cases[failing]["status_code"] == 67
    assert failing in record["required_not_passed"]
    # Every other required case still ran to a verdict of its own.
    for case_id in _groups.DISCOVERY_REQUIRED:
        assert case_id in cases, case_id
        assert cases[case_id]["status"] != "blocked", case_id
    for case_id in (
        "filter_frequency_mixed",
        "filter_frequency_undefined_with_monthly",
        "options_normalized_after_listing",
        "alias_off_lists",
        "truncation_reported",
        "listing_after_truncation_still_works",
        "finalize",
    ):
        assert cases[case_id]["status"] == "pass", case_id
    # The isolated native observations record the status instead of aborting.
    label = "monthly_family" if "family" in factory else "case_index"
    assert cases[f"native_selector_count:{label}"]["actual"] == {"status": 67}
    assert cases[f"native_selector_count:{label}"]["observation"] is True


def test_discovery_passes_with_the_documented_fake(tmp_path, child_env):
    report = validation.run(_options(tmp_path, "make_validation_backend", ["discovery"]))
    assert report["result"] == "PASS", json.dumps(report["groups"]["discovery"])[:3000]
    cases = _cases(report["groups"]["discovery"])
    # The frequency family leaves the case series and the scalars alone; the
    # index words select series by index kind and leave the scalars alone.
    assert cases["native_selector_count:monthly_family"]["actual"] == 6
    assert cases["native_selector_count:case_index"]["actual"] == 4
    assert cases["native_selector_count:date_index"]["actual"] == 5
    assert cases["filter_frequency_undefined"]["actual"] == ["L" * 242, "OTHER", "SALE"]


# -- 4. namelist member semantics -------------------------------------------------


def test_namelist_members_grammar():
    assert famepy.namelist_members(b"{A,B,C}") == (b"A", b"B", b"C")
    assert famepy.namelist_members(b"{A, B, C}") == (b"A", b"B", b"C")
    assert famepy.namelist_members(b"{ A ,B , C }") == (b"A", b"B", b"C")
    assert famepy.namelist_members(b"{}") == () == famepy.namelist_members(b"{ }")
    assert famepy.namelist_members(b"{a,A}") == (b"a", b"A")  # spelling kept, no de-duplication
    for bad in (
        b"",
        b"{",
        b"}",
        b"A,B",
        b"{A,,B}",
        b"{A,}",
        b"{A B}",
        b"{A}x",
        b"{\xfe}",
        b"{A\n}",
    ):
        with pytest.raises(famepy.DataValidationError):
            famepy.namelist_members(bad)
    with pytest.raises(famepy.DataValidationError):
        famepy.namelist_members("{A}")  # type: ignore[arg-type]


def test_raw_namelist_bytes_are_untouched_and_plain_strings_stay_exact(db):
    fake = db.session._native.fake
    fake.namelist_layout = "blank_after_comma"
    famepy.do_write(scalar_object("nl", "namelist", b"{A,B,C}"), db)
    famepy.do_write(scalar_object("s", "string", b"{A,B,C}"), db)
    raw = read(db, "nl")
    assert raw.data == b"{A, B, C}"  # the API returns the library's bytes as they are
    assert famepy.namelist_members(raw.data) == (b"A", b"B", b"C")
    assert read(db, "s").data == b"{A,B,C}"  # a string is byte-exact


def test_namelist_layout_only_passes_and_corruption_fails(tmp_path, child_env):
    report = validation.run(_options(tmp_path, "make_namelist_relayout_backend", ["raw_matrix"]))
    assert report["result"] == "PASS", json.dumps(report["groups"]["raw_matrix"])[:3000]
    cases = _cases(report["groups"]["raw_matrix"])
    assert cases["values:nl_scalar"]["status"] == "pass"
    assert cases["values:nl_scalar"]["actual"] == [{"ascii": "A"}, {"ascii": "B"}, {"ascii": "C"}]
    assert cases["namelist_length:nl_scalar"]["actual"] == 9
    assert cases["namelist_layout:nl_scalar"]["actual"] == "blank_after_comma"
    assert cases["namelist_layout:nl_scalar"]["observation"] is True
    assert cases["cross_process_matrix:values:nl_scalar"]["status"] == "pass"
    assert (
        cases["values:nl_empty"]["actual"] == []
        and cases["namelist_length:nl_empty"]["actual"] == 2
    )
    assert cases["values:s_scalar"]["status"] == "pass"
    text = json.dumps(report)
    assert "{A, B, C}" not in text and "7b41" not in text  # the returned bytes never leave


@pytest.mark.parametrize(
    "factory", ["make_namelist_corrupting_backend", "make_namelist_dropping_backend"]
)
def test_namelist_member_corruption_fails_in_both_processes(tmp_path, child_env, factory):
    report = validation.run(_options(tmp_path, factory, ["raw_matrix"]))
    assert report["result"] == "FAIL"
    record = report["groups"]["raw_matrix"]
    cases = _cases(record)
    assert cases["values:nl_scalar"]["status"] == "fail"
    assert cases["cross_process_matrix:values:nl_scalar"]["status"] == "fail"
    assert {"values:nl_scalar", "cross_process_matrix:values:nl_scalar"} <= set(
        record["required_not_passed"]
    )
    assert cases["values:nl_empty"]["status"] == "pass"
    assert cases["values:s_scalar"]["status"] == "pass"
    assert cases["values:p_series"]["status"] == "pass"


def test_manifest_describes_namelists_by_members():
    entry = _groups.manifest_object("nl", "namelist", [b"{A, B}"], class_name="scalar", type_code=2)
    assert entry["values"] == [["41", "42"]]
    assert _groups._decode_manifest("namelist", entry["values"]) == [[b"A", b"B"]]
    with pytest.raises(famepy.DataValidationError):
        _groups.manifest_object("nl", "namelist", [b"{A,,B}"], class_name="scalar", type_code=2)


# -- 5. text buffers the library may rewrite ------------------------------------------


def test_documented_in_out_text_arguments_use_owned_buffers():
    """Every documented in/output text position is declared writable, no other."""
    for name, spec in SIGNATURES.items():
        writable = WRITABLE_TEXT.get(name, ())
        for position, argument in enumerate(spec.arguments):
            if position in writable:
                assert argument is C, (name, position)
            elif argument is S:
                assert name in ("cfmfame", "cfmissm") or spec.convention == "fame", (
                    name,
                    position,
                )
    assert set(WRITABLE_TEXT) == {
        "cfmopdb",
        "cfmsopt",
        "cfmnlen",
        "cfmgtnl",
        "cfmwtnl",
        "cfmdlob",
        "cfmnwob",
    }


def test_binding_receives_owned_copies_not_caller_bytes(monkeypatch):
    seen = {}

    class Library:
        pass

    native = CtypesNative(Library())

    def call(name, *args):
        seen.setdefault(name, []).append(args)
        if name == "cfmnlen":
            args[3]._obj.value = 3
        if name == "cfmgtnl":
            ct.memmove(args[3], b"{A}", 3)
            args[5]._obj.value = 3

    monkeypatch.setattr(native._binding, "call", call)
    name, value, option = b" kept ", b"{a, b}", b"item class"
    native.open_database(name, 4)
    native.new_object(1, name, 2, 0, 2, 0, 0)
    native.delete_object(1, name)
    native.get_namelist(1, name)
    native.write_namelist(1, name, value)
    native.set_option(option, b"on")
    # Each documented in/output argument is a distinct ctypes char array with
    # its own terminator, never the bytes object the caller passed in.
    checked = 0
    for calls in seen.values():
        for args in calls:
            for argument in args:
                if isinstance(argument, ct.Array):
                    assert argument._type_ is ct.c_char
                    assert argument.raw.endswith(b"\0")
                    assert argument.raw[:-1] in (name, value, option, b"on")
                    checked += 1
                assert not isinstance(argument, bytes)
    assert checked == 9
    assert name == b" kept " and value == b"{a, b}" and option == b"item class"


def test_none_of_the_input_only_text_is_copied(monkeypatch):
    seen = []
    native = CtypesNative(object())
    monkeypatch.setattr(native._binding, "call_status", lambda name, *args: seen.append(args) or 0)
    native.execute(b"display 1")
    assert seen == [(b"display 1",)]


def test_fake_default_mode_rejection_matches_the_documented_open(tmp_path):
    fake = make_fake(persist=True).fake
    fake.initialize()
    path = tmp_path / "x.db"
    key = fake.open_database(str(path).encode(), 2)
    fake.post_database(key)
    fake.close_database(key)
    for mode in (6, 7):
        with pytest.raises(FakeStatus) as error:
            fake.open_database(str(path).encode(), mode)
        assert error.value.status == HBMODE
    assert not (tmp_path / "new.db").exists()
    with pytest.raises(FakeStatus):
        fake.open_database(str(tmp_path / "new.db").encode(), 6)
    assert not (tmp_path / "new.db").exists()


@pytest.mark.parametrize(
    "name,position", [(name, p) for name, positions in WRITABLE_TEXT.items() for p in positions]
)
def test_binding_rejects_immutable_text_before_resolving_native(name, position):
    from famepy._binding import Binding

    args = [None] * len(SIGNATURES[name].arguments)
    for index in WRITABLE_TEXT[name]:
        args[index] = ct.create_string_buffer(b"synthetic")
    args[position] = b"immutable"
    # The object has no native symbols: rejection must precede symbol resolution.
    with pytest.raises(TypeError, match="owned character array"):
        Binding(object()).call_status(name, *args)
