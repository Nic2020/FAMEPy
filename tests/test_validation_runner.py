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
from famepy.validation._process import WorkerResult, read_result

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
            "lifecycle,database,bridge,extended_errors",
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
    for name in ("lifecycle", "database", "bridge", "extended_errors"):
        ids = {case["id"] for case in groups[name]["cases"]}
        assert set(validation.REQUIRED_CASES[name]) <= ids
        assert groups[name]["exit_code"] == 0
    extended = {case["id"]: case for case in groups["extended_errors"]["cases"]}
    assert extended["extended_text_length"]["observation"] is True
    assert extended["extended_text_length"]["actual"] == len(b"synthetic failure for fail 513")
    assert extended["capture_failure_none"]["status"] == "pass"
    ids = {case["id"] for case in groups["database"]["cases"]}
    assert {"cross_process_scalar:meta:kept", "cross_process_scalar:values:kept"} <= ids
    assert {"stale_handle_after_finalize", "stale_close_harmless"} <= ids
    lifecycle_ids = {case["id"] for case in groups["lifecycle"]["cases"]}
    assert {
        "reset_unsupported_while_active",
        "reinitialize_rejected",
        "new_wrapper_rejected",
        "fresh_process:initialize",
        "fresh_process:finalized_state",
    } <= lifecycle_ids
    assert not {"reinitialize", "generation_after_reset", "version_after_reset"} & lifecycle_ids
    julia = next(c for c in groups["bridge"]["cases"] if c["id"] == "julia_differential")
    assert julia["status"] == "unsupported"
    text = json.dumps(report)
    assert str(tmp_path) not in text and "Traceback" not in text
    runs = list((tmp_path / "scratch").iterdir())
    assert len(runs) == 1
    assert sorted(p.name for p in runs[0].iterdir()) == [
        "bridge",
        "database",
        "extended_errors",
        "lifecycle",
    ]


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
            "values:p_series",
        ),
        (
            "make_nonpersisting_backend",
            ["lifecycle", "database"],
            "database",
            "cross_process_scalar:reopen:kept",
        ),
        (
            "make_overlong_error_backend",
            ["lifecycle", "extended_errors"],
            "extended_errors",
            "extended_text_captured",
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


def test_finite_sentinel_profile_passes_every_group(tmp_path, child_env):
    """No group may assume floating missing values are NaNs."""
    report = validation.run(
        _options(tmp_path, "make_finite_sentinel_backend", ["lifecycle", "raw_matrix", "bridge"])
    )
    assert report["result"] == "PASS", json.dumps(report["groups"], indent=1)[:4000]
    lifecycle = {case["id"]: case for case in report["groups"]["lifecycle"]["cases"]}
    assert lifecycle["precision_sentinels_are_nan"]["actual"] is False
    assert lifecycle["precision_sentinels_are_nan"]["observation"] is True


def _worker(payload, returncode=0, kind=None):
    """A launch_worker replacement that hands the parent a finished result."""

    def launch(command, config, timeout, *, tokens, nested=False):
        return WorkerResult(returncode, payload, kind, tokens=tokens)

    return launch


def test_restart_requiring_child_payload_is_rejected(tmp_path, monkeypatch):
    """A child reporting the old restart cases cannot satisfy the required set."""
    cases = [{"id": "reinitialize", "status": "pass"}, {"id": "initialize", "status": "pass"}]
    monkeypatch.setattr(
        validation,
        "launch_worker",
        _worker({"group": "lifecycle", "cases": cases, "counts": {}}),
    )
    report = validation.run(_options(tmp_path, "make_validation_backend", ["lifecycle"]))
    assert report["result"] == "FAIL"
    assert "fresh_process:initialize" in report["groups"]["lifecycle"]["required_missing"]


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
    sanitize = validation.sanitize_case
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
        (_payload(_all_required(), group="database"), "fail", "FAIL", "wrong_group"),
        ("missing_result", "fail", "FAIL", "missing_result"),
        ("invalid_result", "fail", "FAIL", "invalid_result"),
        ("stale_result", "fail", "FAIL", "stale_result"),
        ("partial_result", "fail", "FAIL", "partial_result"),
        ({"group": "lifecycle", "cases": "no"}, "fail", "FAIL", "invalid_result"),
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
    if isinstance(child, int):
        launch = _worker(_payload(_all_required()), returncode=child)
    elif isinstance(child, str):
        launch = _worker(None, kind=child)
    else:
        launch = _worker(child)
    monkeypatch.setattr(validation, "launch_worker", launch)
    report = validation.run(_options(tmp_path, "make_validation_backend", ["lifecycle"]))
    record = report["groups"]["lifecycle"]
    assert record["status"] == expected_status
    assert report["result"] == expected_result
    if marker in (
        "empty_cases",
        "group_exception",
        "wrong_group",
        "missing_result",
        "invalid_result",
        "stale_result",
        "partial_result",
    ):
        assert record["exit_kind"] == marker
    elif marker is not None:
        assert record[marker]
    assert "/private" not in json.dumps(report)


def test_verification_compares_metadata_and_bits():
    fake = make_fake(persist=False)
    session = famepy.Session(native=fake).initialize()
    database = famepy.open_database("mem", "create", session=session)
    famepy.write_object(database, "x", famepy.scalar("precision", 1.0))
    # "nc" itself is a name the library (and the model) reserves.
    famepy.write_object(database, "ncs", famepy.scalar("precision", SENTINELS.precision_nc))
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
                "ncs", "precision", [SENTINELS.precision_na], class_name="scalar", type_code=5
            ),
        ],
    }
    _groups.run_verify(session, manifest, recorder)
    statuses = {case.id: case.status for case in recorder.cases}
    assert statuses["reopen:x"] == "pass" and statuses["meta:x"] == "fail"
    assert statuses["meta:ncs"] == "pass" and statuses["values:ncs"] == "fail"
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
    # Without a result path (a worker run by hand) the document is printed.
    assert json.loads(result.stdout)["setup_error"] == "AttributeError"
    # With one, the streams carry nothing and the result file carries the token.
    result_path = tmp_path / "r.json"
    config = json.dumps(
        {
            "scratch": str(tmp_path),
            "backend": "fake_native:missing_factory",
            "result": str(result_path),
            "log": str(tmp_path / "r.log"),
            "token": "t" * 32,
        }
    )
    result = subprocess.run(
        command, input=config, capture_output=True, text=True, timeout=60, env=_environment()
    )
    assert result.returncode == 31 and result.stdout == "" and result.stderr == ""
    payload, kind = read_result(result_path, "t" * 32)
    assert kind is None and payload["setup_error"] == "AttributeError"
    assert payload["complete"] is True


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
    # Bytes are rendered losslessly only when permitted (runner-owned values).
    assert _report.encode_value(b"\xff") == {"length": 1, "unexpected_bytes": True}
    allowed = {b"\xff", b"", b"ok", b"\t", b"\xff" * 65}
    assert _report.encode_value(b"\xff", allowed) == {"hex": "ff"}
    assert _report.encode_value(b"", allowed) == {"ascii": ""}
    assert _report.encode_value(bytearray(b"ok"), allowed) == {"ascii": "ok"}
    assert _report.encode_value(b"\t", allowed) == {"hex": "09"}
    digest = _report.encode_value(b"\xff" * 65, allowed)
    assert set(digest) == {"length", "sha256"} and digest["length"] == 65
    assert _report.encode_value(float("inf")) == {"bits": "000000000000f07f"}
    assert _report.encode_value(np.float32(1.5)) == 1.5
    assert _report.encode_value(np.array([SENTINELS.numeric_nc]))[0] == {"bits": "0101c07f"}


