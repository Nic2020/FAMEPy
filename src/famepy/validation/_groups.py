# SPDX-License-Identifier: MIT
"""Validation groups. Each runs in its own child process with its own runtime.

Every group owns the synthetic databases and files it creates under the scratch
directory. Cross-process persistence checks spawn a verification child that
reopens a database read-only and compares metadata and values against a
manifest the group wrote. Calendar indices are derived from the library's own
year/period conversion, never from a fixed synthetic constant.

``REQUIRED_CASES`` lists, per group, the case identifiers that must be present
with status ``pass`` for the group to pass; the parent enforces it.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

import famepy
from famepy import bridge
from famepy._constants import FREQUENCY_MONTHLY, NAME_CAPACITY
from famepy._data import classify_by_sentinel, sentinel_value
from famepy._runtime import Session

from ._process import run_child
from ._report import Case, Recorder, encode_value

GROUPS = ("lifecycle", "database", "raw_matrix", "discovery", "commands", "bridge")
DEPENDENT_GROUPS = ("database", "raw_matrix", "discovery", "commands", "bridge")

_DTYPES = {"precision": np.float64, "numeric": np.float32, "boolean": np.int32, "date": np.int64}


class Context:
    def __init__(
        self,
        session: Session,
        scratch: Path,
        recorder: Recorder,
        child_command: Callable[[list[str]], list[str]],
        timeout: float,
        julia: dict[str, str] | None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self.scratch = scratch
        self.recorder = recorder
        self.child_command = child_command
        self.timeout = timeout
        self.julia = julia
        self.config = dict(config or {})
        self.new_session: Callable[[], Session] = lambda: Session(native=session._native)
        self._first: int | None = None

    def path(self, name: str) -> Path:
        return self.scratch / name

    def monthly_index(self, year: int, period: int) -> int:
        """A monthly index from the library's calendar (not a synthetic constant)."""
        with self.session.operation("year_period_to_index") as native:
            return native.year_period_to_index(FREQUENCY_MONTHLY, year, period)

    @property
    def first(self) -> int:
        if self._first is None:
            self._first = self.monthly_index(2020, 1)
        return self._first

    def verify_in_new_process(self, case_id: str, manifest: dict[str, Any]) -> None:
        """Spawn a verification child that reopens the database read-only."""
        manifest_path = self.path(f"{case_id}.manifest.json")
        manifest_path.write_text(json.dumps(manifest), encoding="ascii")
        self._run_nested(case_id, ["--group", "verify", "--manifest", str(manifest_path)])

    def fresh_process(self, case_id: str) -> None:
        """Spawn a child that runs the one-shot lifecycle in a fresh process.

        This is the supported fresh-runtime boundary; it is exercised after
        the current child has finalized, proving that a new process (not a
        restart) initializes again.
        """
        self._run_nested(case_id, ["--group", "fresh_process"])

    def _run_nested(self, case_id: str, arguments: list[str]) -> None:
        command = self.child_command(arguments)
        try:
            result = run_child(command, json.dumps(self.config), self.timeout, nested=True)
        except subprocess.TimeoutExpired:
            self.recorder.add(Case(case_id, "fail", note="verification child timeout"))
            return
        except OSError as error:
            self.recorder.add(
                Case(case_id, "fail", note="verification child start failed", errno=error.errno)
            )
            return
        if result.returncode != 0:
            self.recorder.add(
                Case(case_id, "fail", note="verification child failed", errno=result.returncode)
            )
            return
        try:
            payload = json.loads(result.stdout)
            cases = payload["cases"]
            if not isinstance(cases, list) or not cases:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.recorder.add(Case(case_id, "fail", note="verification output invalid"))
            return
        for case in cases:
            if not isinstance(case, dict):
                self.recorder.add(Case(case_id, "fail", note="verification output invalid"))
                continue
            self.recorder.add(
                Case(
                    f"{case_id}:{case.get('id')}",
                    case.get("status", "fail"),
                    error_type=case.get("error_type"),
                    status_code=case.get("status_code"),
                    expected=case.get("expected"),
                    actual=case.get("actual"),
                    note=case.get("note"),
                )
            )


# -- manifest helpers --------------------------------------------------------


