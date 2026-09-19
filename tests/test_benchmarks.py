# SPDX-License-Identifier: MIT
"""The benchmark harness: determinism, worker protocol, result gates, honesty flags."""

import json
import os
import time
from pathlib import Path

import numpy as np
import pytest
from fake_native import make_fake

from famepy import benchmarks, migration
from famepy._runtime import Session
from famepy.benchmarks import _julia
from famepy.benchmarks.__main__ import main
from famepy.validation._process import WorkerResult

TESTS = Path(__file__).resolve().parent
PRIVATE = "/synthetic/private/credential.txt"


@pytest.fixture
def child_env(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(TESTS) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.delenv("FAME", raising=False)


def _options(tmp_path, backend, **extra):
    options = {
        "scratch": str(tmp_path / "scratch"),
        "scale": "small",
        "backend": f"fake_native:{backend}",
        "repetitions": 1,
        "cold": False,
        "scenarios": ["many_small"],
        "timeout": 120.0,
    }
    options.update(extra)
    return options


# -- fixtures and digests ---------------------------------------------------


def test_synthetic_values_are_deterministic_and_bounded():
    first = benchmarks.lcg_values(1000, 1)
    assert np.array_equal(first, benchmarks.lcg_values(1000, 1))
    assert not np.array_equal(first, benchmarks.lcg_values(1000, 2))
    assert first.min() >= 0.0 and first.max() < 1.0 and len(set(first.tolist())) > 990
    # The first values of seed 1 pin the generator (mirrored by the Julia script).
    assert first[:2].tolist() == [0.42320917087271326, 0.5094074428837206]
    dense = benchmarks.with_missing(first, 0.5, 19)
    assert 400 < int(np.isnan(dense).sum()) < 600
    assert not np.isnan(benchmarks.with_missing(first, 0.0, 19)).any()


def test_fixture_digests_use_fnv1a_over_exact_bytes():
    # Published FNV-1a 64-bit test vectors.
    assert benchmarks.fnv1a64(b"") == 0xCBF29CE484222325
    assert benchmarks.fnv1a64(b"a") == 0xAF63DC4C8601EC8C
    assert benchmarks.fnv1a64(b"foobar") == 0x85944171F73967E8
    hashes = benchmarks.fixture_hashes(benchmarks.SCALES["small"])
    assert set(hashes) == {"many_small", "few_large", "dates", "strings", "missing_density"}
    assert hashes == benchmarks.fixture_hashes(benchmarks.SCALES["small"])
    assert all(len(h) == 16 and int(h, 16) >= 0 for h in hashes.values())
    # The digest is over the values' bytes: NaN positions change it.
    values = benchmarks.lcg_values(2_000, 17)
    plain = benchmarks._hash_arrays([values])
    assert plain != benchmarks._hash_arrays([benchmarks.with_missing(values, 0.5, 19)])
    assert hashes["missing_density"] != plain


# -- instrumentation ----------------------------------------------------------


def test_counting_proxy_counts_native_calls_only():
    fake = make_fake(persist=True)
    counter = benchmarks.CountingNative(fake)
    session = Session(native=counter)
    session.initialize()
    assert counter.calls >= 1
    before = counter.calls
    assert session.version() == 11.8
    assert counter.calls == before + 1
    timer = benchmarks.Timer(counter)
    with timer.phase("noop"):
        pass
    assert timer.phases["noop"].native_calls == 0 and timer.phases["noop"].samples == []
    with timer.phase("version"), session.operation("version") as native:
        native.version()
    assert timer.phases["version"].native_calls == 1
    plain = benchmarks.Timer()
    with plain.phase("work"):
        pass
    assert len(plain.phases["work"].samples) == 1 and plain.phases["work"].native_calls is None
    session.finalize()


def test_measure_separates_timed_passes_from_the_instrumented_pass(tmp_path):
    session = Session(native=make_fake(persist=True))
    session.initialize()
    original = session._native
    scenario = benchmarks.build_scenarios(benchmarks.SCALES["small"], ["many_small"])[0]
    record = benchmarks.measure(session, scenario, tmp_path / "m", "warm", 2)
    assert session._native is original
    phases = record["phases"]
    assert len(phases["write_raw"]["seconds"]["samples"]) == 2
    assert phases["write_raw"]["native_calls"] == 40  # create plus write per object
    assert phases["post"]["native_calls"] == 1
    assert record["memory"]["tracemalloc_peak_bytes"] > 0 and record["verified"] is True
    assert set(record) == {
        "scenario",
        "mode",
        "parameters",
        "repetitions",
        "sizes",
        "verified",
        "fame_version",
        "phases",
        "memory",
        "profile_written",
    }
    # An error inside the instrumented pass still removes the proxy and the tracer.
    import tracemalloc

    def failing(session_, scratch, timer, repetition):
        if timer.counter is not None:
            raise RuntimeError("synthetic")
        return {"objects": 0}

    broken = benchmarks.Scenario("broken", {}, failing)
    with pytest.raises(RuntimeError, match="synthetic"):
        benchmarks.measure(session, broken, tmp_path / "b", "warm", 1)
    assert session._native is original and not tracemalloc.is_tracing()
    session.finalize()


# -- gates and options ---------------------------------------------------------


def test_native_benchmarks_need_the_opt_in(tmp_path):
    report = benchmarks.run({"scratch": str(tmp_path / "b"), "scale": "small"})
    assert "blocked" in report and "warm" not in report
    with pytest.raises(ValueError, match="scale"):
        benchmarks.run({"scratch": str(tmp_path / "c"), "scale": "huge", "native": True})
    with pytest.raises(ValueError, match="Unknown scenarios"):
        benchmarks.build_scenarios(benchmarks.SCALES["small"], ["nothing"])
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "file").write_text("x")
    with pytest.raises(ValueError, match="new or empty"):
        benchmarks.run({"scratch": str(tmp_path / "used"), "backend": "x:y"})


