# SPDX-License-Identifier: MIT
"""Bounded, deterministic native benchmarks with honest scope.

``python -m famepy.benchmarks`` times the package's read and write paths on
synthetic data of fixed shapes and seeds: many small series, a few large
ones, date conversion, strings, missing density and the DataEcon
migration. Every scenario separates its phases (conversion, native write,
post, native read, bridge conversion, workspace forms), verifies what it
read back against what it wrote outside the timed regions, and records
the repetitions, their spread, the sizes involved, the number of native
calls made through the package's own boundary, Python-side peak
allocation and the process peak resident size where the platform reports
it. Timed samples run without instrumentation; the native call counts and
the memory figures come from one separate instrumented pass, so that the
counting proxy, ``tracemalloc`` and ``cProfile`` never sit inside a
reported timing.

Every measurement runs in its own worker process under the validation
runner's worker protocol: a fresh result file and token per launch, the
worker's streams redirected to a local log that is never parsed, a
deadline that terminates the worker's whole process tree, and a strict
allowlist on the result (numbers and fixed labels only; any other field
rejects the result). A warm measurement repeats the scenario inside one
worker after an untimed pass; a cold measurement is a fresh process that
also times ``initialize`` and the first database open. "Cold" means a
fresh interpreter, not a cold file-system cache.

The report carries the runtime FAME version, the library file identity
(size and digest, never a path), the installed package identity (source
digest, optional wheel comparison) and says whether the timings are
vendor timings at all: with an injected backend they are not, and the
report says so. No speed claim is derived here; the numbers are inputs
for a review. The optional Julia comparison runs FAME.jl on the same host
on the scenarios whose fixtures and phase boundaries are equivalent,
checks the fixture hashes both sides computed, and is reported next to
the Python numbers, never merged with them and never as a ratio.
"""

from __future__ import annotations

import cProfile
import datetime as dt
import gc
import hashlib
import importlib
import importlib.metadata
import math
import operator
import platform
import re
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

import famepy
from famepy import bridge
from famepy._errors import error_number
from famepy._native import NativeInterface
from famepy._runtime import Session
from famepy.validation._process import (
    WorkerResult,
    launch_worker,
    redirect_streams,
    reserve_result,
    write_result,
)

SCHEMA_VERSION = 2
SCALES: dict[str, dict[str, int]] = {
    # Bounded shapes; "small" runs in seconds against the fake and is what
    # the offline tests use, "standard" is the native campaign size.
    "small": {
        "many_small_count": 20,
        "many_small_length": 24,
        "few_large_count": 3,
        "few_large_length": 2_000,
        "dates_length": 200,
        "strings_length": 200,
        "missing_length": 2_000,
        "repetitions": 3,
    },
    "standard": {
        "many_small_count": 500,
        "many_small_length": 24,
        "few_large_count": 3,
        "few_large_length": 500_000,
        "dates_length": 20_000,
        "strings_length": 20_000,
        "missing_length": 200_000,
        "repetitions": 5,
    },
}
SCENARIOS = ("many_small", "few_large", "dates", "strings", "missing_density", "migration")
MISSING_DENSITIES = (0.0, 0.1, 0.5, 0.9)
# Scenarios and phases with the same fixture values and the same phase
# boundary (a whole workspace written or read, database opened and closed
# inside) in the Python harness and the Julia script.
COMPARABLE_SCENARIOS = ("many_small", "few_large", "missing_density")
COMPARABLE_PHASES = ("write_workspace", "read_workspace")
NON_COMPARABLE: dict[str, str] = {
    "dates": "the Python fixture has missing dates, which the reference script does not write",
    "strings": "the Python fixture has missing strings, which the reference script does not write",
    "migration": "no reference equivalent",
}
MODES = ("warm", "cold")
# The index of every fixture: every scenario spans a valid calendar range at
# both scales (the standard missing-density fixture is 200 000 days from
# 2000-01-03, well inside year 9999; 200 000 months would not be).
FIXTURE_INDEX: dict[str, dict[str, str]] = {
    "many_small": {"frequency": "monthly", "start": "2000M1"},
    "few_large": {"frequency": "daily", "start": "2000-01-03"},
    "dates": {"frequency": "monthly", "start": "2000M1"},
    "strings": {"frequency": "case", "start": "1"},
    "missing_density": {"frequency": "daily", "start": "2000-01-03"},
    "migration": {"frequency": "monthly", "start": "2000M1"},
}
LAST_CALENDAR_YEAR = 9999
# The native operations a failure record may name: the package's own
# boundary methods, nothing the library or a scenario could invent.
OPERATIONS: frozenset[str] = frozenset(
    name for name, value in vars(NativeInterface).items() if callable(value) and name[0] != "_"
)
MAX_STATUS = 2**31
_LCG_MULTIPLIER = 6364136223846793005
_LCG_INCREMENT = 1442695040888963407
_MASK = (1 << 64) - 1
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_HASH = re.compile(r"^[0-9a-f]{16}$")
_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_BLOCK = re.compile(r"^DataEcon native extension unavailable: [A-Za-z_][A-Za-z0-9_]{0,63}$")
_PHASE = re.compile(r"^[a-z_0-9]{1,40}$")


class BenchmarkFidelityError(AssertionError):
    """A scenario read back something other than what it wrote; its timings are void."""


# -- deterministic synthetic values ------------------------------------------


def lcg_values(count: int, seed: int) -> np.ndarray:
    """``count`` uniform doubles in [0, 1) from a 64-bit LCG (mirrored in Julia)."""
    out = np.empty(count, dtype=np.float64)
    state = seed & _MASK
    for position in range(count):
        state = (_LCG_MULTIPLIER * state + _LCG_INCREMENT) & _MASK
        out[position] = (state >> 11) / float(1 << 53)
    return out