def manifest_object(
    name: str,
    kind: str,
    values: Any,
    *,
    class_name: str,
    type_code: int,
    frequency: int | None = None,
    first_index: int | None = None,
) -> dict[str, Any]:
    """Describe one object completely: class, type, frequency, range and bits."""
    entry: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "class": class_name,
        "type_code": type_code,
        "frequency": frequency,
        "values": _manifest_value(kind, values),
    }
    if class_name == "series":
        count = len(values)
        entry["first_index"] = first_index if count else None
        entry["last_index"] = first_index + count - 1 if count and first_index is not None else None
    return entry


def _manifest_value(kind: str, value: Any) -> Any:
    """Encode values losslessly: floats by bit pattern so NaN payloads survive."""
    if kind in ("precision", "numeric"):
        return [np.array(v, dtype=_DTYPES[kind]).tobytes().hex() for v in value]
    if kind in ("boolean", "date"):
        return [int(v) for v in value]
    return [bytes(v).decode("ascii") for v in value]


def _decode_manifest(kind: str, value: list[Any]) -> Any:
    if kind in ("precision", "numeric"):
        dtype = _DTYPES[kind]
        return np.array(
            [np.frombuffer(bytes.fromhex(v), dtype=dtype)[0] for v in value], dtype=dtype
        )
    if kind == "boolean":
        return np.array(value, dtype=np.int32)
    if kind == "date":
        return np.array(value, dtype=np.int64)
    return [v.encode("ascii") for v in value]


def verify_case_ids(case_id: str, names: list[str]) -> tuple[str, ...]:
    """The verification cases a cross-process check must report."""
    ids: list[str] = []
    for name in names:
        ids.extend(
            (f"{case_id}:reopen:{name}", f"{case_id}:meta:{name}", f"{case_id}:values:{name}")
        )
    return tuple(ids)


def run_verify(session: Session, manifest: dict[str, Any], recorder: Recorder) -> None:
    """Child-side manifest verification against a read-only reopen.

    Metadata (class, type, frequency, range) is compared before any value is
    read, then values are compared with exact dtype and bit patterns.
    """
    index_nc = session.sentinels.index_nc
    with famepy.open_database(manifest["database"], "readonly", session=session) as database:
        for entry in manifest["objects"]:
            name, kind = entry["name"], entry["kind"]
            expected = _decode_manifest(kind, entry["values"])

            def read(name: str = name) -> Any:
                return famepy.quick_info(database, name), _read(database, name)

            result = recorder.check(f"reopen:{name}", read)
            if result is None:
                continue
            info, raw = result
            actual_meta: list[Any] = ["scalar" if info.is_scalar else "series", info.type_code]
            expected_meta: list[Any] = [entry["class"], entry["type_code"]]
            if entry["class"] == "series":
                expected_meta.append(entry["frequency"])
                if entry["first_index"] is None:
                    expected_meta.append("empty")
                else:
                    expected_meta.extend([entry["first_index"], entry["last_index"]])
            if info.is_series:
                actual_meta.append(info.frequency)
                if info.is_empty(index_nc):
                    actual_meta.append("empty")
                else:
                    actual_meta.extend([info.first_index, info.last_index])
            recorder.equal(f"meta:{name}", actual_meta, expected_meta)
            if isinstance(raw, famepy.RawSeries):
                actual: Any = raw.values
            elif kind in ("string", "namelist"):
                actual = [raw.value]
            else:
                actual = np.array([raw.value])
            recorder.equal(f"values:{name}", actual, expected)


# -- groups ----------------------------------------------------------------


LIFECYCLE_REQUIRED = (
    "initialize",
    "idempotent_initialize",
    "version",
    "version_is_positive",
    "sentinels_read",
    "sentinel_facts",
    "reset_unsupported_while_active",
    "still_initialized_after_reset_refusal",
    "finalize",
    "finalized_state",
    "finalize_again_harmless",
    "reinitialize_rejected",
    "new_wrapper_rejected",
    "new_wrapper_untouched",
    "reset_unsupported_after_finalize",
    "operation_after_finalize_rejected",
    "state_stays_finalized",
    "fresh_process:initialize",
    "fresh_process:version_is_positive",
    "fresh_process:finalize",
    "fresh_process:finalized_state",
)