def test_wheel_identity_compares_shipped_sources(tmp_path):
    package_dir = Path(famepy.__file__).resolve().parent
    wheel = tmp_path / "famepy-0.1.0rc1-py3-none-any.whl"
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


def _synthetic_package(base):
    base.mkdir(parents=True)
    (base / "__init__.py").write_text("x = 1")
    return base


def test_import_classification_uses_structure_not_ancestry(tmp_path):
    classify = validation.classify_import
    checkout = _synthetic_package(tmp_path / "repo" / "src" / "famepy")
    (tmp_path / "repo" / "pyproject.toml").write_text("[project]")
    installed = _synthetic_package(tmp_path / "env" / "lib" / "site-packages" / "famepy")
    flat = _synthetic_package(tmp_path / "flat" / "famepy")
    (tmp_path / "flat" / "pyproject.toml").write_text("[project]")
    # An actual checkout is rejected wherever the campaign runs from.
    facts = classify(checkout, tmp_path / "elsewhere")
    assert facts["imported_from_checkout"] and not facts["imported_from_site_packages"]
    assert classify(flat, tmp_path)["imported_from_checkout"]
    # A normal installed import passes.
    facts = classify(installed, tmp_path / "elsewhere")
    assert facts == {
        "imported_from_checkout": False,
        "imported_from_site_packages": True,
        "working_directory_is_ancestor": False,
    }
    # The working directory being an ancestor of the environment is only an observation.
    facts = classify(installed, tmp_path)
    assert facts["working_directory_is_ancestor"] is True
    assert facts["imported_from_site_packages"] and not facts["imported_from_checkout"]
    identity = validation.package_identity(None, "abc1234", package_dir=installed, cwd=tmp_path)
    assert identity["imported_from_site_packages"] and not identity["imported_from_checkout"]
    assert identity["working_directory_is_ancestor"] is True
    assert len(identity["package_sources_sha256"]) == 64