def with_missing(values: np.ndarray, density: float, seed: int) -> np.ndarray:
    picks = lcg_values(len(values), seed)
    out = np.array(values, copy=True)
    out[picks < density] = np.nan
    return out


def fnv1a64(data: bytes) -> int:
    """FNV-1a, 64-bit (mirrored in Julia over the same little-endian bytes)."""
    value = _FNV_OFFSET
    for byte in data:
        value = ((value ^ byte) * _FNV_PRIME) & _MASK
    return value


def _join_text(values: Any) -> bytes:
    return b"\x00".join((value or "").encode("ascii") for value in values)


def _hash_arrays(arrays: list[np.ndarray]) -> str:
    return f"{fnv1a64(b''.join(np.ascontiguousarray(a).tobytes() for a in arrays)):016x}"


def _ts() -> Any:
    import tsecon

    return tsecon


def _series_batch(count: int, length: int, frequency: Any, seed: int) -> dict[str, Any]:
    ts = _ts()
    first = ts.mm(2000, 1) if isinstance(frequency, ts.Monthly) else ts.daily("2000-01-03")
    return {
        f"s{index:05d}": ts.TSeries(first, lcg_values(length, seed + index))
        for index in range(count)
    }


def _dates_fixture(length: int) -> Any:
    ts = _ts()
    base = ts.daily("2000-01-01")
    offsets = lcg_values(length, 11)
    values = [None if pick < 0.05 else base + int(pick * 20_000) for pick in offsets]
    return bridge.DateSeries(ts.mm(2000, 1), values)


def _strings_fixture(length: int) -> Any:
    ts = _ts()
    picks = lcg_values(length, 13)
    values = [None if pick < 0.05 else f"item{int(pick * 1_000_000):07d}" for pick in picks]
    return bridge.StringSeries(ts.MIT(ts.Unit(), 1), values)


def _missing_fixture(length: int) -> dict[str, Any]:
    ts = _ts()
    base = lcg_values(length, 17)
    first = ts.daily("2000-01-03")
    return {
        f"d{int(density * 100):02d}": ts.TSeries(first, with_missing(base, density, 19))
        for density in MISSING_DENSITIES
    }


def fixture_domains(scale: dict[str, int]) -> dict[str, dict[str, Any]]:
    """Index frequency, first moment (as its integer) and length per fixture.

    Computed from the fixtures themselves, not from ``FIXTURE_INDEX``; the
    Julia script reports the same record from its own fixtures and the
    coordinator compares both with the declared index.
    """
    ts = _ts()
    many = _series_batch(scale["many_small_count"], scale["many_small_length"], ts.Monthly(), 1)
    large = _series_batch(scale["few_large_count"], scale["few_large_length"], ts.Daily(), 7)
    dates = _dates_fixture(scale["dates_length"])
    strings = _strings_fixture(scale["strings_length"])
    missing = _missing_fixture(scale["missing_length"])

    def domain(series: Any) -> dict[str, Any]:
        return {
            "frequency": _index_name(series.firstdate.frequency),
            "start": int(series.firstdate),
            "length": len(series),
        }

    return {
        "many_small": domain(next(iter(many.values()))),
        "few_large": domain(next(iter(large.values()))),
        "dates": domain(dates),
        "strings": domain(strings),
        "missing_density": domain(next(iter(missing.values()))),
    }


def _index_name(frequency: Any) -> str:
    ts = _ts()
    if isinstance(frequency, ts.Daily):
        return "daily"
    if isinstance(frequency, ts.Monthly):
        return "monthly"
    if isinstance(frequency, ts.Unit):
        return "case"
    return type(frequency).__name__.lower()


def fixture_end_year(frequency: str, start: int, length: int) -> int:
    """The calendar year of the last observation of a fixture (case: 0)."""
    if frequency == "daily":
        return dt.date.fromordinal(start + length - 1).year
    if frequency == "monthly":
        return (start + length - 1) // 12
    return 0


def fixture_hashes(scale: dict[str, int]) -> dict[str, str]:
    """FNV-1a digests of the exact fixture bytes per scenario (mirrored in Julia)."""
    ts = _ts()
    many = _series_batch(scale["many_small_count"], scale["many_small_length"], ts.Monthly(), 1)
    large = _series_batch(scale["few_large_count"], scale["few_large_length"], ts.Daily(), 7)
    dates = _dates_fixture(scale["dates_length"])
    strings = _strings_fixture(scale["strings_length"])
    missing = _missing_fixture(scale["missing_length"])
    return {
        "many_small": _hash_arrays([value.values for value in many.values()]),
        "few_large": _hash_arrays([value.values for value in large.values()]),
        "dates": _hash_arrays(
            [np.array([-1 if v is None else int(v) for v in dates.values], dtype=np.int64)]
        ),
        "strings": f"{fnv1a64(_join_text(strings.values)):016x}",
        "missing_density": _hash_arrays([value.values for value in missing.values()]),
    }


# -- measurement helpers -----------------------------------------------------


class CountingNative:
    """Proxy over the native boundary that counts calls per method name."""

    def __init__(self, native: Any) -> None:
        self._native = native
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._native, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return attribute(*args, **kwargs)

        return call