def group_lifecycle(ctx: Context) -> None:
    session, r = ctx.session, ctx.recorder
    r.check("initialize", session.initialize)
    r.equal("idempotent_initialize", session.initialize() is session, True)
    version = r.check("version", session.version)
    r.equal("version_is_positive", version is not None and float(version) > 0, True)
    facts = r.check("sentinels_read", lambda: _sentinel_facts(session))
    if facts is not None:
        lengths = facts.pop("string_lengths")
        all_nan = facts.pop("precision_all_nan")
        r.equal(
            "sentinel_facts",
            facts,
            {
                "precision_distinct": True,
                "numeric_distinct": True,
                "boolean_distinct": True,
                "index_distinct": True,
            },
        )
        r.fact("string_sentinel_lengths", lengths)
        r.fact("precision_sentinels_are_nan", all_nan)
    # reset() is unsupported and must not touch an active runtime.
    r.expect_error(
        "reset_unsupported_while_active", session.reset, (famepy.UnsupportedOperationError,)
    )
    r.equal(
        "still_initialized_after_reset_refusal",
        [session.state, session.version() == version],
        ["initialized", True],
    )
    # Finalization is terminal for this process: everything below must be
    # rejected in Python before any native call.
    r.check("finalize", session.finalize)
    r.equal("finalized_state", session.state, "finalized")
    r.check("finalize_again_harmless", session.finalize)
    r.expect_error("reinitialize_rejected", session.initialize, (famepy.RuntimeStateError,))
    other = ctx.new_session()
    before = [other.state, other.is_loaded, other.is_initialized, other.generation]
    r.expect_error("new_wrapper_rejected", other.initialize, (famepy.RuntimeStateError,))
    r.equal(
        "new_wrapper_untouched",
        [other.state, other.is_loaded, other.is_initialized, other.generation],
        before,
    )
    r.expect_error(
        "reset_unsupported_after_finalize", session.reset, (famepy.UnsupportedOperationError,)
    )
    r.expect_error(
        "operation_after_finalize_rejected", session.version, (famepy.RuntimeStateError,)
    )
    r.equal("state_stays_finalized", [session.state, session.generation], ["finalized", 1])
    ctx.fresh_process("fresh_process")


def group_fresh_process(ctx: Context) -> None:
    """Minimal lifecycle in a child spawned after its parent finalized."""
    session, r = ctx.session, ctx.recorder
    r.check("initialize", session.initialize)
    version = r.check("version", session.version)
    r.equal("version_is_positive", version is not None and float(version) > 0, True)
    r.check("finalize", session.finalize)
    r.equal("finalized_state", session.state, "finalized")


def _sentinel_facts(session: Session) -> dict[str, Any]:
    s = session.sentinels
    return {
        "precision_distinct": len(
            {np.float64(v).tobytes() for v in (s.precision_nc, s.precision_na, s.precision_nd)}
        )
        == 3,
        "precision_all_nan": all(
            math.isnan(v) for v in (s.precision_nc, s.precision_na, s.precision_nd)
        ),
        "numeric_distinct": len(
            {np.float32(v).tobytes() for v in (s.numeric_nc, s.numeric_na, s.numeric_nd)}
        )
        == 3,
        "boolean_distinct": len({s.boolean_nc, s.boolean_na, s.boolean_nd}) == 3,
        "index_distinct": len({s.index_nc, s.index_na, s.index_nd}) == 3,
        "string_lengths": [len(s.string_nc), len(s.string_na), len(s.string_nd)],
    }


DATABASE_REQUIRED = (
    "create_write_post_close",
    "reopen_readonly",
    "reopen_readonly_value",
    *verify_case_ids("cross_process_scalar", ["kept"]),
    "close_without_post",
    "readonly_missing_file",
    "create_existing_file",
    "mode_readonly",
    "mode_update",
    "mode_shared",
    "mode_write",
    "mode_direct_write",
    "mode_overwrite",
    "mode_overwrite_empties",
    "work_database_flow",
    "work_database",
    "stale_handle_after_finalize",
    "stale_close_harmless",
    "finalize",
)


