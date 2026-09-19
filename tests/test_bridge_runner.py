# SPDX-License-Identifier: MIT
"""The frequencies, workspace and extended bridge groups of the validation runner."""

import json
import os
from pathlib import Path

import pytest

from famepy import validation
from famepy.validation import _bridge_groups, _julia

TESTS = Path(__file__).resolve().parent


@pytest.fixture
def child_env(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TESTS) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.delenv("FAME", raising=False)


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


def test_required_case_lists_cover_every_anchor_and_kind():
    assert set(validation.GROUPS) >= {"frequencies", "workspace", "bridge"}
    assert len(_bridge_groups.CALENDAR_CODES) == 31
    required = validation.REQUIRED_CASES["frequencies"]
    for label in ("weekly_sunday", "quarterly_october", "semiannual_july", "annual_march"):
        for fact in ("inverse", "adjacent", "year_boundary", "periods_in_year"):
            assert f"{fact}:{label}" in required
        assert f"cross_process_frequencies:values:f_{label}" in required
    assert "date_values:dv_business_of_case" in required
    bridge_required = validation.REQUIRED_CASES["bridge"]
    assert {"kind:date_series", "missing:boolean:nd", "empty:t6"} <= set(bridge_required)
    assert "boolean_missing_never_true" in bridge_required
    assert {label for label, _, _ in _julia.frequency_specs()} == {
        _bridge_groups._label(code) for code in _bridge_groups.CALENDAR_CODES
    }
    assert "no_such_object" not in _julia.SCRIPT and "\\" not in _julia.SCRIPT.split("\n")[1]
    assert _julia.SCRIPT.isascii()


def test_bridge_groups_pass_with_the_fake(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_validation_backend", ["lifecycle", "frequencies", "workspace"])
    )
    assert report["result"] == "PASS", json.dumps(report["groups"], indent=1)[:4000]
    frequencies = report["groups"]["frequencies"]
    ids = {case["id"]: case for case in frequencies["cases"]}
    assert set(validation.REQUIRED_CASES["frequencies"]) <= set(ids)
    # The library's week-53 reading is recorded, never asserted.
    observation = ids["library_last_week_2020:weekly_sunday"]
    assert observation["observation"] is True and observation["actual"] == [2020, 52]
    workspace = {case["id"] for case in report["groups"]["workspace"]["cases"]}
    assert set(validation.REQUIRED_CASES["workspace"]) <= workspace
    text = json.dumps(report)
    assert str(tmp_path) not in text and "Traceback" not in text


@pytest.mark.parametrize(
    "factory,groups,group,failing",
    [
        (
            "make_calendar_shifting_backend",
            ["lifecycle", "frequencies"],
            "frequencies",
            "inverse:daily",
        ),
        (
            "make_leap_ignoring_backend",
            ["lifecycle", "frequencies"],
            "frequencies",
            "adjacent:daily",
        ),
        (
            "make_boolean_coercing_backend",
            ["lifecycle", "bridge"],
            "bridge",
            "missing:boolean:nc",
        ),
        (
            "make_listing_omitting_backend",
            ["lifecycle", "workspace"],
            "workspace",
            "read_all",
        ),
    ],
)
def test_faulty_bridge_backends_cannot_produce_a_pass(
    tmp_path, child_env, factory, groups, group, failing
):
    report = validation.run(_options(tmp_path, factory, groups))
    assert report["result"] == "FAIL"
    record = report["groups"][group]
    assert record["status"] == "fail"
    statuses = {case["id"]: case["status"] for case in record["cases"]}
    assert statuses[failing] == "fail"
    assert failing in record.get("required_not_passed", [])


def test_leap_ignoring_backend_only_breaks_the_daily_calendar(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_leap_ignoring_backend", ["lifecycle", "frequencies"])
    )
    failed = sorted(
        case["id"] for case in report["groups"]["frequencies"]["cases"] if case["status"] == "fail"
    )
    assert failed == ["adjacent:daily", "year_boundary:daily"]


def test_nc_to_na_backend_is_caught_by_raw_category_assertions(tmp_path, child_env):
    """The bridge reads NC and NA alike as NaN; the raw fixture checks must not."""
    report = validation.run(
        _options(tmp_path, "make_nc_to_na_backend", ["lifecycle", "frequencies", "workspace"])
    )
    assert report["result"] == "FAIL"
    frequencies = report["groups"]["frequencies"]
    failed = {case["id"] for case in frequencies["cases"] if case["status"] == "fail"}
    assert "series_raw_categories:daily" in failed
    assert "cross_process_frequencies:values:f_daily" in failed
    assert "series_round_trip:daily" not in failed  # the lossy comparison alone would pass
    assert "series_raw_categories:monthly" not in failed
    assert "series_raw_categories:daily" in frequencies["required_not_passed"]
    workspace = report["groups"]["workspace"]
    failed = {case["id"] for case in workspace["cases"] if case["status"] == "fail"}
    assert {"b_raw_categories", "cross_process_workspace:values:B"} <= failed
    assert "read_all_values" not in failed


def test_manifests_come_from_fixtures_not_readback():
    """Cross-process expectations are built without reading the database."""
    import inspect

    source = inspect.getsource(_bridge_groups)
    for function in ("group_frequencies", "group_workspace"):
        body = inspect.getsource(getattr(_bridge_groups, function))
        manifest_part = body[body.index("objects = [") :]
        assert "_read(" not in manifest_part and "read_object" not in manifest_part, function
    assert "expected_precision_values" in source and "reference_b_values" in source


def test_configured_julia_makes_its_cases_required(tmp_path, monkeypatch):
    from famepy.validation._process import WorkerResult

    base = [{"id": case_id, "status": "pass"} for case_id in validation.REQUIRED_CASES["bridge"]]
    unsupported = [*base, {"id": "julia_differential", "status": "unsupported"}]

    def worker(payload):
        def launch(command, config, timeout, *, tokens, nested=False):
            return WorkerResult(0, payload, None, tokens=tokens)

        return launch

    monkeypatch.setattr(
        validation,
        "launch_worker",
        worker({"group": "bridge", "cases": unsupported, "counts": {}}),
    )
    options = _options(tmp_path, "make_validation_backend", ["bridge"])
    report = validation.run(options)
    assert report["groups"]["bridge"]["status"] == "pass"
    assert "julia_required" not in report["groups"]["bridge"]
    julia = {"executable": "julia", "project": "project"}
    report = validation.run({**options, "scratch": str(tmp_path / "s2"), "julia": julia})
    record = report["groups"]["bridge"]
    assert record["status"] == "fail" and record["julia_required"] is True
    assert "julia_reads_python_frequency:weekly_friday" in record["required_missing"]
    assert "python_reads_julia_kind:jkw_empty" in record["required_missing"]
    assert "julia_fame_tree_pinned" not in _julia.JULIA_REQUIRED
    complete = [*base, *[{"id": case_id, "status": "pass"} for case_id in _julia.JULIA_REQUIRED]]
    complete.append({"id": "julia_fame_tree_pinned", "status": "unsupported"})
    monkeypatch.setattr(
        validation, "launch_worker", worker({"group": "bridge", "cases": complete, "counts": {}})
    )
    report = validation.run({**options, "scratch": str(tmp_path / "s3"), "julia": julia})
    assert report["groups"]["bridge"]["status"] == "pass"
