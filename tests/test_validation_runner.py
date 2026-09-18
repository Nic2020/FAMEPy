# SPDX-License-Identifier: MIT
"""The consolidated runner: gates, privacy, faulty backends and adversarial children."""

import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from fake_native import PRIVATE_MARKER, SENTINELS, make_fake

import famepy
from famepy import validation
from famepy.validation import _groups, _report
from famepy.validation.__main__ import main
from famepy.validation._process import Completed

TESTS = Path(__file__).resolve().parent


def _environment():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(TESTS) + os.pathsep + environment.get("PYTHONPATH", "")
    environment.pop("FAME", None)
    return environment


@pytest.fixture
def child_env(monkeypatch):
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)


def _options(tmp_path, backend, groups, **extra):
    options = {
        "scratch": str(tmp_path / "s"),
        "native": True,
        "timeout": 60.0,
        "backend": f"fake_native:{backend}",
        "groups": groups,
    }
    options.update(extra)
    return options


# -- gates ------------------------------------------------------------------


def test_without_native_opt_in_everything_is_blocked(tmp_path, monkeypatch):
    monkeypatch.delenv("FAME", raising=False)
    monkeypatch.delenv("FAMEPY_LIBRARY", raising=False)
    report = validation.run(
        {"scratch": str(tmp_path / "scratch"), "native": False, "timeout": 30.0}
    )
    assert report["result"] == "BLOCKED"
    assert set(report["groups"]) == set(validation.GROUPS)
    assert all(group["status"] == "blocked" for group in report["groups"].values())
    assert report["preflight"]["library"]["status"] == "library_unavailable"
    assert report["preflight"]["scratch"]["usable"] is True
    assert str(tmp_path) not in json.dumps(report)


def test_missing_library_blocks_native_groups(tmp_path, monkeypatch):
    monkeypatch.delenv("FAME", raising=False)
    monkeypatch.delenv("FAMEPY_LIBRARY", raising=False)
    report = validation.run(
        {
            "scratch": str(tmp_path / "scratch"),
            "native": True,
            "timeout": 30.0,
            "groups": ["lifecycle", "database"],
        }
    )
    assert report["result"] == "BLOCKED"
    assert report["groups"]["lifecycle"]["status"] == "blocked"
    assert "library" in report["groups"]["lifecycle"]["note"]
    assert "raw_matrix" not in report["groups"]


def test_invalid_options_are_rejected_before_anything_runs(tmp_path):
    with pytest.raises(ValueError):
        validation.run({"scratch": str(tmp_path / "a"), "groups": ["nope"]})
    with pytest.raises(ValueError, match="source-sha"):
        validation.run({"scratch": str(tmp_path / "b"), "source_sha": "not a revision"})
    with pytest.raises(ValueError, match="attestation"):
        validation.run({"scratch": str(tmp_path / "c"), "abi_attestation": "abc"})
    assert not (tmp_path / "a").exists()
    with pytest.raises(SystemExit):
        main(
            ["--scratch", str(tmp_path / "d"), "--report", str(tmp_path / "r"), "--source-sha", "x"]
        )


# -- scratch safety: nothing pre-existing is ever touched ---------------------


def test_nonempty_scratch_is_refused_and_untouched(tmp_path, monkeypatch):
    scratch = tmp_path / "used"
    scratch.mkdir()
    (scratch / ".famepy-write-check").write_bytes(b"preexisting synthetic data")
    (scratch / "lifecycle.db").write_bytes(b"synthetic database bytes")
    (scratch / "overwrite.db").write_bytes(b"more synthetic bytes")
    before = {p.name: p.read_bytes() for p in scratch.iterdir()}
    monkeypatch.setattr(
        validation, "_run_child", lambda *a, **k: pytest.fail("a child was launched")
    )
    options = _options(tmp_path, "make_validation_backend", ["lifecycle", "database"])
    options["scratch"] = str(scratch)
    report = validation.run(options)
    assert report["result"] == "BLOCKED"
    assert report["preflight"]["scratch"] == {
        "created": False,
        "usable": False,
        "reason": "not_empty",
    }
    assert all(group["status"] == "blocked" for group in report["groups"].values())
    assert {p.name: p.read_bytes() for p in scratch.iterdir()} == before
    facts, run_dir = validation.reserve_scratch(scratch)
    assert run_dir is None and facts["usable"] is False
    assert {p.name: p.read_bytes() for p in scratch.iterdir()} == before
    plain_file = tmp_path / "file"
    plain_file.write_bytes(b"x")
    options["scratch"] = str(plain_file)
    report = validation.run(options)
    assert report["preflight"]["scratch"]["reason"] == "not_a_directory"
    assert plain_file.read_bytes() == b"x"