def group_database(ctx: Context) -> None:
    session, r = ctx.session, ctx.recorder
    session.initialize()
    path = ctx.path("lifecycle.db")

    def create_and_post() -> None:
        with famepy.open_database(path, "create", session=session) as database:
            famepy.write_object(database, "kept", famepy.scalar("precision", 1.5))
            database.post()

    r.check("create_write_post_close", create_and_post)

    def reopen_readonly() -> Any:
        with famepy.open_database(path, session=session) as database:
            return np.array([_read(database, "kept").value])

    r.equal("reopen_readonly_value", r.check("reopen_readonly", reopen_readonly), np.array([1.5]))
    ctx.verify_in_new_process(
        "cross_process_scalar",
        {
            "database": str(path),
            "objects": [
                manifest_object(
                    "kept",
                    "precision",
                    [1.5],
                    class_name="scalar",
                    type_code=int(famepy.ObjectType.PRECISION),
                )
            ],
        },
    )

    def unposted_close() -> list[str]:
        with famepy.open_database(path, "update", session=session) as database:
            famepy.write_object(database, "unposted", famepy.scalar("precision", 2.0))
        with famepy.open_database(path, session=session) as database:
            return [info.name_text for info in famepy.list_objects(database)]

    names = r.check("close_without_post", unposted_close)
    if names is not None:
        r.fact(
            "observed_unposted_write_discarded",
            "UNPOSTED" not in names,
            note="observation only; not a promised semantic",
        )
    r.expect_error(
        "readonly_missing_file",
        lambda: famepy.open_database(ctx.path("absent.db"), session=session),
        (famepy.FameError,),
    )
    r.expect_error(
        "create_existing_file",
        lambda: famepy.open_database(path, "create", session=session),
        (famepy.FameError,),
    )
    for mode in ("readonly", "update", "shared", "write", "direct_write"):

        def open_close(mode: str = mode) -> None:
            with famepy.open_database(path, mode, session=session) as database:
                if database.mode.name.lower() != mode:
                    raise AssertionError("mode mismatch")

        r.check(f"mode_{mode}", open_close)
    overwrite = ctx.path("overwrite.db")

    def overwrite_flow() -> int:
        with famepy.open_database(overwrite, "create", session=session) as database:
            famepy.write_object(database, "a", famepy.scalar("precision", 1.0))
            database.post()
        with famepy.open_database(overwrite, "overwrite", session=session) as database:
            return len(famepy.list_objects(database))

    r.equal("mode_overwrite_empties", r.check("mode_overwrite", overwrite_flow), 0)

    def workdb_flow() -> Any:
        work = famepy.work_database(session=session)
        if famepy.work_database(session=session) is not work:
            raise AssertionError("work database is not a singleton")
        famepy.write_object(work, "w", famepy.scalar("precision", 3.0), replace=True)
        value = np.array([_read(work, "w").value])
        work.close()
        return value

    r.equal("work_database", r.check("work_database_flow", workdb_flow), np.array([3.0]))

    # Terminal: finalization invalidates handles; this is the last native step.
    stale = famepy.open_database(path, session=session)
    r.check("finalize", session.finalize)
    r.expect_error(
        "stale_handle_after_finalize",
        lambda: famepy.quick_info(stale, "kept"),
        (famepy.StaleHandleError,),
    )
    r.check("stale_close_harmless", stale.close)
    r.equal("stale_handle_closed", stale.is_open, False)


# name, kind, series frequency name, date frequency name
SERIES_CASES: tuple[tuple[str, str, str, str | None], ...] = (
    ("p_series", "precision", "monthly", None),
    ("n_series", "numeric", "monthly", None),
    ("b_series", "boolean", "monthly", None),
    ("d_series", "date", "monthly", "monthly"),
    ("s_series", "string", "case", None),
    ("s_missing_series", "string", "case", None),
    ("empty", "precision", "monthly", None),
    ("s_empty", "string", "case", None),
)
SCALAR_CASES: tuple[tuple[str, str], ...] = (
    ("p_scalar", "precision"),
    ("p_missing", "precision"),
    ("n_scalar", "numeric"),
    ("n_missing", "numeric"),
    ("b_scalar", "boolean"),
    ("b_missing", "boolean"),
    ("d_scalar", "date"),
    ("d_missing", "date"),
    ("s_scalar", "string"),
    ("s_missing", "string"),
    ("nl_scalar", "namelist"),
    ("nl_empty", "namelist"),
)


def _series_values(name: str, s: famepy.Sentinels, first: int) -> Any:
    return {
        "p_series": np.array([1.0, s.precision_nc, s.precision_na, s.precision_nd, -2.5]),
        "n_series": np.array([1.5, s.numeric_nc, s.numeric_na, s.numeric_nd], dtype=np.float32),
        "b_series": np.array([1, 0, s.boolean_nc, s.boolean_na, s.boolean_nd], dtype=np.int32),
        "d_series": np.array([first + 1, s.index_nc, s.index_na, s.index_nd], dtype=np.int64),
        "s_series": [b"alpha", b"", b"x" * 40],
        "s_missing_series": [s.string_nc, s.string_na, s.string_nd, b"normal"],
        "empty": np.empty(0, dtype=np.float64),
        "s_empty": [],
    }[name]