def peak_rss_bytes() -> int | None:
    """Process peak resident size since start (platform report), or None."""
    try:
        if sys.platform == "win32":
            import ctypes as ct

            class Counters(ct.Structure):
                _fields_ = [
                    ("cb", ct.c_uint32),
                    ("PageFaultCount", ct.c_uint32),
                    ("PeakWorkingSetSize", ct.c_size_t),
                    ("WorkingSetSize", ct.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ct.c_size_t),
                    ("QuotaPagedPoolUsage", ct.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ct.c_size_t),
                    ("QuotaNonPagedPoolUsage", ct.c_size_t),
                    ("PagefileUsage", ct.c_size_t),
                    ("PeakPagefileUsage", ct.c_size_t),
                ]

            counters = Counters()
            counters.cb = ct.sizeof(Counters)
            windll = getattr(ct, "WinDLL")  # noqa: B009 - absent on POSIX stubs
            psapi = windll("psapi")
            kernel = windll("kernel32")
            kernel.GetCurrentProcess.restype = ct.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [ct.c_void_p, ct.POINTER(Counters), ct.c_uint32]
            psapi.GetProcessMemoryInfo.restype = ct.c_int
            if not psapi.GetProcessMemoryInfo(
                kernel.GetCurrentProcess(), ct.byref(counters), counters.cb
            ):
                return None
            return int(counters.PeakWorkingSetSize)
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(usage) * (1 if sys.platform == "darwin" else 1024)
    except Exception:  # noqa: BLE001 - a missing counter is reported as None
        return None


@dataclass
class Phase:
    samples: list[float] = field(default_factory=list)
    native_calls: int | None = None

    def to_json(self) -> dict[str, Any]:
        seconds = sorted(self.samples)
        middle = len(seconds) // 2
        median = (
            None
            if not seconds
            else seconds[middle]
            if len(seconds) % 2
            else (seconds[middle - 1] + seconds[middle]) / 2
        )
        return {
            "seconds": {
                "samples": [round(s, 6) for s in self.samples],
                "min": round(seconds[0], 6) if seconds else None,
                "median": round(median, 6) if median is not None else None,
                "max": round(seconds[-1], 6) if seconds else None,
            },
            "native_calls": self.native_calls,
        }


class Timer:
    """Collects phase timings for one scenario across repetitions.

    With ``counter`` (the instrumented pass) the native calls per phase are
    recorded and the elapsed time is not; without it, only the time is.
    """

    def __init__(self, counter: CountingNative | None = None) -> None:
        self.phases: dict[str, Phase] = {}
        self.counter = counter

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        global _phase_in_progress
        record = self.phases.setdefault(name, Phase())
        _phase_in_progress = name
        # Leave the label on exceptions; clear it after either successful path.
        if self.counter is not None:
            before = self.counter.calls
            yield
            record.native_calls = self.counter.calls - before
        else:
            gc.collect()
            started = time.perf_counter()
            yield
            record.samples.append(time.perf_counter() - started)
        _phase_in_progress = None


# The phase a scenario is in (a worker runs one scenario at a time).
_phase_in_progress: str | None = None


def failure_record(error: BaseException, scenario: str, mode: str) -> dict[str, Any]:
    """The bounded diagnostics of a failed measurement: class name, numeric
    status, an operation from the fixed allowlist and the phase in progress.
    Nothing textual from the exception, the library or the scenario is
    copied, so a private path or message cannot ride along.
    """
    global _phase_in_progress
    record: dict[str, Any] = {"scenario": scenario, "mode": mode}
    kind = type(error).__name__
    if _ERROR_TYPE.fullmatch(kind):
        record["error_type"] = kind
    status = error_number(getattr(error, "status", None))
    if status is not None:
        record["status"] = status
    operation = getattr(error, "operation", None)
    if isinstance(operation, str) and operation in OPERATIONS:
        record["operation"] = operation
    if _phase_in_progress in PHASE_LABELS:
        record["phase"] = _phase_in_progress
    _phase_in_progress = None
    return record


# -- scenarios ----------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    parameters: dict[str, Any]
    run: Callable[[Session, Path, Timer, int], dict[str, Any]]


def _check(condition: bool, what: str) -> None:
    if not condition:
        raise BenchmarkFidelityError(f"{what} did not read back as written")


def _same_floats(actual: Any, expected: np.ndarray) -> bool:
    got = np.asarray(actual, dtype=np.float64)
    return got.shape == expected.shape and bool(
        np.array_equal(got.view(np.uint64), np.asarray(expected).view(np.uint64))
    )


def _round_trip(
    session: Session, scratch: Path, timer: Timer, repetition: int, batch: dict[str, Any]
) -> dict[str, Any]:
    path = scratch / f"round-{repetition}.db"
    workspace_path = scratch / f"workspace-{repetition}.db"
    with timer.phase("convert_to_fame"):
        converted = [famepy.refame(name, value, session=session) for name, value in batch.items()]
    with famepy.opendb(path, "create", session=session) as database:
        with timer.phase("write_raw"):
            for obj in converted:
                famepy.do_write(obj, database)
        with timer.phase("post"):
            famepy.postdb(database)
    with famepy.opendb(path, "readonly", session=session) as database:
        with timer.phase("read_raw"):
            objects = [
                famepy.do_read(famepy.quick_info(database, name), database) for name in batch
            ]
        with timer.phase("convert_from_fame"):
            converted_back = [famepy.unfame(obj, database=database) for obj in objects]
        with timer.phase("read_bridge"):
            bridged = [
                famepy.unfame(famepy.do_read(famepy.quick_info(database, name), database))
                for name in batch
            ]
    with timer.phase("write_workspace"):
        famepy.writefame(workspace_path, batch, mode="create")
    with timer.phase("read_workspace"):
        workspace = famepy.readfame(workspace_path)
    triples = zip(batch.items(), converted_back, bridged, strict=True)
    for (name, expected), back, bridged_back in triples:
        _check(_same_floats(back.values, expected.values), f"{name} (raw)")
        _check(_same_floats(bridged_back.values, expected.values), f"{name} (bridge)")
        _check(back.firstdate == expected.firstdate, f"{name} (first date)")
        _check(_same_floats(workspace[name].values, expected.values), f"{name} (ws)")
    observations = sum(len(value) for value in batch.values())
    return {
        "objects": len(batch),
        "observations": observations,
        "database_bytes": path.stat().st_size if path.exists() else None,
    }


