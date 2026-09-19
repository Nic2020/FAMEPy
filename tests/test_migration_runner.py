# SPDX-License-Identifier: MIT
"""The migration qualification group of the validation runner."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tsecon as ts
from fake_native import make_fake

from famepy import migration, validation
from famepy._runtime import Session
from famepy.validation import _groups, _migration_group
from famepy.validation._report import Recorder

TESTS = Path(__file__).resolve().parent
AVAILABLE, BLOCK = migration.dataecon_available()
needs_dataecon = pytest.mark.skipif(
    not AVAILABLE, reason=f"DataEcon native extension unavailable: {BLOCK}"
)


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


def test_required_cases_cover_every_fixture_and_negative_case():
    required = validation.REQUIRED_CASES["migration"]
    assert "migration" in validation.GROUPS and "migration" in _groups.DEPENDENT_GROUPS
    for name in _migration_group.ALL_NAMES:
        assert f"write:{name}" in required
        assert f"readback:{name}" in required
        assert f"cross_process_migration:object:{name}" in required
    assert set(_migration_group.NEGATIVE_CASES) <= set(required)
    assert {"dataecon_available", "plan_actions", "migration_complete", "finalize"} <= set(required)


@needs_dataecon
def test_migration_group_passes_with_the_fake(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_validation_backend", ["lifecycle", "migration"])
    )
    assert report["result"] == "PASS", json.dumps(report["groups"], indent=1)[:4000]
    record = report["groups"]["migration"]
    ids = {case["id"]: case for case in record["cases"]}
    assert set(validation.REQUIRED_CASES["migration"]) <= set(ids)
    assert record["counts"]["blocked"] == 0 and record["counts"]["fail"] == 0
    assert ids["cross_process_migration:object:m_dates_missing"]["status"] == "pass"
    for case_id in (
        "corruption_detected",
        "shifted_mask_detected",
        "wrong_frequency_detected",
        "nonempty_empty_detected",
        "cross_process_corruption:object:m_pseries_nd",
        "cross_process_corruption:object:m_empty",
        "incomplete_archive_status_kept",
        "stale_plan_no_file",
    ):
        assert ids[case_id]["status"] == "pass", case_id
    assert ids["contained_status_incomplete"]["actual"] == ["incomplete", "1", "2"]
    text = json.dumps(report)
    assert str(tmp_path) not in text and "Traceback" not in text


@needs_dataecon
def test_category_altering_backend_cannot_pass_migration(tmp_path, child_env):
    """NC stored as NA outside the monthly calendar: the mask no longer matches the fixture."""
    report = validation.run(_options(tmp_path, "make_nc_to_na_backend", ["lifecycle", "migration"]))
    assert report["result"] == "FAIL"
    record = report["groups"]["migration"]
    statuses = {case["id"]: case["status"] for case in record["cases"]}
    assert statuses["readback:m_business"] == "fail"
    assert statuses["cross_process_migration:object:m_business"] == "fail"
    assert statuses["readback:m_pseries"] == "pass"
    assert "readback:m_business" in record["required_not_passed"]


def test_unavailable_dataecon_blocks_the_group_distinctly(tmp_path, monkeypatch):
    monkeypatch.setattr(migration, "dataecon_available", lambda: (False, "ImportError"))
    session = Session(native=make_fake(persist=True))
    recorder = Recorder()
    ctx = _groups.Context(session, tmp_path, recorder, lambda extra: [], 30.0, None)
    _migration_group.group_migration(ctx)
    statuses = {case.id: case.status for case in recorder.cases}
    assert statuses["dataecon_available"] == "blocked"
    assert set(statuses.values()) == {"blocked"}
    assert set(validation.REQUIRED_CASES["migration"]) <= set(statuses)
    record = {
        "cases": [case.to_json() for case in recorder.cases],
        "counts": recorder.counts(),
        "exit_code": 0,
    }
    assert validation._group_status(record, validation.REQUIRED_CASES["migration"]) == "blocked"
    assert session.state == "loaded" and session._native.fake.calls == []


@needs_dataecon
def test_verify_child_compares_descriptions_and_status(tmp_path, child_env):
    from tsecon.dataecon import open_dataecon

    destination = tmp_path / "a.daec"
    with open_dataecon(destination, "a") as db:
        migration._layout.write_scalar(db, "/", "x", "precision", 1.5, 0)
        db.set_attribute("/", migration.ATTRIBUTE_PREFIX + "layout", migration.LAYOUT_VERSION)
        db.set_attribute("/", migration.ATTRIBUTE_PREFIX + "status", "complete")
        db.set_attribute("/", migration.ATTRIBUTE_PREFIX + "planned", "1")
        db.set_attribute("/", migration.ATTRIBUTE_PREFIX + "written", "1")
    good = migration.describe(migration.expected_object("x", 1.5))
    bad = migration.describe(migration.expected_object("x", 2.5))
    manifest = {
        "database": str(destination),
        "catalog": "/",
        "status": "complete",
        "objects": [
            {"name": "x", "description": good},
            {"name": "missing", "description": good},
        ],
    }
    recorder = Recorder()
    _migration_group.run_verify_migration(manifest, recorder)
    statuses = {case.id: case.status for case in recorder.cases}
    assert statuses == {"status": "pass", "object:x": "pass", "object:missing": "fail"}
    manifest["objects"] = [{"name": "x", "description": bad}]
    manifest["status"] = "incomplete"
    recorder = Recorder()
    _migration_group.run_verify_migration(manifest, recorder)
    statuses = {case.id: case.status for case in recorder.cases}
    assert statuses == {"status": "fail", "object:x": "fail"}
    # An object marked corrupted passes only when it no longer reads back as described.
    manifest["status"] = "complete"
    manifest["objects"] = [
        {"name": "x", "description": bad, "corrupted": True},
        {"name": "x", "description": good, "corrupted": True},
    ]
    recorder = Recorder()
    _migration_group.run_verify_migration(manifest, recorder)
    assert [case.status for case in recorder.cases] == ["pass", "pass", "fail"]
    # The child exits 33 on an invalid manifest, never reporting a pass.
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(json.dumps({"database": str(destination)}), encoding="ascii")
    command = [
        sys.executable,
        "-m",
        "famepy.validation._child",
        "--group",
        "verify_migration",
        "--manifest",
        str(manifest_path),
    ]
    config = json.dumps({"scratch": str(tmp_path), "backend": "fake_native:make_fake"})
    result = subprocess.run(command, input=config, capture_output=True, text=True, timeout=60)
    assert result.returncode == 33


@needs_dataecon
def test_fixture_expectations_are_independent_of_the_readback():
    from fake_native import SENTINELS

    expected = _migration_group.expected_objects(SENTINELS, 24240)
    assert set(expected) == set(_migration_group.ALL_NAMES)
    description = migration.describe(expected["m_dates_missing"])
    assert description["representation"] == "codes" and description["categories"] == [0, 1, 0]
    assert description["values"][1] is None
    assert migration.describe(expected["m_empty"])["firstdate"] is None
    assert migration.describe(expected["m_case"])["frequency"] == "case"
    assert migration.describe(expected["m_weekly"])["firstdate"] == int(ts.weekly("2020-02-28", 7))
