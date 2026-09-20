# SPDX-License-Identifier: MIT
"""One-shot lifecycle across process boundaries and non-NaN sentinel profiles."""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from canonical import read, scalar_object, series_object, value, write
from fake_native import FINITE_SENTINELS, SENTINELS, make_fake, make_finite_sentinel_backend
from tsecon import TSeries, mm

import famepy
from famepy import bridge

TESTS = Path(__file__).resolve().parent


def _environment():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(TESTS) + os.pathsep + environment.get("PYTHONPATH", "")
    environment.pop("FAME", None)
    return environment


_SCRIPT = """
import json, sys
from fake_native import make_fake
import famepy
fake = make_fake(persist=False)
owner = famepy.Session(native=fake).initialize()
owner.finalize()
try:
    famepy.Session(native=fake).initialize()
    second = "initialized"
except famepy.RuntimeStateError:
    second = "rejected"
print(json.dumps({"init": fake.fake.init_count, "fin": fake.fake.fin_count, "second": second}))
"""


def test_each_spawned_process_gets_exactly_one_lifecycle():
    """Two processes each initialize once; neither restarts after finalization."""
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
            env=_environment(),
            check=True,
        )
        outputs.append(json.loads(result.stdout.strip().splitlines()[-1]))
    assert outputs == [{"init": 1, "fin": 1, "second": "rejected"}] * 2


def test_fresh_process_child_group_runs_after_parent_finalized(tmp_path):
    fake = make_fake(persist=False)
    owner = famepy.Session(native=fake).initialize()
    owner.finalize()
    command = [sys.executable, "-m", "famepy.validation._child", "--group", "fresh_process"]
    config = json.dumps({"scratch": str(tmp_path), "backend": "fake_native:make_fake"})
    result = subprocess.run(
        command, input=config, capture_output=True, text=True, timeout=120, env=_environment()
    )
    assert result.returncode == 0
    cases = {case["id"]: case["status"] for case in json.loads(result.stdout)["cases"]}
    assert cases == {
        "initialize": "pass",
        "version": "pass",
        "version_is_positive": "pass",
        "finalize": "pass",
        "finalized_state": "pass",
    }
    with pytest.raises(famepy.RuntimeStateError):
        owner.initialize()


# -- distinct finite (non-NaN) floating sentinels -------------------------------


@pytest.fixture
def finite_db(tmp_path):
    adapter = make_finite_sentinel_backend()
    adapter.fake.persist = False
    session = famepy.Session(native=adapter).initialize()
    database = famepy.opendb("finite", "create", session=session)
    yield database
    famepy.closedb(database)
    session.finalize()


def test_finite_profile_is_distinct_from_the_nan_profile():
    assert not any(
        math.isnan(v)
        for v in (
            FINITE_SENTINELS.precision_nc,
            FINITE_SENTINELS.precision_na,
            FINITE_SENTINELS.precision_nd,
        )
    )
    assert math.isnan(SENTINELS.precision_nc)
    assert len({FINITE_SENTINELS.precision_nc, FINITE_SENTINELS.precision_na}) == 2


def test_raw_round_trip_preserves_finite_sentinels(finite_db):
    s = finite_db.session.sentinels
    assert s is FINITE_SENTINELS
    values = np.array([1.0, s.precision_nc, s.precision_na, s.precision_nd, 2.0])
    famepy.do_write(series_object("p", "precision", "monthly", 0, values), finite_db)
    raw = read(finite_db, "p")
    assert np.array_equal(raw.data, values)
    assert not np.isnan(raw.data).any()
    codes = famepy.classify_by_sentinel(raw.data, "precision", s)
    assert codes.tolist() == [0, 1, 2, 3, 0]
    assert [famepy.missing_type(finite_db, "precision", v) for v in raw.data] == [0, 1, 2, 3, 0]
    numeric = np.array([s.numeric_nc, np.float32(1.5), s.numeric_nd], dtype=np.float32)
    famepy.do_write(series_object("n", "numeric", "monthly", 0, numeric), finite_db)
    got = read(finite_db, "n").data
    assert np.array_equal(got.view(np.uint32), numeric.view(np.uint32))
    assert famepy.classify_by_sentinel(got, "numeric", s).tolist() == [1, 0, 3]
    assert [famepy.missing_type(finite_db, "numeric", v) for v in got] == [1, 0, 3]
    famepy.do_write(scalar_object("ps", "precision", s.precision_na), finite_db)
    scalar = read(finite_db, "ps").data
    assert scalar == s.precision_na and famepy.missing_type(finite_db, "precision", scalar) == 2
    # An ordinary NaN is a normal value under this profile, not a missing code.
    famepy.do_write(scalar_object("nan", "precision", math.nan), finite_db)
    assert famepy.missing_type(finite_db, "precision", read(finite_db, "nan").data) == 0