def test_symlinked_scratch_is_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable here")
    facts, run_dir = validation.reserve_scratch(link)
    assert facts["reason"] == "symlink" and run_dir is None
    assert list(target.iterdir()) == []


def test_each_run_reserves_its_own_directory(tmp_path):
    scratch = tmp_path / "new"
    facts, run_dir = validation.reserve_scratch(scratch)
    assert facts["usable"] is True and facts["created"] is True
    assert run_dir is not None and run_dir.parent == scratch and list(run_dir.iterdir()) == []
    facts2, run_dir2 = validation.reserve_scratch(scratch)
    assert facts2["usable"] is False and facts2["reason"] == "not_empty" and run_dir2 is None
    assert [p.name for p in scratch.iterdir()] == [run_dir.name]


# -- a real campaign with the fake backend -------------------------------------


def test_full_campaign_with_fake_backend_in_subprocesses(tmp_path, child_env):
    report_path = tmp_path / "report.json"
    code = main(
        [
            "--scratch",
            str(tmp_path / "scratch"),
            "--report",
            str(report_path),
            "--native",
            "--backend",
            "fake_native:make_validation_backend",
            "--timeout",
            "60",
            "--groups",
            "lifecycle,database,bridge",
            "--source-sha",
            "fb8ccd9f-dirty",
            "--abi-attestation",
            "0" * 64,
        ]
    )
    report = json.loads(report_path.read_text())
    assert code == 0, json.dumps(report["groups"], indent=1)[:4000]
    assert report["result"] == "PASS"
    assert report["schema_version"] == validation.SCHEMA_VERSION
    preflight = report["preflight"]
    assert preflight["identity"]["source_sha"] == "fb8ccd9f-dirty"
    assert preflight["abi_attestation"] == "0" * 64
    assert len(preflight["abi_table_sha256"]) == 64
    assert preflight["library"]["status"] == "injected_backend"
    assert preflight["scratch"]["usable"] is True
    groups = report["groups"]
    for name in ("lifecycle", "database", "bridge"):
        ids = {case["id"] for case in groups[name]["cases"]}
        assert set(validation.REQUIRED_CASES[name]) <= ids
        assert groups[name]["exit_code"] == 0
    ids = {case["id"] for case in groups["database"]["cases"]}
    assert {"cross_process_scalar:meta:kept", "cross_process_scalar:values:kept"} <= ids
    julia = next(c for c in groups["bridge"]["cases"] if c["id"] == "julia_differential")
    assert julia["status"] == "unsupported"
    text = json.dumps(report)
    assert str(tmp_path) not in text and "Traceback" not in text
    runs = list((tmp_path / "scratch").iterdir())
    assert len(runs) == 1
    assert sorted(p.name for p in runs[0].iterdir()) == ["bridge", "database", "lifecycle"]


def test_lifecycle_failure_blocks_dependent_groups(tmp_path, child_env):
    report = validation.run(_options(tmp_path, "make_failing_backend", ["lifecycle", "database"]))
    assert report["result"] == "FAIL"
    assert report["groups"]["lifecycle"]["status"] == "fail"
    failing = [c for c in report["groups"]["lifecycle"]["cases"] if c["status"] == "fail"]
    assert failing and failing[0]["status_code"] == 97
    assert report["groups"]["database"]["status"] == "blocked"


# -- faulty backends must never produce PASS ---------------------------------


@pytest.mark.parametrize(
    "factory,groups,group,failing",
    [
        ("make_negative_version_backend", ["lifecycle"], "lifecycle", "version_is_positive"),
        (
            "make_nan_canonicalizing_backend",
            ["lifecycle", "raw_matrix"],
            "raw_matrix",
            "read:p_series",
        ),
        (
            "make_nonpersisting_backend",
            ["lifecycle", "database"],
            "database",
            "cross_process_scalar:reopen:kept",
        ),
    ],
)
def test_faulty_backends_cannot_produce_a_pass(
    tmp_path, child_env, factory, groups, group, failing
):
    report = validation.run(_options(tmp_path, factory, groups))
    assert report["result"] == "FAIL"
    record = report["groups"][group]
    assert record["status"] == "fail"
    statuses = {case["id"]: case["status"] for case in record["cases"]}
    assert statuses[failing] == "fail"
    assert failing in record.get("required_not_passed", [])


def test_timeout_terminates_a_hanging_child(tmp_path, child_env):
    started = time.monotonic()
    report = validation.run(
        _options(tmp_path, "make_hanging_backend", ["lifecycle", "database"], timeout=1.0)
    )
    assert report["result"] == "FAIL"
    lifecycle = report["groups"]["lifecycle"]
    assert lifecycle["exit_kind"] == "timeout" and lifecycle["timed_out"] is True
    assert report["groups"]["database"]["status"] == "blocked"
    assert time.monotonic() - started < 60