def scenario_many_small(scale: dict[str, int]) -> Scenario:
    count, length = scale["many_small_count"], scale["many_small_length"]

    def run(session: Session, scratch: Path, timer: Timer, repetition: int) -> dict[str, Any]:
        return _round_trip(
            session, scratch, timer, repetition, _series_batch(count, length, _ts().Monthly(), 1)
        )

    return Scenario(
        "many_small", {"count": count, "length": length, **FIXTURE_INDEX["many_small"]}, run
    )


def scenario_few_large(scale: dict[str, int]) -> Scenario:
    count, length = scale["few_large_count"], scale["few_large_length"]

    def run(session: Session, scratch: Path, timer: Timer, repetition: int) -> dict[str, Any]:
        return _round_trip(
            session, scratch, timer, repetition, _series_batch(count, length, _ts().Daily(), 7)
        )

    return Scenario(
        "few_large", {"count": count, "length": length, **FIXTURE_INDEX["few_large"]}, run
    )


def scenario_dates(scale: dict[str, int]) -> Scenario:
    length = scale["dates_length"]

    def run(session: Session, scratch: Path, timer: Timer, repetition: int) -> dict[str, Any]:
        series = _dates_fixture(length)
        path = scratch / f"dates-{repetition}.db"
        with timer.phase("convert_to_fame"):
            obj = famepy.refame("dates", series, session=session)
        with famepy.opendb(path, "create", session=session) as database:
            with timer.phase("write_raw"):
                famepy.do_write(obj, database)
            with timer.phase("post"):
                famepy.postdb(database)
        with famepy.opendb(path, "readonly", session=session) as database:
            with timer.phase("read_raw"):
                back = famepy.do_read(famepy.quick_info(database, "dates"), database)
            with timer.phase("convert_from_fame"):
                dates = famepy.unfame(back, database=database)
        _check(tuple(dates.values) == tuple(series.values), "dates")
        _check(dates.firstdate == series.firstdate, "dates (first date)")
        return {"objects": 1, "observations": length, "database_bytes": path.stat().st_size}

    return Scenario("dates", {"length": length, "index": "monthly", "values": "daily"}, run)


def scenario_strings(scale: dict[str, int]) -> Scenario:
    length = scale["strings_length"]

    def run(session: Session, scratch: Path, timer: Timer, repetition: int) -> dict[str, Any]:
        series = _strings_fixture(length)
        path = scratch / f"strings-{repetition}.db"
        with timer.phase("convert_to_fame"):
            obj = famepy.refame("strings", series, session=session)
        with famepy.opendb(path, "create", session=session) as database:
            with timer.phase("write_raw"):
                famepy.do_write(obj, database)
            with timer.phase("post"):
                famepy.postdb(database)
        with famepy.opendb(path, "readonly", session=session) as database:
            with timer.phase("read_raw"):
                back = famepy.do_read(famepy.quick_info(database, "strings"), database)
            with timer.phase("convert_from_fame"):
                strings = famepy.unfame(back, database=database)
        _check(tuple(strings.values) == tuple(series.values), "strings")
        _check(strings.firstdate == series.firstdate, "strings (first date)")
        return {"objects": 1, "observations": length, "database_bytes": path.stat().st_size}

    return Scenario("strings", {"length": length, "index": "case"}, run)


def scenario_missing_density(scale: dict[str, int]) -> Scenario:
    length = scale["missing_length"]

    def run(session: Session, scratch: Path, timer: Timer, repetition: int) -> dict[str, Any]:
        fixture = _missing_fixture(length)
        path = scratch / f"missing-{repetition}.db"
        workspace_path = scratch / f"missing-workspace-{repetition}.db"
        with famepy.opendb(path, "create", session=session) as database:
            for name, series in fixture.items():
                with timer.phase(f"write_density_{name[1:]}"):
                    famepy.do_write(famepy.refame(name, series, database=database), database)
            famepy.postdb(database)
        back: dict[str, Any] = {}
        with famepy.opendb(path, "readonly", session=session) as database:
            for name in fixture:
                with timer.phase(f"read_density_{name[1:]}"):
                    back[name] = famepy.unfame(
                        famepy.do_read(famepy.quick_info(database, name), database),
                        database=database,
                    )
        with timer.phase("write_workspace"):
            famepy.writefame(workspace_path, fixture, mode="create")
        with timer.phase("read_workspace"):
            workspace = famepy.readfame(workspace_path)
        for name, series in fixture.items():
            _check(_same_floats(back[name].values, series.values), name)
            _check(_same_floats(workspace[name].values, series.values), f"{name} (ws)")
        return {
            "objects": len(MISSING_DENSITIES),
            "observations": length * len(MISSING_DENSITIES),
            "database_bytes": path.stat().st_size,
        }

    return Scenario(
        "missing_density",
        {
            "length": length,
            "densities": list(MISSING_DENSITIES),
            **FIXTURE_INDEX["missing_density"],
        },
        run,
    )


def scenario_migration(scale: dict[str, int]) -> Scenario:
    count, length = scale["many_small_count"], scale["many_small_length"]

    def run(session: Session, scratch: Path, timer: Timer, repetition: int) -> dict[str, Any]:
        from famepy import migration

        available, block = migration.dataecon_available()
        if not available:
            return {"blocked": f"DataEcon native extension unavailable: {block}"}
        batch = _series_batch(count, length, _ts().Monthly(), 23)
        source = scratch / f"migration-source-{repetition}.db"
        famepy.writefame(source, batch, mode="create")
        archive = scratch / f"archive-{repetition}.daec"
        with timer.phase("plan"):
            plan = migration.plan_migration(source, session=session)
        with timer.phase("migrate"):
            report = migration.migrate(source, archive, plan=plan, session=session)
        from tsecon.dataecon import open_dataecon

        back: dict[str, Any] = {}
        with timer.phase("read_back"), open_dataecon(archive) as db:
            for name in migration.list_migrated(db):
                back[name] = migration.read_migrated(db, name)
        _check(report.complete, "migration report")
        for name, series in batch.items():
            expected = migration.describe(migration.expected_object(name, series))
            _check(migration.describe(back[name]) == expected, name)
        return {
            "objects": len(batch),
            "observations": count * length,
            "complete": report.complete,
            "archive_bytes": archive.stat().st_size,
        }

    return Scenario("migration", {"count": count, "length": length}, run)