def _scalar_value(name: str, s: famepy.Sentinels, first: int) -> Any:
    return {
        "p_scalar": 2.5,
        "p_missing": s.precision_nd,
        "n_scalar": np.float32(0.5),
        "n_missing": s.numeric_na,
        "b_scalar": 1,
        "b_missing": s.boolean_nc,
        "d_scalar": first + 3,
        "d_missing": s.index_na,
        "s_scalar": b"hello world",
        "s_missing": s.string_nc,
        "nl_scalar": b"{A,B,C}",
        "nl_empty": b"{}",
    }[name]


def _matrix_required() -> tuple[str, ...]:
    ids: list[str] = ["write_matrix", "read_matrix"]
    for name, _kind, _frequency, _date in SERIES_CASES:
        ids.extend((f"read:{name}", f"kind:{name}"))
        if name not in ("empty", "s_empty"):
            ids.append(f"classifier_agreement:{name}")
    for name, kind in SCALAR_CASES:
        ids.append(f"read:{name}")
        if kind != "namelist":
            ids.append(f"classifier_agreement:{name}")
    ids.extend(
        (
            "empty_series_is_empty",
            "empty_quick_info",
            "subrange_read",
            "subrange_first_index",
            "period_round_trip",
            *verify_case_ids(
                "cross_process_matrix",
                [name for name, *_ in SERIES_CASES] + [name for name, _ in SCALAR_CASES],
            ),
            "replace_and_delete",
            "replace_value",
            "finalize",
        )
    )
    return tuple(ids)


def group_raw_matrix(ctx: Context) -> None:
    session, r = ctx.session, ctx.recorder
    session.initialize()
    s = session.sentinels
    first = ctx.first
    path = ctx.path("matrix.db")
    manifest_objects: list[dict[str, Any]] = []

    def write_all() -> None:
        with famepy.open_database(path, "create", session=session) as database:
            for name, kind, frequency, date_frequency in SERIES_CASES:
                values = _series_values(name, s, first)
                start = 1 if frequency == "case" else first
                raw = famepy.series(kind, frequency, start, values, date_frequency=date_frequency)
                famepy.write_object(database, name, raw)
                manifest_objects.append(
                    manifest_object(
                        name,
                        kind,
                        values,
                        class_name="series",
                        type_code=raw.type_code,
                        frequency=raw.frequency,
                        first_index=start,
                    )
                )
            for name, kind in SCALAR_CASES:
                value = _scalar_value(name, s, first)
                raw_scalar = famepy.scalar(
                    kind, value, date_frequency="monthly" if kind == "date" else None
                )
                famepy.write_object(database, name, raw_scalar)
                manifest_objects.append(
                    manifest_object(
                        name, kind, [value], class_name="scalar", type_code=raw_scalar.type_code
                    )
                )
            database.post()

    r.check("write_matrix", write_all)

    def read_back() -> None:
        with famepy.open_database(path, session=session) as database:
            for name, kind, _frequency, _date in SERIES_CASES:
                values = _series_values(name, s, first)
                raw = _read(database, name)
                r.equal(f"read:{name}", raw.values, values)
                if len(values):
                    codes = classify_by_sentinel(raw.values, kind, s)
                    native = [famepy.missing_type(database, kind, v) for v in raw.values]
                    r.equal(f"classifier_agreement:{name}", codes.tolist(), native)
                r.equal(f"kind:{name}", famepy.quick_info(database, name).kind, kind)
            for name, kind in SCALAR_CASES:
                value = _scalar_value(name, s, first)
                raw = _read(database, name)
                if kind in ("string", "namelist"):
                    r.equal(f"read:{name}", raw.value, value)
                else:
                    r.equal(f"read:{name}", np.array([raw.value]), np.array([value], _DTYPES[kind]))
                if kind != "namelist":
                    typed: Any = [raw.value] if kind == "string" else np.array([raw.value])
                    r.equal(
                        f"classifier_agreement:{name}",
                        int(classify_by_sentinel(typed, kind, s)[0]),
                        famepy.missing_type(database, kind, raw.value),
                    )
            empty = _read(database, "empty")
            r.equal("empty_series_is_empty", empty.is_empty, True)
            r.equal(
                "empty_quick_info", famepy.quick_info(database, "empty").is_empty(s.index_nc), True
            )
            sub = _read(database, "p_series", first_index=first + 1, last_index=first + 2)
            r.equal("subrange_read", sub.values, _series_values("p_series", s, first)[1:3])
            r.equal("subrange_first_index", sub.first_index, first + 1)
            period = famepy.index_to_period(FREQUENCY_MONTHLY, first + 4, database=database)
            r.equal(
                "period_round_trip",
                famepy.period_to_index(FREQUENCY_MONTHLY, period, database=database),
                first + 4,
            )

    r.check("read_matrix", read_back)
    ctx.verify_in_new_process(
        "cross_process_matrix", {"database": str(path), "objects": manifest_objects}
    )

    def replace_flow() -> tuple[int, Any]:
        with famepy.open_database(path, "update", session=session) as database:
            status = 0
            try:
                famepy.write_object(database, "p_scalar", famepy.scalar("precision", 9.0))
            except famepy.FameError as error:
                status = error.status
            famepy.write_object(database, "p_scalar", famepy.scalar("precision", 9.0), replace=True)
            famepy.delete_object(database, "n_scalar")
            famepy.delete_object(database, "n_scalar", missing_ok=True)
            database.post()
            return status, np.array([_read(database, "p_scalar").value])

    result = r.check("replace_and_delete", replace_flow)
    if result is not None:
        r.equal("replace_value", result[1], np.array([9.0]))
        r.fact("existing_name_status", result[0], note="status when creating an existing name")
    r.check("finalize", session.finalize)