def test_harness_reports_phases_sizes_identity_and_honesty_flags(tmp_path, child_env):
    report_path = tmp_path / "bench.json"
    code = main(
        [
            "--scratch",
            str(tmp_path / "scratch"),
            "--report",
            str(report_path),
            "--backend",
            "fake_native:make_validation_backend",
            "--scale",
            "small",
            "--repetitions",
            "2",
            "--scenarios",
            "many_small,dates,missing_density",
            "--profile",
            "--source-sha",
            "abc1234",
        ]
    )
    assert code == 0
    report = json.loads(report_path.read_text())
    assert report["schema_version"] == benchmarks.SCHEMA_VERSION
    assert report["result"] == "complete" and report["failures"] == []
    assert report["library"] == {
        "backend": "injected",
        "vendor_timing": False,
        "fame_version": 11.8,
    }
    identity = report["identity"]
    assert len(identity["package_sources_sha256"]) == 64 and identity["source_sha"] == "abc1234"
    assert report["environment"]["famepy"] and report["repetitions"] == 2
    warm = report["warm"]
    assert set(warm) == {"many_small", "dates", "missing_density"}
    many = warm["many_small"]
    assert many["parameters"] == {"count": 20, "length": 24, "frequency": "monthly"}
    assert many["sizes"]["objects"] == 20 and many["sizes"]["observations"] == 480
    assert many["profile_written"] is True and many["verified"] is True
    assert (tmp_path / "scratch" / "warm-many_small" / "work" / "profile.prof").is_file()
    phases = many["phases"]
    for name in (
        "convert_to_fame",
        "write_raw",
        "post",
        "read_raw",
        "convert_from_fame",
        "read_bridge",
        "write_workspace",
        "read_workspace",
    ):
        record = phases[name]
        assert len(record["seconds"]["samples"]) == 2
        assert record["seconds"]["min"] <= record["seconds"]["median"] <= record["seconds"]["max"]
        assert isinstance(record["native_calls"], int)
    assert phases["write_raw"]["native_calls"] == 40
    assert many["memory"]["tracemalloc_peak_bytes"] > 0
    assert many["memory"]["scope"] == benchmarks.MEMORY_SCOPE
    density = warm["missing_density"]["phases"]
    assert {"write_density_00", "read_density_90", "write_workspace", "read_workspace"} <= set(
        density
    )
    cold = report["cold"]
    assert set(cold) == {"many_small", "dates", "missing_density"}
    assert cold["dates"]["startup_seconds"] > 0 and cold["dates"]["first_open_seconds"] > 0
    assert len(cold["dates"]["phases"]["write_raw"]["seconds"]["samples"]) == 1
    assert cold["dates"]["phases"]["write_raw"]["native_calls"] is None
    comparison = report["comparison"]
    assert comparison["comparable"] == [
        ["many_small", "write_workspace"],
        ["many_small", "read_workspace"],
        ["missing_density", "write_workspace"],
        ["missing_density", "read_workspace"],
    ]
    assert set(comparison["non_comparable"]) == {"dates"}
    assert set(report["fixture_hashes"]) == {
        "many_small",
        "few_large",
        "dates",
        "strings",
        "missing_density",
    }
    assert report["instrumentation"] == benchmarks.INSTRUMENTATION_NOTE
    text = json.dumps(report)
    assert str(tmp_path) not in text and "Traceback" not in text