def build_scenarios(scale: dict[str, int], selected: list[str] | None) -> list[Scenario]:
    builders = {
        "many_small": scenario_many_small,
        "few_large": scenario_few_large,
        "dates": scenario_dates,
        "strings": scenario_strings,
        "missing_density": scenario_missing_density,
        "migration": scenario_migration,
    }
    names = list(selected or SCENARIOS)
    unknown = [name for name in names if name not in builders]
    if unknown:
        raise ValueError(f"Unknown scenarios: {', '.join(unknown)}")
    return [builders[name](scale) for name in names]


# -- sessions and identity ------------------------------------------------------


def build_session(options: dict[str, Any]) -> Session:
    factory = options.get("backend")
    if factory:
        module_name, _, attribute = factory.partition(":")
        native = getattr(importlib.import_module(module_name), attribute)()
        return Session(native=native)
    from famepy._discovery import discover

    return Session(discover(options.get("library"), root=options.get("root")))


def library_identity(options: dict[str, Any]) -> dict[str, Any]:
    if options.get("backend"):
        return {"backend": "injected", "vendor_timing": False}
    from famepy._discovery import discover

    candidate = discover(options.get("library"), root=options.get("root"))
    digest = hashlib.sha256()
    path = Path(candidate.path)
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return {
        "backend": "native",
        "vendor_timing": True,
        "library_bytes": path.stat().st_size,
        "library_sha256": digest.hexdigest(),
        "library_source": candidate.source,
    }


def _versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {"famepy": famepy.__version__}
    for name in ("TimeSeriesEconPy", "numpy"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


# -- the measurement of one scenario (worker side) -------------------------------


def measure(
    session: Session,
    scenario: Scenario,
    directory: Path,
    mode: str,
    repetitions: int,
    *,
    profile: bool = False,
) -> dict[str, Any]:
    """One scenario in this process: plain timed passes, then an instrumented pass.

    ``warm``: an untimed pass, ``repetitions`` timed passes without any
    instrumentation, then one instrumented pass (counting proxy,
    ``tracemalloc`` and optionally ``cProfile``) whose timings are not
    reported. ``cold``: a single timed pass, with ``initialize`` and the
    first database open timed by the worker before it. The proxy and the
    tracer are always removed again, whatever happens.
    """
    directory.mkdir(exist_ok=True)
    timer = Timer()
    if mode == "warm":
        sizes = scenario.run(session, directory, Timer(), 0)
        if sizes.get("blocked"):
            return {"scenario": scenario.name, "mode": mode, "blocked": sizes["blocked"]}
        for repetition in range(1, repetitions + 1):
            sizes = scenario.run(session, directory, timer, repetition)
    else:
        sizes = scenario.run(session, directory, timer, 0)
        if sizes.get("blocked"):
            return {"scenario": scenario.name, "mode": mode, "blocked": sizes["blocked"]}
    record: dict[str, Any] = {
        "scenario": scenario.name,
        "mode": mode,
        "parameters": scenario.parameters,
        "repetitions": repetitions if mode == "warm" else 1,
        "sizes": sizes,
        "verified": True,
        "fame_version": session.version(),
    }
    if mode == "cold":
        record["phases"] = {name: phase.to_json() for name, phase in timer.phases.items()}
        return record
    original = session._native
    counter = CountingNative(original)
    instrumented = Timer(counter)
    profiler = cProfile.Profile() if profile else None
    session._native = counter
    tracemalloc.start()
    try:
        if profiler is not None:
            profiler.enable()
        scenario.run(session, directory, instrumented, repetitions + 1)
    finally:
        if profiler is not None:
            profiler.disable()
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        session._native = original
    if profiler is not None:
        profiler.dump_stats(str(directory / "profile.prof"))
    for name, phase in instrumented.phases.items():
        timer.phases.setdefault(name, Phase()).native_calls = phase.native_calls
    record["phases"] = {name: phase.to_json() for name, phase in timer.phases.items()}
    record["memory"] = {"tracemalloc_peak_bytes": peak, "peak_rss_bytes": peak_rss_bytes()}
    record["profile_written"] = profiler is not None
    return record


def worker_main(config: dict[str, Any]) -> int:
    """The worker entry (``--worker``): configuration on stdin, result by file.

    Exit codes: 0 completed (the result may still be a block), 30 invalid
    configuration, 31 backend construction or initialization failed, 32 the
    scenario raised (its error class is in the result).
    """
    try:
        scale = SCALES[config["scale"]]
        mode = config["mode"]
        if mode not in MODES:
            raise ValueError
        scenario = build_scenarios(scale, [config["scenario"]])[0]
        scratch = Path(config["scratch"])
        repetitions = int(config.get("repetitions") or scale["repetitions"])
        if not 1 <= repetitions <= 100:
            raise ValueError
        scratch.mkdir(parents=True, exist_ok=False)
        redirect_streams(config)
    except (KeyError, ValueError, TypeError, OSError):
        return 30
    payload: dict[str, Any] = {"scenario": scenario.name, "mode": mode}
    started = time.perf_counter()
    try:
        session = build_session(config)
        session.initialize()
    except Exception as error:  # noqa: BLE001 - reported by bounded record only
        write_result(config, failure_record(error, scenario.name, mode))
        return 31
    startup = time.perf_counter() - started
    code = 0
    try:
        if mode == "cold":
            started = time.perf_counter()
            with famepy.opendb(scratch / "first-open.db", "create", session=session) as db:
                famepy.postdb(db)
            first_open = time.perf_counter() - started
        payload = measure(
            session, scenario, scratch, mode, repetitions, profile=bool(config.get("profile"))
        )
        if mode == "cold" and "blocked" not in payload:
            payload["startup_seconds"] = round(startup, 6)
            payload["first_open_seconds"] = round(first_open, 6)
    except Exception as error:  # noqa: BLE001
        payload = failure_record(error, scenario.name, mode)
        code = 32
    finally:
        try:
            session.finalize()
        except Exception:  # noqa: BLE001 - the measurement is already decided
            pass
    write_result(config, payload)
    return code


# -- the launch and validation of one measurement (coordinator side) ------------


_SCENARIO_PHASES = {
    "many_small": frozenset(
        (
            "convert_to_fame",
            "write_raw",
            "post",
            "read_raw",
            "convert_from_fame",
            "read_bridge",
            "write_workspace",
            "read_workspace",
        )
    ),
    "few_large": frozenset(
        (
            "convert_to_fame",
            "write_raw",
            "post",
            "read_raw",
            "convert_from_fame",
            "read_bridge",
            "write_workspace",
            "read_workspace",
        )
    ),
    "dates": frozenset(("convert_to_fame", "write_raw", "post", "read_raw", "convert_from_fame")),
    "strings": frozenset(("convert_to_fame", "write_raw", "post", "read_raw", "convert_from_fame")),
    "missing_density": frozenset(
        (
            "write_workspace",
            "read_workspace",
            *(
                f"{action}_density_{int(density * 100):02d}"
                for action in ("write", "read")
                for density in MISSING_DENSITIES
            ),
        )
    ),
    "migration": frozenset(("plan", "migrate", "read_back")),
}
# Every phase label a scenario can report; a failure record names one of these.
PHASE_LABELS: frozenset[str] = frozenset().union(*_SCENARIO_PHASES.values())


def _number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _numbers(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_number(v) for v in value)


def _optional_int(value: Any) -> bool:
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63
    )


