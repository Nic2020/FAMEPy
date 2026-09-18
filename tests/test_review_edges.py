# SPDX-License-Identifier: MIT
"""Regression cases for the final operational-core review boundaries."""

import ctypes as ct
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
from tsecon import MIT, Monthly, TSeries, mm

import famepy
from famepy import _runtime, bridge, validation
from famepy._constants import ObjectType, frequency_code
from famepy._discovery import discover
from famepy.validation._process import _kill_tree, run_child


@pytest.mark.parametrize("frequency", [0, 1, True, 2**40, ObjectType.NUMERIC])
def test_date_rejection_before_write(db, frequency):
    famepy.write_object(db, "keep", famepy.scalar("precision", 7.0))
    fake = db.session._native.fake
    fake.calls.clear()
    with pytest.raises(famepy.DataValidationError):
        famepy.write_object(db, "keep", famepy.RawScalar("date", 1, frequency), replace=True)
    assert fake.calls == []
    assert famepy.read_object(db, "keep").value == 7.0
    with pytest.raises(famepy.DataValidationError):
        famepy.RawSeries("date", 129, 1, np.ones(1, dtype=np.int64), frequency)


def test_enum_and_float_overflow():
    with pytest.raises(ValueError):
        frequency_code(ObjectType.NUMERIC)
    with pytest.raises(famepy.DataValidationError):
        famepy.RawScalar("numeric", 1e100)


@pytest.mark.parametrize("bad_name", ["", "x" * 243])
def test_name_rejection_before_io(session, tmp_path, bad_name):
    path = tmp_path / "keep.db"
    with famepy.open_database(path, "create", session=session) as database:
        famepy.write_object(database, "keep", famepy.scalar("precision", 7.0))
        database.post()
        session._native.fake.calls.clear()
        with pytest.raises(ValueError):
            famepy.write_object(database, bad_name, famepy.scalar("precision", 1.0), replace=True)
        assert session._native.fake.calls == []
    before = path.read_bytes()
    session._native.fake.calls.clear()
    with pytest.raises(ValueError):
        bridge.write_scalar(path, bad_name, 1.0, mode="overwrite")
    assert session._native.fake.calls == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["date", "empty_dtype"])
def test_series_checks_before_open(session, tmp_path, kind):
    ts = (
        TSeries(MIT.from_yp(Monthly(), 2**31, 1), np.ones(1))
        if kind == "date"
        else TSeries(mm(2020, 1), np.empty(0, dtype=np.float32))
    )
    session._native.fake.calls.clear()
    with pytest.raises(famepy.DataValidationError):
        bridge.write_tseries(tmp_path / "not_created.db", "x", ts, mode="overwrite")
    assert session._native.fake.calls == []
    assert not (tmp_path / "not_created.db").exists()


def test_signed_minimum_exact(db):
    ts = TSeries(mm(2020, 1), np.array([-(2**63)], dtype=np.int64))
    bridge.write_tseries(db, "exact", ts)
    assert bridge.read_tseries(db, "exact").values[0] == float(-(2**63))


def test_quote_boundaries(tmp_path):
    (tmp_path / "a.inp").write_bytes(b"display 4")
    (tmp_path / "a;b.inp").write_bytes(b"display 5")
    quoted = b'display "prefix; input a; suffix"; input a'
    assert famepy.expand_input(quoted, base_dir=tmp_path) == (
        b'display "prefix; input a; suffix";\ndisplay 4\n'
    )
    assert famepy.expand_input(b'input "a;b"', base_dir=tmp_path) == b"\ndisplay 5\n"


def test_loaded_explicit_owner_fork(tmp_path, monkeypatch):
    path = tmp_path / "library.dll"
    path.touch()
    monkeypatch.setattr(ct, "CDLL", lambda *args: object())
    _runtime.Runtime(discover(path)).load()
    _runtime._after_fork()
    assert _runtime._INHERITED
    with pytest.raises(famepy.InheritedRuntimeError):
        _runtime.Session(discover(path)).initialize()