@pytest.mark.skipif(
    not migration.dataecon_available()[0], reason="DataEcon native extension unavailable"
)
def test_migration_scenario_times_plan_migrate_and_readback(tmp_path, child_env):
    report = benchmarks.run(_options(tmp_path, "make_fake", scenarios=["migration"]))
    record = report["warm"]["migration"]
    assert set(record["phases"]) == {"plan", "migrate", "read_back"}
    assert record["sizes"]["complete"] is True and record["sizes"]["objects"] == 20
    assert report["result"] == "complete"


def test_blocked_migration_scenario_is_a_reported_failure(tmp_path, child_env, monkeypatch):
    session = Session(native=make_fake(persist=True))
    session.initialize()
    monkeypatch.setattr(migration, "dataecon_available", lambda: (False, "ImportError"))
    scenario = benchmarks.build_scenarios(benchmarks.SCALES["small"], ["migration"])[0]
    record = benchmarks.measure(session, scenario, tmp_path / "m", "warm", 1)
    session.finalize()
    assert record == {
        "scenario": "migration",
        "mode": "warm",
        "blocked": "DataEcon native extension unavailable: ImportError",
    }
    assert (
        benchmarks.validate_measurement(
            {**record, "token": "t", "complete": True}, scenario, "warm"
        )
        is None
    )
    # A block string outside the allowlist is not a block, it is an invalid result.
    forged = {**record, "blocked": "see " + PRIVATE, "token": "t", "complete": True}
    assert benchmarks.validate_measurement(forged, scenario, "warm") == "invalid_result"


# -- worker protocol ---------------------------------------------------------------


def test_timeout_terminates_a_hanging_worker_and_fails_the_run(tmp_path, child_env):
    started = time.monotonic()
    report = benchmarks.run(_options(tmp_path, "make_hanging_backend", timeout=2.0))
    assert time.monotonic() - started < 60
    assert report["warm"]["many_small"]["error"] == "timeout"
    assert report["result"] == "incomplete"
    assert report["failures"] == [
        {
            "measurement": "warm:many_small",
            "error": "timeout",
            "duration_seconds": report["warm"]["many_small"]["duration_seconds"],
        }
    ]


def test_backend_failure_and_corrupted_reads_cannot_yield_timings(tmp_path, child_env):
    report = benchmarks.run(_options(tmp_path, "make_failing_backend"))
    record = report["warm"]["many_small"]
    assert record["error"] == "backend_setup_failed" and record["error_type"] == "FameError"
    assert report["result"] == "incomplete"
    report = benchmarks.run(_options(tmp_path / "c", "make_benchmark_corrupting_backend"))
    record = report["warm"]["many_small"]
    assert record == {
        "error": "scenario_failed",
        "error_type": "BenchmarkFidelityError",
        "stray_output_bytes": record["stray_output_bytes"],
    }
    assert "phases" not in record and report["result"] == "incomplete"