def _valid_phases(phases: Any, samples: int, scenario: str) -> bool:
    if not isinstance(phases, dict) or frozenset(phases) != _SCENARIO_PHASES.get(scenario):
        return False
    for record in phases.values():
        if not isinstance(record, dict) or set(record) != {"seconds", "native_calls"}:
            return False
        seconds = record["seconds"]
        if not isinstance(seconds, dict) or set(seconds) != {"samples", "min", "median", "max"}:
            return False
        if not _numbers(seconds["samples"]) or len(seconds["samples"]) != samples:
            return False
        if not all(_number(seconds[key]) for key in ("min", "median", "max")):
            return False
        values = seconds["samples"]
        if (
            seconds["min"] != min(values)
            or seconds["max"] != max(values)
            or abs(seconds["median"] - statistics.median(values)) > 1e-6
        ):
            return False
        if not _optional_int(record["native_calls"]):
            return False
    return True


def _valid_sizes(sizes: Any) -> bool:
    allowed = {"objects", "observations", "database_bytes", "archive_bytes", "complete"}
    if not isinstance(sizes, dict) or not {"objects", "observations"} <= set(sizes) <= allowed:
        return False
    return all(
        value is True if key == "complete" else _optional_int(value) for key, value in sizes.items()
    )


def validate_measurement(
    payload: Any, scenario: Scenario, mode: str, *, expected_repetitions: int | None = None
) -> str | None:
    """The reason a worker result is unusable, or None when it is exactly as expected."""
    if not isinstance(payload, dict):
        return "invalid_result"
    if payload.get("scenario") != scenario.name or payload.get("mode") != mode:
        return "wrong_scenario"
    fixed = {"scenario", "mode", "token", "complete"}
    if "blocked" in payload:
        if set(payload) != fixed | {"blocked"}:
            return "invalid_result"
        block = payload["blocked"]
        return None if isinstance(block, str) and _BLOCK.fullmatch(block) else "invalid_result"
    expected = fixed | {
        "parameters",
        "repetitions",
        "sizes",
        "verified",
        "phases",
        "fame_version",
    }
    if mode == "warm":
        expected |= {"memory", "profile_written"}
    else:
        expected |= {"startup_seconds", "first_open_seconds"}
    if set(payload) != expected:
        return "invalid_result"
    if payload["parameters"] != scenario.parameters or payload["verified"] is not True:
        return "invalid_result"
    if not _number(payload["fame_version"]) or payload["fame_version"] == 0:
        return "invalid_result"
    repetitions = payload["repetitions"]
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
        return "invalid_result"
    if expected_repetitions is not None and repetitions != expected_repetitions:
        return "invalid_result"
    if not _valid_phases(payload["phases"], repetitions, scenario.name) or not _valid_sizes(
        payload["sizes"]
    ):
        return "invalid_result"
    if mode == "warm":
        memory = payload["memory"]
        if not isinstance(memory, dict) or set(memory) != {
            "tracemalloc_peak_bytes",
            "peak_rss_bytes",
        }:
            return "invalid_result"
        if not all(_optional_int(value) for value in memory.values()):
            return "invalid_result"
        if not isinstance(payload["profile_written"], bool):
            return "invalid_result"
    elif not (_number(payload["startup_seconds"]) and _number(payload["first_open_seconds"])):
        return "invalid_result"
    return None


MEMORY_SCOPE = (
    "tracemalloc counts Python and NumPy allocations during one instrumented pass; "
    "peak RSS is the whole worker process since start, including native memory"
)
INSTRUMENTATION_NOTE = (
    "timed samples ran without instrumentation; native call counts, memory and the "
    "optional profile come from one separate instrumented pass whose time is not reported"
)