def test_bridge_missing_conventions_under_finite_sentinels(finite_db):
    s = finite_db.session.sentinels
    ts = TSeries(mm(2020, 1), np.array([1.0, np.nan, 3.0]))
    write(finite_db, "ts", ts)
    raw = read(finite_db, "ts")
    assert raw.data[1] == s.precision_nc and not np.isnan(raw.data[1])
    back = value(finite_db, "ts")
    assert np.array_equal(back.values, ts.values, equal_nan=True)
    with pytest.raises(bridge.MissingValueError):
        value(finite_db, "ts", missing="strict")
    famepy.do_write(
        series_object(
            "all",
            "precision",
            "monthly",
            0,
            np.array([s.precision_nc, s.precision_na, s.precision_nd]),
        ),
        finite_db,
    )
    assert np.isnan(value(finite_db, "all").values).all()
    write(finite_db, "sc", math.nan)
    stored = read(finite_db, "sc").data
    assert stored == s.precision_nc and math.isnan(value(finite_db, "sc"))
    with pytest.raises(bridge.MissingValueError):
        value(finite_db, "sc", missing="strict")
    write(finite_db, "empty", TSeries(mm(2020, 3), np.empty(0)), empty="reference")
    stored_empty = read(finite_db, "empty")
    assert stored_empty.data[0] == s.precision_na
    assert len(value(finite_db, "empty", empty="reference")) == 0


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, OSError])
@pytest.mark.parametrize("action", ["initialize", "finalize"])
def test_native_boundary_exception_consumes_attempt(monkeypatch, action, failure_type):
    backend = make_fake(persist=False)
    owner = famepy.Session(native=backend)
    if action == "finalize":
        owner.initialize()
    calls = []

    def fail():
        calls.append(1)
        raise failure_type()

    monkeypatch.setattr(backend, action, fail)
    with pytest.raises(failure_type):
        getattr(owner, action)()
    assert owner.is_terminal
    with pytest.raises(famepy.RuntimeStateError):
        owner.initialize()
    with pytest.raises(famepy.RuntimeStateError):
        famepy.Session(native=backend).initialize()
    owner.finalize()
    assert calls == [1]


def test_setup_cleanup_interrupt_preserves_original_error(monkeypatch):
    backend = make_fake(persist=False)
    owner = famepy.Session(native=backend)
    original = ValueError("synthetic setup failure")
    calls = []

    def setup():
        raise original

    def cleanup():
        calls.append(1)
        raise KeyboardInterrupt()

    monkeypatch.setattr(backend, "sentinels", setup)
    monkeypatch.setattr(backend, "finalize", cleanup)
    with pytest.raises(ValueError) as caught:
        owner.initialize()
    assert caught.value is original
    assert owner.state == "broken"
    owner.finalize()
    assert calls == [1]


def test_lifecycle_runner_accepts_unloaded_native_wrapper(tmp_path, monkeypatch):
    from famepy._discovery import discover
    from famepy.validation._groups import Context, group_lifecycle
    from famepy.validation._report import Recorder

    library = tmp_path / "synthetic.dll"
    library.touch()
    candidate = discover(library)
    backend = make_fake(persist=False)
    owner = famepy.Session(native=backend)
    recorder = Recorder()
    ctx = Context(owner, tmp_path, recorder, lambda args: args, 10, None)
    ctx.new_session = lambda: famepy.Session(candidate)
    monkeypatch.setattr(ctx, "fresh_process", lambda case_id: None)
    group_lifecycle(ctx)
    cases = {case.id: case for case in recorder.cases}
    assert cases["new_wrapper_rejected"].status == "pass"
    assert cases["new_wrapper_untouched"].status == "pass"
    assert not any(case.status == "fail" for case in recorder.cases)


def test_module_initialize_rejects_inherited_active_default(monkeypatch):
    from famepy import _runtime

    owner = famepy.Session(native=make_fake(persist=False)).initialize()
    monkeypatch.setattr(_runtime, "_DEFAULT", owner)
    _runtime._after_fork()
    with pytest.raises(famepy.InheritedRuntimeError):
        famepy.init_chli()