def test_cli_exits_nonzero_when_any_measurement_fails(tmp_path, child_env, capsys):
    code = main(
        [
            "--scratch",
            str(tmp_path / "s"),
            "--report",
            str(tmp_path / "r.json"),
            "--backend",
            "fake_native:make_benchmark_corrupting_backend",
            "--scenarios",
            "many_small",
            "--repetitions",
            "1",
            "--no-cold",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "warm many_small: failed (scenario_failed)" in out and "result: incomplete" in out


def _worker(payload, *, returncode=0, kind=None):
    def launch(command, config, timeout, *, tokens, nested=False):
        result = WorkerResult(returncode, tokens=tokens)
        if payload is not None:
            result.payload = {**payload, "token": tokens["token"], "complete": True}
        else:
            result.result_kind = kind
        return result

    return launch


def _measurement(scenario):
    payload = {
        "scenario": scenario.name,
        "mode": "warm",
        "parameters": scenario.parameters,
        "repetitions": 1,
        "sizes": {"objects": 20, "observations": 480, "database_bytes": 10},
        "verified": True,
        "fame_version": 11.8,
        "phases": {
            "write_raw": {
                "seconds": {"samples": [0.001], "min": 0.001, "median": 0.001, "max": 0.001},
                "native_calls": 40,
            }
        },
        "memory": {"tracemalloc_peak_bytes": 1, "peak_rss_bytes": None},
        "profile_written": False,
    }
    phase = payload["phases"]["write_raw"]
    payload["phases"] = {
        name: {"seconds": dict(phase["seconds"]), "native_calls": 40}
        for name in benchmarks._SCENARIO_PHASES[scenario.name]
    }
    return payload


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, None),
        ({"scenario": "few_large"}, "wrong_scenario"),
        ({"mode": "cold"}, "wrong_scenario"),
        ({"note": PRIVATE}, "invalid_result"),
        ({"verified": False}, "invalid_result"),
        ({"parameters": {"count": 21, "length": 24, "frequency": "monthly"}}, "invalid_result"),
        ({"sizes": {"path": PRIVATE}}, "invalid_result"),
        ({"phases": {}}, "invalid_result"),
        ({"phases": {"write raw": {"seconds": {}, "native_calls": 1}}}, "invalid_result"),
        (
            {
                "phases": {
                    "write_raw": {
                        "seconds": {"samples": ["1"], "min": 1, "median": 1, "max": 1},
                        "native_calls": 1,
                    }
                }
            },
            "invalid_result",
        ),
        (
            {
                "phases": {
                    "write_raw": {
                        "seconds": {"samples": [1.0, 2.0], "min": 1, "median": 1, "max": 2},
                        "native_calls": 1,
                    }
                }
            },
            "invalid_result",
        ),
        ({"memory": {"tracemalloc_peak_bytes": PRIVATE, "peak_rss_bytes": 1}}, "invalid_result"),
        ({"repetitions": True}, "invalid_result"),
        ({"fame_version": "11.8 " + PRIVATE}, "invalid_result"),
    ],
)
def test_measurement_allowlist(change, expected):
    scenario = benchmarks.build_scenarios(benchmarks.SCALES["small"], ["many_small"])[0]
    payload = {**_measurement(scenario), **change, "token": "t", "complete": True}
    assert benchmarks.validate_measurement(payload, scenario, "warm") == expected