@pytest.mark.parametrize(
    "field,value",
    [
        ("imported_from_site_packages", False),
        ("imported_from_checkout", True),
        ("source_sha", None),
        ("wheel_name_valid", False),
        ("wheel_matches_installed", False),
        ("abi_attestation", None),
    ],
)
def test_native_provenance_gate(tmp_path, monkeypatch, field, value):
    identity = dict(
        imported_from_site_packages=True,
        imported_from_checkout=False,
        source_sha="a" * 40,
        wheel_name_valid=True,
        wheel_matches_installed=True,
    )
    attestation = "b" * 64
    if field == "abi_attestation":
        attestation = value
    else:
        identity[field] = value
    monkeypatch.setattr(validation, "package_identity", lambda *args: identity)
    monkeypatch.setattr(famepy, "diagnose", lambda *a, **k: {"status": "symbols_found"})
    monkeypatch.setattr(validation, "_run_child", lambda *a, **k: pytest.fail("native group ran"))
    report = validation.run(
        dict(
            scratch=str(tmp_path / "s"),
            native=True,
            groups=["lifecycle"],
            abi_attestation=attestation,
        )
    )
    assert report["result"] == "BLOCKED"


@pytest.mark.parametrize("variant", ["duplicate", "observation"])
def test_case_identity_integrity(variant):
    required = ("check",)
    cases = [{"id": "check", "status": "pass"}]
    if variant == "duplicate":
        cases.append(dict(cases[0]))
    else:
        cases[0]["observation"] = True
    record = dict(cases=cases, exit_code=0, counts=dict(fail=0, blocked=0))
    assert validation._group_status(record, required) == "fail"


def test_taskkill_failure_fallback(monkeypatch):
    calls = []
    process = SimpleNamespace(pid=123, kill=lambda: calls.append("kill"))
    monkeypatch.setattr(sys, "platform", "win32")

    def unavailable(*args, **kwargs):
        raise OSError("synthetic")

    monkeypatch.setattr(subprocess, "run", unavailable)
    _kill_tree(process)
    assert calls == ["kill"]


def test_nested_group_timeout(tmp_path):
    # The verifier stays in the outer POSIX group, so killing the outer worker
    # must not leave it alive to produce its delayed marker. Also covers Windows.
    marker = tmp_path / "late"
    leaf = "import time,pathlib,sys; time.sleep(3); pathlib.Path(sys.argv[1]).write_text('alive')"
    outer = (
        "import sys; from famepy.validation._process import run_child; "
        f"run_child([sys.executable,'-c',{leaf!r},{str(marker)!r}], '', 20, nested=True)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_child([sys.executable, "-c", outer], "", 1.5)
    time.sleep(3.5)
    assert not marker.exists()


def test_signed_minimum_in_report():
    case = {"id": "date", "status": "pass", "actual": -(2**63)}
    assert validation._sanitize_case(case) == case


def test_cursor_cleanup_error(db):
    famepy.write_object(db, "x", famepy.scalar("precision", 1.0))
    fake = db.session._native.fake
    fake.fail_next["fame_quick_info"] = 513
    fake.fail_next["fame_free_wildcard"] = 999
    with pytest.raises(famepy.FameError) as failure:
        famepy.list_objects(db)
    assert failure.value.status == 513
    assert fake.options[b"ITEM CLASS"] == b"ON"


def test_listing_error_capture(db, monkeypatch):
    famepy.write_object(db, "x", famepy.scalar("precision", 1.0))
    fake = db.session._native.fake
    fake.error_text = b"original error"
    fake.fail_next["fame_quick_info"] = 513
    free = fake.free_wildcard

    def cleanup(cursor):
        fake.error_text = b"cleanup changed the error"
        return free(cursor)

    monkeypatch.setattr(fake, "free_wildcard", cleanup)
    db.session.extended_error_retrieval = famepy.ExtendedErrorRetrieval(
        lambda native: len(native.fake.error_text),
        lambda native, buffer: ct.memmove(buffer, native.fake.error_text, len(buffer) - 1),
    )
    with pytest.raises(famepy.FameError) as failure:
        famepy.list_objects(db)
    assert failure.value.extended_text == b"original error"