FAILURE_FIELDS = frozenset(
    {"scenario", "mode", "token", "complete", "error_type", "status", "operation", "phase"}
)


def failure_diagnostics(payload: Any, scenario: Scenario, mode: str) -> dict[str, Any]:
    """The accepted diagnostics of a worker failure record, field by field.

    A record with any field outside the fixed set, or a value outside its
    bound or allowlist, contributes no diagnostics (``diagnostics:
    rejected``); a failure never carries text, paths or timing data.
    """
    if not isinstance(payload, dict) or set(payload) - FAILURE_FIELDS:
        return {"diagnostics": "rejected"}
    if payload.get("scenario") != scenario.name or payload.get("mode") != mode:
        return {"diagnostics": "rejected"}
    accepted: dict[str, Any] = {}
    kind = payload.get("error_type")
    if kind is not None:
        if not isinstance(kind, str) or not _ERROR_TYPE.fullmatch(kind):
            return {"diagnostics": "rejected"}
        accepted["error_type"] = kind
    status = payload.get("status")
    if status is not None:
        if error_number(status) is None:
            return {"diagnostics": "rejected"}
        accepted["status"] = status
    operation = payload.get("operation")
    if operation is not None:
        if not isinstance(operation, str) or operation not in OPERATIONS:
            return {"diagnostics": "rejected"}
        accepted["operation"] = operation
    phase = payload.get("phase")
    if phase is not None:
        if not isinstance(phase, str) or phase not in _SCENARIO_PHASES[scenario.name]:
            return {"diagnostics": "rejected"}
        accepted["phase"] = phase
    return accepted


def _classify(result: WorkerResult, scenario: Scenario, mode: str) -> dict[str, Any] | None:
    """A failure record for a worker that did not complete, or None."""
    if result.returncode == 30:
        return {"error": "invalid_configuration"}
    if result.returncode in (31, 32):
        error = "backend_setup_failed" if result.returncode == 31 else "scenario_failed"
        return {"error": error, **failure_diagnostics(result.payload, scenario, mode)}
    if result.returncode != 0:
        return {"error": "worker_failed", "exit_code": result.returncode}
    if result.payload is None:
        return {"error": result.result_kind or "invalid_result"}
    return None


def run_measurement(
    scenario: Scenario, mode: str, options: dict[str, Any], scratch: Path, timeout: float
) -> dict[str, Any]:
    """Launch one worker for ``scenario`` in ``mode`` and validate its result file."""
    directory = scratch / f"{mode}-{scenario.name}"
    directory.mkdir(exist_ok=True)
    command = [sys.executable, "-m", "famepy.benchmarks", "--worker"]
    config = {
        "scratch": str(directory / "work"),
        "library": options.get("library"),
        "root": options.get("root"),
        "backend": options.get("backend"),
        "scale": options["scale"],
        "scenario": scenario.name,
        "mode": mode,
        "repetitions": options.get("repetitions"),
        "profile": bool(options.get("profile")) and mode == "warm",
    }
    tokens = reserve_result(directory, mode)
    started = time.monotonic()
    try:
        result = launch_worker(command, config, timeout, tokens=tokens)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "duration_seconds": round(time.monotonic() - started, 3)}
    except OSError as error:
        return {"error": "start_failed", "errno": error.errno}
    failure = _classify(result, scenario, mode)
    if failure is not None:
        failure["stray_output_bytes"] = result.log_bytes + result.pipe_bytes
        return failure
    expected_repetitions = (
        1
        if mode == "cold"
        else int(options.get("repetitions") or SCALES[options["scale"]]["repetitions"])
    )
    reason = validate_measurement(
        result.payload, scenario, mode, expected_repetitions=expected_repetitions
    )
    if reason is not None:
        return {"error": reason}
    assert result.payload is not None
    record = {
        key: value for key, value in result.payload.items() if key not in ("token", "complete")
    }
    record["stray_output_bytes"] = result.log_bytes + result.pipe_bytes
    if "memory" in record:
        record["memory"]["scope"] = MEMORY_SCOPE
    return record


def comparison_report(
    report: dict[str, Any], scenarios: list[Scenario], julia: dict[str, Any] | None
) -> dict[str, Any]:
    """Candidate workload pairs versus completed, verified Python/Julia pairs.

    A candidate pair is a scenario/phase the two workloads define alike. It
    becomes a verified pair only when the Python warm measurement of that
    scenario was accepted, the Julia run was accepted with its own read-back
    verification, and the fixture digests and index domains agree for the
    scenario. Nothing failed, absent or unverified is listed as comparable,
    and no ratio is computed.
    """
    requested = {s.name for s in scenarios}
    candidates = [
        [name, phase]
        for name in COMPARABLE_SCENARIOS
        for phase in COMPARABLE_PHASES
        if name in requested
    ]
    warm = report.get("warm", {})
    python_accepted = {
        name: name in warm and "error" not in warm[name] and "blocked" not in warm[name]
        for name in COMPARABLE_SCENARIOS
    }
    comparison: dict[str, Any] = {
        "candidate_pairs": candidates,
        "python_accepted": {n: python_accepted[n] for n in COMPARABLE_SCENARIOS if n in requested},
        "non_comparable": {
            s.name: NON_COMPARABLE[s.name] for s in scenarios if s.name in NON_COMPARABLE
        },
        "julia": "absent",
        "verified_pairs": [],
        "note": "verified pairs list Python and reference timings side by side only where "
        "both measurements completed and were verified on the same fixtures; no ratio is "
        "computed; the Python samples ran without instrumentation and the reference "
        "script has none",
    }
    if julia is None:
        return comparison
    if "error" in julia:
        comparison["julia"] = "failed"
        return comparison
    comparison["julia"] = "verified"
    comparison["julia_qualification"] = (
        "pinned FAME.jl tree" if julia.get("fame_tree_pinned") else "unpinned FAME.jl tree"
    )
    hashes = {
        name: julia["fixture_hashes"].get(name) == report["fixture_hashes"][name]
        for name in COMPARABLE_SCENARIOS
    }
    domains = {
        name: julia["fixture_domains"].get(name) == report["fixture_domains"][name]
        for name in COMPARABLE_SCENARIOS
    }
    comparison["julia_fixtures_match"] = hashes
    comparison["julia_domains_match"] = domains
    comparison["verified_pairs"] = [
        pair
        for pair in candidates
        if python_accepted[pair[0]] and hashes[pair[0]] and domains[pair[0]]
    ]
    return comparison