@pytest.mark.parametrize(
    "child,error",
    [
        ("missing_result", "missing_result"),
        ("stale_result", "stale_result"),
        ("partial_result", "partial_result"),
        ("oversized_result", "oversized_result"),
        ("invalid_result", "invalid_result"),
        (30, "invalid_configuration"),
        (31, "backend_setup_failed"),
        (32, "scenario_failed"),
        (7, "worker_failed"),
        ({"scenario": "few_large"}, "wrong_scenario"),
        ({"note": PRIVATE}, "invalid_result"),
    ],
)
def test_worker_result_gates(tmp_path, monkeypatch, child, error):
    scenario = benchmarks.build_scenarios(benchmarks.SCALES["small"], ["many_small"])[0]
    if isinstance(child, int):
        launch = _worker(
            {"scenario": "many_small", "mode": "warm", "error_type": "X"}, returncode=child
        )
    elif isinstance(child, str):
        launch = _worker(None, kind=child)
    else:
        launch = _worker({**_measurement(scenario), **child})
    monkeypatch.setattr(benchmarks, "launch_worker", launch)
    record = benchmarks.run_measurement(
        scenario, "warm", {"scale": "small", "backend": "x:y", "repetitions": 1}, tmp_path, 10.0
    )
    assert record["error"] == error
    assert PRIVATE not in json.dumps(record)
    good = _worker(_measurement(scenario))
    monkeypatch.setattr(benchmarks, "launch_worker", good)
    (tmp_path / "g").mkdir()
    record = benchmarks.run_measurement(
        scenario,
        "warm",
        {"scale": "small", "backend": "x:y", "repetitions": 1},
        tmp_path / "g",
        10.0,
    )
    assert "error" not in record and record["phases"]["write_raw"]["native_calls"] == 40


def test_worker_rejects_bad_configuration(tmp_path):
    assert benchmarks.worker_main({"scale": "small"}) == 30
    assert benchmarks.worker_main({"scale": "huge", "mode": "warm", "scenario": "many_small"}) == 30
    config = {"scale": "small", "mode": "hot", "scenario": "many_small", "scratch": str(tmp_path)}
    assert benchmarks.worker_main(config) == 30


# -- Julia comparison ------------------------------------------------------------------


def test_julia_benchmark_script_is_ascii_and_mirrors_the_generator():
    assert _julia.SCRIPT.isascii()
    assert "6364136223846793005" in _julia.SCRIPT and "1442695040888963407" in _julia.SCRIPT
    assert "0xcbf29ce484222325" in _julia.SCRIPT and "0x100000001b3" in _julia.SCRIPT
    for scenario in benchmarks.COMPARABLE_SCENARIOS:
        assert f'"{scenario}"' in _julia.SCRIPT
    for scenario in benchmarks.NON_COMPARABLE:
        assert f'"{scenario}"' not in _julia.SCRIPT
    assert "\\" not in _julia.SCRIPT.split("\n")[1]


def _julia_payload(**changes):
    payload = {
        "group": "benchmark",
        "token": "t",
        "complete": True,
        "fame_tree_hash": "a" * 40,
        "julia_version": "1.11.2",
        "phases": {
            name: {"write_workspace": [0.5], "read_workspace": [0.25]}
            for name in benchmarks.COMPARABLE_SCENARIOS
        },
        "fixture_hashes": {name: "0" * 16 for name in benchmarks.COMPARABLE_SCENARIOS},
    }
    payload.update(changes)
    return payload


def test_julia_result_allowlist_rejects_unexpected_fields():
    accepted = _julia.validate_julia_result(_julia_payload(), 1)
    assert accepted is not None and accepted["fame_tree_pinned"] is False
    assert accepted["phases"]["many_small"]["read_workspace"]["seconds"]["median"] == 0.25
    for change in (
        {"unexpected_private_field": PRIVATE},
        {"group": "julia"},
        {"julia_version": PRIVATE},
        {"fame_tree_hash": PRIVATE},
        {"fixture_hashes": {"many_small": "0" * 16}},
        {"phases": {**_julia_payload()["phases"], "dates": {"write_workspace": [1.0]}}},
        {"phases": {**_julia_payload()["phases"], "many_small": {"write_workspace": [1.0]}}},
    ):
        assert _julia.validate_julia_result(_julia_payload(**change), 1) is None
    assert _julia.validate_julia_result(_julia_payload(), 2) is None