# -- privacy of the final parent JSON -----------------------------------------


def test_private_markers_never_reach_the_final_report(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_leaky_backend", ["lifecycle", "discovery", "commands"])
    )
    text = json.dumps(report)
    assert PRIVATE_MARKER not in text
    assert str(tmp_path) not in text and "Traceback" not in text
    commands = report["groups"]["commands"]
    assert commands["status"] == "pass"  # the marker rode along with successful output
    statuses = {case["id"]: case["status"] for case in commands["cases"]}
    assert statuses["display_evaluates"] == "pass" and statuses["invalid_command_status"] == "pass"
    discovery = report["groups"]["discovery"]
    assert discovery["status"] == "fail"
    failing = [case for case in discovery["cases"] if case["status"] == "fail"]
    assert failing[0]["error_type"] == "PermissionError" and failing[0]["errno"] == 13
    assert report["result"] == "FAIL"


def test_case_schema_rejects_unsafe_values():
    sanitize = validation._sanitize_case
    malformed = "malformed case record"
    assert sanitize("nope")["note"] == malformed
    assert (
        sanitize({"id": "ok", "status": "pass", "actual": "C:/synthetic/private"})["note"]
        == malformed
    )
    assert sanitize({"id": "ok", "status": "pass", "note": "see /tmp/x"})["status"] == "fail"
    assert sanitize({"id": "ok", "status": "pass", "frames": ["C:/x.py:f"]})["status"] == "fail"
    assert sanitize({"id": "ok", "status": "maybe"})["status"] == "fail"
    assert sanitize({"id": "../x", "status": "pass"})["id"] == "malformed"
    assert sanitize({"id": "ok", "status": "pass", "actual": {"bits": "zz"}})["status"] == "fail"
    assert (
        sanitize({"id": "ok", "status": "pass", "expected": [["x", "/private"]]})["status"]
        == "fail"
    )
    assert (
        sanitize({"id": "ok", "status": "pass", "actual": {"ascii": "a\\\\xff"}})["status"]
        == "fail"
    )
    assert sanitize({"id": "ok", "status": "pass", "errno": "13"})["status"] == "fail"
    assert sanitize({"id": "ok", "status": "pass", "observation": "yes"})["status"] == "fail"
    good = {
        "id": "read:p_series",
        "status": "pass",
        "expected": [1.0, {"bits": "0101000000f8ff7f"}],
        "actual": {"ascii": "hello world"},
        "frames": ["validation/_groups.py:group_lifecycle"],
        "errno": 13,
        "observation": True,
    }
    assert sanitize(good) == good
    assert "C:/synthetic/private" not in json.dumps(
        sanitize({"id": "ok", "status": "pass", "actual": {"k": "C:/synthetic/private"}})
    )


# -- adversarial child payloads ------------------------------------------------


def _payload(cases, group="lifecycle"):
    return {"group": group, "cases": cases, "counts": {}}


def _all_required(status_override=None):
    cases = [
        {"id": case_id, "status": "pass"} for case_id in validation.REQUIRED_CASES["lifecycle"]
    ]
    if status_override:
        for case in cases:
            if case["id"] in status_override:
                case["status"] = status_override[case["id"]]
    return cases


@pytest.mark.parametrize(
    "child,expected_status,expected_result,marker",
    [
        (_payload(_all_required({"version": "blocked"})), "blocked", "BLOCKED", None),
        (
            _payload(_all_required({"version": "unsupported"})),
            "fail",
            "FAIL",
            "required_not_passed",
        ),
        (_payload([]), "fail", "FAIL", "empty_cases"),
        (_payload(_all_required({"version": "ok"})), "fail", "FAIL", "malformed_cases"),
        (_payload([{"id": "initialize", "status": "pass"}]), "fail", "FAIL", "required_missing"),
        (_payload(_all_required(), group="database"), "fail", "FAIL", "invalid_output"),
        ("not json", "fail", "FAIL", "invalid_output"),
        (32, "fail", "FAIL", "group_exception"),
        (
            _payload(_all_required() + [{"id": "x", "status": "pass", "actual": "/private"}]),
            "fail",
            "FAIL",
            "malformed_cases",
        ),
    ],
)
def test_child_payload_gates(
    tmp_path, monkeypatch, child, expected_status, expected_result, marker
):
    def fake_run_child(command, input_text, timeout):
        if isinstance(child, int):
            return Completed(child, json.dumps(_payload(_all_required())))
        if isinstance(child, str):
            return Completed(0, child)
        return Completed(0, json.dumps(child))

    monkeypatch.setattr(validation, "run_child", fake_run_child)
    report = validation.run(_options(tmp_path, "make_validation_backend", ["lifecycle"]))
    record = report["groups"]["lifecycle"]
    assert record["status"] == expected_status
    assert report["result"] == expected_result
    if marker in ("empty_cases", "invalid_output", "group_exception"):
        assert record["exit_kind"] == marker
    elif marker is not None:
        assert record[marker]
    assert "/private" not in json.dumps(report)