RAW_MATRIX_REQUIRED = _matrix_required()


def _read(database: famepy.Database, name: Any, **kwargs: Any) -> Any:
    """Read an object as Any so groups can inspect scalar and series fields."""
    return famepy.read_object(database, name, **kwargs)


DISCOVERY_REQUIRED = (
    "populate",
    "listing",
    "list_all",
    "wildcard_question",
    "wildcard_caret",
    "filter_class_series",
    "filter_type_numeric",
    "filter_frequency_monthly",
    "alias_off_lists",
    "scalar_range_from_quick_info",
    "long_name_length",
    "truncation_reported",
    "listing_after_truncation_still_works",
    "finalize",
)


def group_discovery(ctx: Context) -> None:
    session, r = ctx.session, ctx.recorder
    session.initialize()
    path = ctx.path("discovery.db")
    long_name = "L" * NAME_CAPACITY
    first = ctx.first

    def populate() -> None:
        with famepy.open_database(path, "create", session=session) as database:
            famepy.write_object(
                database, "sales_a", famepy.series("precision", "monthly", first, np.zeros(2))
            )
            famepy.write_object(
                database,
                "sales_b",
                famepy.series("numeric", "monthly", first, np.zeros(2, np.float32)),
            )
            famepy.write_object(database, "sale", famepy.scalar("precision", 1.0))
            famepy.write_object(database, "other", famepy.scalar("string", b"x"))
            famepy.write_object(database, long_name, famepy.scalar("boolean", 1))
            database.post()

    r.check("populate", populate)

    def listing() -> None:
        with famepy.open_database(path, session=session) as database:
            names = sorted(i.name_text for i in famepy.list_objects(database))
            r.equal("list_all", names, sorted([long_name, "OTHER", "SALE", "SALES_A", "SALES_B"]))
            r.equal(
                "wildcard_question",
                sorted(i.name_text for i in famepy.list_objects(database, "sales?")),
                ["SALES_A", "SALES_B"],
            )
            r.equal(
                "wildcard_caret",
                sorted(i.name_text for i in famepy.list_objects(database, "sales_^")),
                ["SALES_A", "SALES_B"],
            )
            r.equal(
                "filter_class_series",
                sorted(i.name_text for i in famepy.list_objects(database, classes="series")),
                ["SALES_A", "SALES_B"],
            )
            r.equal(
                "filter_type_numeric",
                sorted(i.name_text for i in famepy.list_objects(database, types="numeric")),
                ["SALES_B"],
            )
            r.equal(
                "filter_frequency_monthly",
                sorted(i.name_text for i in famepy.list_objects(database, frequencies="monthly")),
                ["SALES_A", "SALES_B"],
            )
            r.equal("alias_off_lists", len(famepy.list_objects(database, alias=False)) >= 5, True)
            scalar_info = famepy.list_objects(database, "sale")[0]
            info = famepy.quick_info(database, "sale")
            r.equal(
                "scalar_range_from_quick_info",
                [scalar_info.first_index, scalar_info.last_index],
                [info.first_index, info.last_index],
            )
            r.equal(
                "long_name_length",
                max(len(i.name) for i in famepy.list_objects(database)),
                NAME_CAPACITY,
            )
            r.expect_error(
                "truncation_reported",
                lambda: famepy.list_objects(database, capacity=8),
                (famepy.NameTruncatedError,),
            )
            r.equal("listing_after_truncation_still_works", len(famepy.list_objects(database)), 5)

    r.check("listing", listing)
    r.check("finalize", session.finalize)