def test_julia_run_uses_the_result_file_protocol(tmp_path, monkeypatch):
    seen = {}

    def fake_run_child(command, input_text, timeout, *, nested=False, output_path=None):
        seen["command"] = command
        result_path, token = command[5], command[6]
        Path(result_path).write_text(json.dumps(_julia_payload(token=token)), encoding="ascii")
        Path(output_path).write_text("stray output " + PRIVATE)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(_julia, "run_child", fake_run_child)
    got = _julia.run_julia_benchmark(
        {"executable": "unused", "project": "unused"}, tmp_path, benchmarks.SCALES["small"], 1, 1
    )
    assert "error" not in got and PRIVATE not in json.dumps(got)
    assert set(got) == {
        "fame_tree_hash",
        "fame_tree_pinned",
        "julia_version",
        "phases",
        "fixture_hashes",
        "note",
    }

    def stale(command, input_text, timeout, *, nested=False, output_path=None):
        Path(command[5]).write_text(json.dumps(_julia_payload(token="other")), encoding="ascii")
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(_julia, "run_child", stale)
    (tmp_path / "s").mkdir()
    got = _julia.run_julia_benchmark(
        {"executable": "unused", "project": "unused"},
        tmp_path / "s",
        benchmarks.SCALES["small"],
        1,
        1,
    )
    assert got == {"error": "stale_result"}


def test_julia_fixture_mismatch_removes_the_comparable_pairs(tmp_path, child_env, monkeypatch):
    def forged(julia, scratch, scale, repetitions, timeout):
        accepted = _julia.validate_julia_result(_julia_payload(), repetitions)
        assert accepted is not None
        return accepted

    monkeypatch.setattr(_julia, "run_julia_benchmark", forged)
    report = benchmarks.run(
        _options(tmp_path, "make_fake", julia={"executable": "unused", "project": "unused"})
    )
    assert report["comparison"]["julia_fixtures_match"] == {
        "many_small": False,
        "few_large": False,
        "missing_density": False,
    }
    assert report["comparison"]["comparable"] == []
    assert {"measurement": "julia", "error": "fixture_mismatch"} in report["failures"]
    assert report["result"] == "incomplete"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.0, 10**400])
def test_measurement_rejects_invalid_durations(invalid):
    scenario = benchmarks.build_scenarios(benchmarks.SCALES["small"], ["many_small"])[0]
    payload = {**_measurement(scenario), "token": "t", "complete": True}
    payload["phases"]["write_raw"]["seconds"]["samples"] = [invalid]
    assert benchmarks.validate_measurement(payload, scenario, "warm") == "invalid_result"
    julia = _julia_payload()
    julia["phases"]["many_small"]["read_workspace"] = [invalid]
    assert _julia.validate_julia_result(julia, 1) is None


def test_measurement_rejects_unknown_labels_and_inconsistent_work():
    import copy

    scenario = benchmarks.build_scenarios(benchmarks.SCALES["small"], ["many_small"])[0]
    base = {**_measurement(scenario), "token": "t", "complete": True}
    wrong = copy.deepcopy(base)
    wrong["sizes"]["unexpected_label"] = 1
    assert benchmarks.validate_measurement(wrong, scenario, "warm") == "invalid_result"
    wrong = copy.deepcopy(base)
    wrong["phases"].pop("read_workspace")
    assert benchmarks.validate_measurement(wrong, scenario, "warm") == "invalid_result"
    wrong = copy.deepcopy(base)
    wrong["memory"]["peak_rss_bytes"] = -1
    assert benchmarks.validate_measurement(wrong, scenario, "warm") == "invalid_result"
    wrong = copy.deepcopy(base)
    wrong["phases"]["write_raw"]["seconds"]["median"] = 99.0
    assert benchmarks.validate_measurement(wrong, scenario, "warm") == "invalid_result"
    assert (
        benchmarks.validate_measurement(base, scenario, "warm", expected_repetitions=2)
        == "invalid_result"
    )


@pytest.mark.parametrize(
    "options",
    [
        {"repetitions": 0},
        {"repetitions": -1},
        {"repetitions": True},
        {"repetitions": 1.5},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"timeout": 3601},
        {"timeout": True},
    ],
)
def test_invalid_limits_do_not_start_work_or_create_scratch(tmp_path, options):
    scratch = tmp_path / "unused"
    with pytest.raises(ValueError):
        benchmarks.run({"scratch": str(scratch), "backend": "fake_native:make_fake", **options})
    assert not scratch.exists()