def test_verification_compares_metadata_and_bits():
    fake = make_fake(persist=False)
    session = famepy.Session(native=fake).initialize()
    database = famepy.open_database("mem", "create", session=session)
    famepy.write_object(database, "x", famepy.scalar("precision", 1.0))
    famepy.write_object(database, "nc", famepy.scalar("precision", SENTINELS.precision_nc))
    database.post()
    database.close()
    recorder = _report.Recorder()
    manifest = {
        "database": "mem",
        "objects": [
            _groups.manifest_object(
                "x",
                "precision",
                np.array([1.0]),
                class_name="series",
                type_code=5,
                frequency=129,
                first_index=123,
            ),
            _groups.manifest_object(
                "nc", "precision", [SENTINELS.precision_na], class_name="scalar", type_code=5
            ),
        ],
    }
    _groups.run_verify(session, manifest, recorder)
    statuses = {case.id: case.status for case in recorder.cases}
    assert statuses["reopen:x"] == "pass" and statuses["meta:x"] == "fail"
    assert statuses["meta:nc"] == "pass" and statuses["values:nc"] == "fail"
    session.finalize()
    assert _report._equal(SENTINELS.precision_nc, SENTINELS.precision_na) is False
    assert _report._equal(np.float32(1.0), 1.0) is False
    assert _report._equal(np.float64(1.0), 1.0) is True
    assert _report._equal(True, 1) is False


def test_child_exit_codes_for_bad_input(tmp_path):
    command = [sys.executable, "-m", "famepy.validation._child", "--group", "lifecycle"]
    result = subprocess.run(command, input="not json", capture_output=True, text=True, timeout=60)
    assert result.returncode == 30
    config = json.dumps({"scratch": str(tmp_path), "backend": "fake_native:missing_factory"})
    result = subprocess.run(
        command, input=config, capture_output=True, text=True, timeout=60, env=_environment()
    )
    assert result.returncode == 31
    assert json.loads(result.stdout)["setup_error"] == "AttributeError"


def test_recorder_records_no_return_values():
    recorder = _report.Recorder()
    assert recorder.check("value", lambda: PRIVATE_MARKER) == PRIVATE_MARKER
    assert recorder.cases[0].to_json() == {"id": "value", "status": "pass"}

    def boom():
        raise OSError(13, "private path /secret")

    recorder.check("os", boom)
    case = recorder.cases[1]
    assert case.status == "fail" and case.error_type == "PermissionError" and case.errno == 13
    assert "secret" not in json.dumps(case.to_json())
    recorder.expect_error("expected", boom, (OSError,))
    assert recorder.cases[2].status == "pass"
    recorder.expect_error("unexpected", boom, (ValueError,))
    assert recorder.cases[3].status == "fail"
    recorder.expect_error("none", lambda: None, (ValueError,))
    assert recorder.cases[4].status == "fail"
    assert recorder.equal("eq", float("nan"), float("nan")) is True
    fact = recorder.fact("obs", 3)
    assert fact.observation and fact.to_json()["observation"] is True
    assert recorder.counts() == {"pass": 4, "fail": 3, "blocked": 0, "unsupported": 0}
    assert _report.encode_value(b"\xff") == {"ascii": "\\xff"}
    assert _report.encode_value(float("inf")) == {"bits": "000000000000f07f"}
    assert _report.encode_value(np.float32(1.5)) == 1.5
    assert _report.encode_value(np.array([SENTINELS.numeric_nc]))[0] == {"bits": "0101c07f"}


def test_wheel_identity_compares_shipped_sources(tmp_path):
    package_dir = Path(famepy.__file__).resolve().parent
    wheel = tmp_path / "famepy-0.0.2.dev0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for file in package_dir.rglob("*.py"):
            archive.write(file, "famepy/" + file.relative_to(package_dir).as_posix())
    identity = validation.package_identity(wheel, "abc1234")
    assert identity["wheel_matches_installed"] is True
    assert identity["wheel_name_valid"] is True and identity["source_sha"] == "abc1234"
    other = tmp_path / "other-1.0-py3-none-any.whl"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("famepy/__init__.py", "tampered")
    identity = validation.package_identity(other, "not hex")
    assert identity["wheel_matches_installed"] is False and identity["wheel_name"] is None
    assert identity["source_sha"] is None
    assert (
        validation.package_identity(tmp_path / "missing.whl", None)["wheel_error"] == "unreadable"
    )
    assert len(validation.abi_table_sha256()) == 64