COMMANDS_REQUIRED = (
    "display_command",
    "display_evaluates",
    "quiet_command",
    "quiet_returns_empty",
    "input_expansion",
    "input_expanded_evaluates",
    "input_cycle_refused",
    "computed_file_refused",
    "invalid_command_status",
    "invalid_command_output_captured",
    "temp_files_removed",
    "command_after_failure",
    "extended_error_not_configured",
    "finalize",
)


def group_commands(ctx: Context) -> None:
    """Command cases record predicates only; raw output never leaves the child."""
    session, r = ctx.session, ctx.recorder
    session.initialize()
    temp = ctx.path("cmd-temp")
    temp.mkdir(exist_ok=True)
    output = r.check(
        "display_command", lambda: famepy.run_command("display 2+2", session=session, temp_dir=temp)
    )
    r.equal(
        "display_evaluates",
        output is not None and b"4" in output,
        True,
        note="predicate only; output kept local",
    )
    quiet = r.check(
        "quiet_command",
        lambda: famepy.run_command("display 2+2", session=session, quiet=True, temp_dir=temp),
    )
    r.equal("quiet_returns_empty", quiet is not None and len(quiet) == 0, True)
    include = ctx.path("include.inp")
    nested = ctx.path("nested.inp")
    include.write_bytes(b"display 3+3\ninput nested\n")
    nested.write_bytes(b"display 4+4")
    out = r.check(
        "input_expansion",
        lambda: famepy.run_command(
            "input include", session=session, base_dir=ctx.scratch, temp_dir=temp
        ),
    )
    r.equal(
        "input_expanded_evaluates",
        out is not None and b"6" in out and b"8" in out and b"input" not in out.lower(),
        True,
        note="predicate only; output kept local",
    )
    cycle_a, cycle_b = ctx.path("cycle_a.inp"), ctx.path("cycle_b.inp")
    cycle_a.write_bytes(b"input cycle_b")
    cycle_b.write_bytes(b"input cycle_a")
    r.expect_error(
        "input_cycle_refused",
        lambda: famepy.run_command(
            "input cycle_a", session=session, base_dir=ctx.scratch, temp_dir=temp
        ),
        (famepy.IncludeError,),
    )
    r.expect_error(
        "computed_file_refused",
        lambda: famepy.expand_input(b"input file(name)", base_dir=ctx.scratch),
        (famepy.IncludeError,),
    )
    error = r.expect_error(
        "invalid_command_status",
        lambda: famepy.run_command("fail 513", session=session, temp_dir=temp),
        (famepy.CommandError,),
    )
    r.equal(
        "invalid_command_output_captured",
        error is not None and isinstance(getattr(error, "output", None), bytes),
        True,
    )
    r.equal("temp_files_removed", len(list(temp.iterdir())), 0)
    r.check(
        "command_after_failure",
        lambda: famepy.run_command("display 5+5", session=session, temp_dir=temp),
    )
    r.expect_error(
        "extended_error_not_configured",
        session.extended_error_text,
        (famepy.UnsupportedOperationError,),
    )
    r.check("finalize", session.finalize)


BRIDGE_REQUIRED = (
    "bridge_write",
    "bridge_read",
    "firstdate",
    "values_with_nan",
    "strict_missing",
    "scalar",
    "scalar_nan",
    "empty_needs_firstdate",
    "empty_with_firstdate",
    "reference_empty_preserved",
    "reference_empty_collapsed",
    "nan_written_as_nc",
    "nc_sentinel_value_matches",
    *verify_case_ids("cross_process_bridge", ["sc"]),
    "finalize",
)

BRIDGE_VALUES = np.array([1.0, np.nan, 3.5, -1e300, 2.0**-1000])