def run(options: dict[str, Any]) -> dict[str, Any]:
    if options.get("julia_selfcheck") and not options.get("julia"):
        raise ValueError("julia_selfcheck requires a Julia executable and project")
    scale_name = options.get("scale", "small")
    if scale_name not in SCALES:
        raise ValueError("scale must be 'small' or 'standard'")
    scale = SCALES[scale_name]
    requested = options.get("repetitions")
    if requested is None:
        requested = scale["repetitions"]
    try:
        if isinstance(requested, bool):
            raise TypeError
        repetitions = operator.index(requested)
    except TypeError:
        raise ValueError("repetitions must be an integer between 1 and 100") from None
    if not 1 <= repetitions <= 100:
        raise ValueError("repetitions must be between 1 and 100")
    requested_timeout = options.get("timeout", 600.0)
    timeout = float(requested_timeout)
    if isinstance(requested_timeout, bool) or not 0 < timeout <= 3600:
        raise ValueError("timeout must be positive and at most 3600 seconds")
    scenarios = build_scenarios(scale, options.get("scenarios"))
    scratch = Path(options["scratch"])
    if scratch.exists() and any(scratch.iterdir()):
        raise ValueError("scratch must be a new or empty directory")
    scratch.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "environment": {
            "platform": sys.platform,
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            **_versions(),
        },
        "scale": scale_name,
        "repetitions": repetitions,
        "note": "timings are inputs for review, not a speed claim; injected backends "
        "measure the package's own overhead only",
        "instrumentation": INSTRUMENTATION_NOTE,
        "cold_semantics": "a fresh interpreter process per scenario (startup and first open "
        "timed apart); the file-system cache is not cleared",
    }
    if not options.get("native") and not options.get("backend"):
        report["blocked"] = "native benchmarks require the explicit --native opt-in"
        return report
    from famepy.validation import package_identity

    report["identity"] = package_identity(options.get("wheel"), options.get("source_sha"))
    # No native call runs in this process: the library identity is a file
    # digest and the runtime version comes from the workers' results.
    report["library"] = library_identity(options)
    report["library"]["fame_version"] = None
    launch = {
        key: options.get(key) for key in ("library", "root", "backend", "repetitions", "profile")
    }
    launch["scale"] = scale_name
    failures: list[dict[str, Any]] = []
    modes = ["warm", "cold"] if options.get("cold", True) else ["warm"]
    for mode in modes:
        report[mode] = {}
        for scenario in scenarios:
            record = run_measurement(scenario, mode, launch, scratch, timeout)
            version = record.pop("fame_version", None)
            if version is not None and report["library"]["fame_version"] is None:
                report["library"]["fame_version"] = version
            report[mode][scenario.name] = record
            if "error" in record or "blocked" in record:
                failures.append(
                    {"measurement": f"{mode}:{scenario.name}", **{k: record[k] for k in record}}
                )
    report["fixture_hashes"] = fixture_hashes(scale)
    report["fixture_domains"] = fixture_domains(scale)
    julia: dict[str, Any] | None = None
    if options.get("julia"):
        from ._julia import run_julia_benchmark, run_julia_selfcheck

        julia = run_julia_benchmark(options["julia"], scratch, scale, repetitions, timeout)
        report["julia"] = julia
        if "error" in julia:
            failures.append({"measurement": "julia", "error": julia["error"]})
        if options.get("julia_selfcheck"):
            selfcheck = run_julia_selfcheck(options["julia"], scratch, timeout)
            report["julia_selfcheck"] = selfcheck
            failures.extend(
                {"measurement": f"julia_selfcheck:{case}", "error": "negative_case_not_detected"}
                for case, outcome in selfcheck.items()
                if outcome["outcome"] != "pass"
            )
    report["comparison"] = comparison_report(report, scenarios, julia)
    if julia is not None and "error" not in julia:
        for key in ("julia_fixtures_match", "julia_domains_match"):
            if not all(report["comparison"][key].values()):
                failures.append({"measurement": "julia", "error": key.replace("julia_", "")})
    report["failures"] = failures
    report["result"] = "complete" if not failures else "incomplete"
    return report


__all__ = [
    "COMPARABLE_PHASES",
    "COMPARABLE_SCENARIOS",
    "FAILURE_FIELDS",
    "FIXTURE_INDEX",
    "LAST_CALENDAR_YEAR",
    "OPERATIONS",
    "PHASE_LABELS",
    "INSTRUMENTATION_NOTE",
    "MEMORY_SCOPE",
    "MISSING_DENSITIES",
    "MODES",
    "NON_COMPARABLE",
    "SCALES",
    "SCENARIOS",
    "SCHEMA_VERSION",
    "BenchmarkFidelityError",
    "CountingNative",
    "Scenario",
    "Timer",
    "build_scenarios",
    "comparison_report",
    "failure_diagnostics",
    "failure_record",
    "fixture_domains",
    "fixture_end_year",
    "fixture_hashes",
    "fnv1a64",
    "lcg_values",
    "library_identity",
    "measure",
    "peak_rss_bytes",
    "run",
    "run_measurement",
    "validate_measurement",
    "with_missing",
    "worker_main",
]