def group_bridge(ctx: Context) -> None:
    from tsecon import TSeries, mm

    session, r = ctx.session, ctx.recorder
    session.initialize()
    path = ctx.path("bridge.db")
    ts = TSeries(mm(2020, 1), BRIDGE_VALUES.copy())

    def write() -> None:
        bridge.write_tseries(path, "ts", ts, mode="create")
        bridge.write_scalar(path, "sc", 2.5, mode="update")
        bridge.write_scalar(path, "nan", math.nan, mode="update")
        bridge.write_tseries(path, "empty", TSeries(mm(2020, 3), np.empty(0)), mode="update")
        bridge.write_tseries(
            path, "ref_empty", TSeries(mm(2020, 4), np.empty(0)), mode="update", empty="reference"
        )

    r.check("bridge_write", write)

    def read() -> None:
        back = bridge.read_tseries(path, "ts")
        r.equal("firstdate", int(back.firstdate), int(ts.firstdate))
        r.equal("values_with_nan", np.array_equal(back.values, ts.values, equal_nan=True), True)
        r.expect_error(
            "strict_missing",
            lambda: bridge.read_tseries(path, "ts", missing="strict"),
            (bridge.MissingValueError,),
        )
        r.equal("scalar", bridge.read_scalar(path, "sc"), 2.5)
        r.equal("scalar_nan", math.isnan(bridge.read_scalar(path, "nan")), True)
        r.expect_error(
            "empty_needs_firstdate",
            lambda: bridge.read_tseries(path, "empty"),
            (bridge.EmptySeriesError,),
        )
        r.equal(
            "empty_with_firstdate",
            len(bridge.read_tseries(path, "empty", empty_firstdate=mm(2020, 3))),
            0,
        )
        r.equal("reference_empty_preserved", len(bridge.read_tseries(path, "ref_empty")), 1)
        collapsed = bridge.read_tseries(path, "ref_empty", empty="reference")
        r.equal(
            "reference_empty_collapsed",
            [len(collapsed), int(collapsed.firstdate)],
            [0, int(mm(2020, 4))],
        )
        with famepy.open_database(path, session=session) as database:
            raw = _read(database, "ts")
            r.equal(
                "nan_written_as_nc",
                classify_by_sentinel(raw.values, "precision", session.sentinels).tolist(),
                [0, 1, 0, 0, 0],
            )
            r.equal(
                "nc_sentinel_value_matches",
                np.array([raw.values[1]]),
                np.array([sentinel_value("precision", 1, session.sentinels)]),
            )

    r.check("bridge_read", read)
    ctx.verify_in_new_process(
        "cross_process_bridge",
        {
            "database": str(path),
            "objects": [
                manifest_object(
                    "sc",
                    "precision",
                    [2.5],
                    class_name="scalar",
                    type_code=int(famepy.ObjectType.PRECISION),
                )
            ],
        },
    )
    if ctx.julia is None:
        r.unsupported("julia_differential", "no Julia executable or project configured")
    else:
        from ._julia import run_julia_differential

        run_julia_differential(ctx, path)
    r.check("finalize", session.finalize)


GROUP_FUNCTIONS: dict[str, Callable[[Context], None]] = {
    "lifecycle": group_lifecycle,
    "database": group_database,
    "raw_matrix": group_raw_matrix,
    "discovery": group_discovery,
    "commands": group_commands,
    "bridge": group_bridge,
    # Not selectable from the command line: spawned by ``lifecycle``.
    "fresh_process": group_fresh_process,
}

REQUIRED_CASES: dict[str, tuple[str, ...]] = {
    "lifecycle": LIFECYCLE_REQUIRED,
    "database": DATABASE_REQUIRED,
    "raw_matrix": RAW_MATRIX_REQUIRED,
    "discovery": DISCOVERY_REQUIRED,
    "commands": COMMANDS_REQUIRED,
    "bridge": BRIDGE_REQUIRED,
}


def environment_identity() -> dict[str, Any]:
    import os
    import platform

    return {
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "pid_is_child": os.getpid() != os.getppid(),
    }


__all__ = [
    "GROUPS",
    "DEPENDENT_GROUPS",
    "GROUP_FUNCTIONS",
    "REQUIRED_CASES",
    "Context",
    "manifest_object",
    "run_verify",
    "verify_case_ids",
    "encode_value",
    "environment_identity",
]
