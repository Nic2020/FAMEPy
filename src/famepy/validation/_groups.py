# SPDX-License-Identifier: MIT
"""Validation groups. Each runs in its own child process with its own runtime.

Every group owns the synthetic databases and files it creates under the scratch
directory. Cross-process persistence checks spawn a verification child that
reopens a database read-only and compares metadata and values against a
manifest the group wrote. Calendar indices are derived from the library's own
year/period conversion, never from a fixed synthetic constant.

Fixtures are constructed and validated before any destructive native call,
and every object is written, read and verified as its own case, so one
failing object never hides the others. A case that depends on an object
which was not created is reported as blocked, and the required-case list
still fails the group.

``REQUIRED_CASES`` lists, per group, the case identifiers that must be present
with status ``pass`` for the group to pass; the parent enforces it.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

import famepy
from famepy import bridge
from famepy._constants import FREQUENCY_MONTHLY, NAME_CAPACITY, AccessMode
from famepy._data import classify_by_sentinel, namelist_members, sentinel_value
from famepy._errors import HBMODE, FameError
from famepy._runtime import Session
from famepy._text import to_native
from famepy._wildcard import native_listing_count

from ._process import launch_worker, reserve_result
from ._report import Case, Recorder, encode_value
from ._schema import sanitize_case

GROUPS = ("lifecycle", "database", "raw_matrix", "discovery", "commands", "bridge")
DEPENDENT_GROUPS = ("database", "raw_matrix", "discovery", "commands", "bridge")

_DTYPES = {"precision": np.float64, "numeric": np.float32, "boolean": np.int32, "date": np.int64}
_NOT_CREATED = "object not created"
_PREREQUISITE = "prerequisite case did not pass"


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
        self._run_nested(case_id, "verify", ["--manifest", str(manifest_path)])

    def fresh_process(self, case_id: str) -> None:
        """Spawn a child that runs the one-shot lifecycle in a fresh process.

        This is the supported fresh-runtime boundary; it is exercised after
        the current child has finalized, proving that a new process (not a
        restart) initializes again.
        """
        self._run_nested(case_id, "fresh_process", [])

    def _run_nested(self, case_id: str, group: str, arguments: list[str]) -> None:
        """Run a nested worker and adopt its cases; its result file is the only input.

        The result must name the group that was requested, and every case is
        validated against the report schema before adoption, keeping every
        field (including the observation flag, so that an observation can
        never satisfy a required assertion). Malformed or duplicate cases
        are recorded as failures, never coerced.
        """
        command = self.child_command(["--group", group, *arguments])
        config = {key: value for key, value in self.config.items() if key not in ("result", "log")}
        tokens = reserve_result(self.scratch, case_id)
        try:
            result = launch_worker(command, config, self.timeout, tokens=tokens, nested=True)
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
        payload = result.payload
        if payload is None:
            kind = (result.result_kind or "invalid result").replace("_", " ")
            self.recorder.add(Case(case_id, "fail", note=f"verification {kind}"))
            return
        if payload.get("group") != group:
            self.recorder.add(Case(case_id, "fail", note="verification wrong group"))
            return
        cases = payload.get("cases")
        if not isinstance(cases, list) or not cases:
            self.recorder.add(Case(case_id, "fail", note="verification output invalid"))
            return
        seen: set[str] = set()
        for case in cases:
            clean = sanitize_case(case)
            if clean.get("note") == "malformed case record" or clean["id"] in seen:
                self.recorder.add(
                    Case(f"{case_id}:malformed", "fail", note="verification case malformed")
                )
                continue
            seen.add(clean["id"])
            self.recorder.add(
                Case(
                    f"{case_id}:{clean['id']}",
                    clean["status"],
                    error_type=clean.get("error_type"),
                    status_code=clean.get("status_code"),
                    errno=clean.get("errno"),
                    expected=clean.get("expected"),
                    actual=clean.get("actual"),
                    note=clean.get("note"),
                    frames=list(clean.get("frames", [])),
                    observation=bool(clean.get("observation", False)),
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
    """Encode values losslessly: floats by bit pattern, strings as bytes in hex.

    String values are bytes, not text: the library's own string sentinels are
    not ASCII, so a text decoding would fail or alter them. A namelist is
    described by its ordered members (each in hex), because the library
    documents no fixed layout for the list text.
    """
    if kind in ("precision", "numeric"):
        return [np.array(v, dtype=_DTYPES[kind]).tobytes().hex() for v in value]
    if kind in ("boolean", "date"):
        return [int(v) for v in value]
    if kind == "namelist":
        return [[m.hex() for m in namelist_members(bytes(v))] for v in value]
    return [bytes(v).hex() for v in value]


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
    if kind == "namelist":
        return [[bytes.fromhex(m) for m in members] for members in value]
    return [bytes.fromhex(v) for v in value]


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
    recorder.permit(
        session.sentinels.string_nc, session.sentinels.string_na, session.sentinels.string_nd
    )
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
            elif kind == "namelist":
                # Ordered members, not the list text: a reordered, missing or
                # corrupted member fails; a different layout does not.
                actual = [_members_or_none(raw.value)]
            elif kind == "string":
                actual = [raw.value]
            else:
                actual = np.array([raw.value])
            recorder.equal(f"values:{name}", actual, expected)


def _members_or_none(value: Any) -> list[bytes] | None:
    """Namelist members for comparison; text outside the grammar compares as None."""
    try:
        return list(namelist_members(value))
    except famepy.DataValidationError:
        return None


def _namelist_layout(value: bytes, members: tuple[bytes, ...]) -> str:
    """Which documented spelling the library used: a synthetic label, never the bytes."""
    if value == b"{" + b",".join(members) + b"}":
        return "compact"
    if value == b"{" + b", ".join(members) + b"}":
        return "blank_after_comma"
    return "other"


def _block_missing(recorder: Recorder, case_ids: tuple[str, ...] | list[str], note: str) -> None:
    """Record a blocked case for every identifier that has no case yet."""
    present = {case.id for case in recorder.cases}
    for case_id in case_ids:
        if case_id not in present:
            recorder.blocked(case_id, note)


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
        ascii_strings = facts.pop("string_sentinels_ascii")
        r.equal(
            "sentinel_facts",
            facts,
            {
                "precision_distinct": True,
                "numeric_distinct": True,
                "boolean_distinct": True,
                "index_distinct": True,
                "string_distinct": True,
            },
        )
        r.fact("string_sentinel_lengths", lengths)
        r.fact("string_sentinels_are_ascii", ascii_strings)
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
    strings = (s.string_nc, s.string_na, s.string_nd)
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
        "string_distinct": len(set(strings)) == 3,
        "string_lengths": [len(v) for v in strings],
        "string_sentinels_ascii": all(v.isascii() for v in strings),
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
    "mode_write_fixture",
    "mode_write_refused",
    "mode_write_native_status",
    "mode_write_fixture_unchanged",
    "mode_write_new_path_status",
    "mode_write_new_path_absent",
    "mode_direct_write_fixture",
    "mode_direct_write_refused",
    "mode_direct_write_native_status",
    "mode_direct_write_fixture_unchanged",
    "mode_direct_write_new_path_status",
    "mode_direct_write_new_path_absent",
    "mode_overwrite",
    "mode_overwrite_empties",
    "work_database_flow",
    "work_database",
    "stale_handle_after_finalize",
    "stale_close_harmless",
    "finalize",
)


def _native_open_status(session: Session, path: Path, mode: AccessMode) -> int:
    """Status of the library's own local open for ``mode``; a success is closed again.

    The public API refuses the connection modes before any native call, so
    the documented rejection by the local open is checked at the native
    layer: zero means the library opened the database (unexpected under the
    documentation and closed immediately), otherwise the status is returned.
    """
    text = to_native(str(path), what="database name")
    with session.operation("open database") as native:
        try:
            key = native.open_database(text, int(mode))
        except FameError as error:
            return error.status
        native.close_database(key)
    return 0


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
    for mode in ("readonly", "update", "shared"):

        def open_close(mode: str = mode) -> None:
            with famepy.open_database(path, mode, session=session) as database:
                if database.mode.name.lower() != mode:
                    raise AssertionError("mode mismatch")

        r.check(f"mode_{mode}", open_close)
    # Write and direct-write are modes of a database opened on a named server
    # connection, an API this package does not bind. The package refuses them
    # before any native call, and the library's own local open is documented
    # to reject them with the bad-mode status: both are asserted, on an
    # existing database (which must stay unchanged) and on a path that does
    # not exist (which must stay absent).
    for mode in ("write", "direct_write"):
        member = AccessMode[mode.upper()]
        existing = ctx.path(f"{mode}_existing.db")
        new_path = ctx.path(f"{mode}_new.db")

        def make_fixture(existing: Path = existing) -> None:
            with famepy.open_database(existing, "create", session=session) as database:
                famepy.write_object(database, "base", famepy.scalar("precision", 1.0))
                database.post()

        def reopen(existing: Path = existing) -> list[str]:
            with famepy.open_database(existing, session=session) as database:
                return sorted(info.name_text for info in famepy.list_objects(database))

        def native_status(target: Path, member: AccessMode = member) -> Callable[[], int]:
            return lambda: _native_open_status(session, target, member)

        def refused(existing: Path = existing, mode: str = mode) -> famepy.Database:
            return famepy.open_database(existing, mode, session=session)

        def absent(new_path: Path = new_path) -> bool:
            return new_path.exists()

        if r.ok(f"mode_{mode}_fixture", make_fixture):
            r.expect_error(f"mode_{mode}_refused", refused, (famepy.UnsupportedOperationError,))
            r.expect(f"mode_{mode}_native_status", native_status(existing), HBMODE)
            r.expect(f"mode_{mode}_fixture_unchanged", reopen, ["BASE"])
        else:
            _block_missing(
                r,
                (
                    f"mode_{mode}_refused",
                    f"mode_{mode}_native_status",
                    f"mode_{mode}_fixture_unchanged",
                ),
                "fixture database not created",
            )
        r.expect(f"mode_{mode}_new_path_status", native_status(new_path), HBMODE)
        r.expect(f"mode_{mode}_new_path_absent", absent, False)
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


# -- raw matrix fixtures ------------------------------------------------------

# name, kind, series frequency name, date frequency name. Every missing
# observation here is interior: the first and last values are normal, so the
# case tests preservation of NC/NA/ND inside a series, not endpoint handling.
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
# Endpoint cases: missing observations at the start or end of a series, or a
# series of nothing but ND. What the library persists for them is recorded as
# an observation (range and classification codes), not asserted, until the
# vendor rule is established. The one assertion is that the normal value
# survives at its position, which holds under any endpoint rule.
ENDPOINT_SHAPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("trailing_nd", ("value", "nd")),
    ("leading_nd", ("nd", "value")),
    ("trailing_nc", ("value", "nc")),
    ("leading_nc", ("nc", "value")),
    ("trailing_na", ("value", "na")),
    ("leading_na", ("na", "value")),
    ("all_nd", ("nd", "nd")),
)
ENDPOINT_KINDS: tuple[tuple[str, str, str], ...] = (
    ("p", "precision", "monthly"),
    ("n", "numeric", "monthly"),
    ("b", "boolean", "monthly"),
    ("d", "date", "monthly"),
    ("s", "string", "case"),
)


@dataclass(frozen=True)
class MatrixFixture:
    name: str
    kind: str
    raw: Any
    values: Any
    manifest: dict[str, Any] | None
    endpoint: tuple[str, ...] | None = None  # the shape for endpoint cases


def _series_values(name: str, s: famepy.Sentinels, first: int) -> Any:
    return {
        "p_series": np.array([1.0, s.precision_nc, s.precision_na, s.precision_nd, -2.5]),
        "n_series": np.array(
            [1.5, s.numeric_nc, s.numeric_na, s.numeric_nd, 2.5], dtype=np.float32
        ),
        "b_series": np.array([1, 0, s.boolean_nc, s.boolean_na, s.boolean_nd, 1], dtype=np.int32),
        "d_series": np.array(
            [first + 1, s.index_nc, s.index_na, s.index_nd, first + 5], dtype=np.int64
        ),
        "s_series": [b"alpha", b"", b"x" * 40],
        "s_missing_series": [b"start", s.string_nc, s.string_na, s.string_nd, b"normal"],
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


def _endpoint_values(kind: str, shape: tuple[str, ...], s: famepy.Sentinels, first: int) -> Any:
    normal: dict[str, Any] = {
        "precision": 1.0,
        "numeric": np.float32(1.5),
        "boolean": 1,
        "date": first + 1,
        "string": b"v",
    }
    items = [
        normal[kind] if slot == "value" else sentinel_value(kind, _CODE[slot], s) for slot in shape
    ]
    if kind == "string":
        return items
    return np.array(items, dtype=_DTYPES[kind])


_CODE = {"nc": 1, "na": 2, "nd": 3}


def endpoint_case_names() -> list[str]:
    return [
        f"{prefix}_{shape}"
        for prefix, _kind, _freq in ENDPOINT_KINDS
        for shape, _ in ENDPOINT_SHAPES
    ]


def build_matrix_fixtures(s: famepy.Sentinels, first: int) -> list[MatrixFixture]:
    """Construct and validate every object and its manifest entry before any write."""
    fixtures: list[MatrixFixture] = []
    for name, kind, frequency, date_frequency in SERIES_CASES:
        values = _series_values(name, s, first)
        start = 1 if frequency == "case" else first
        raw = famepy.series(kind, frequency, start, values, date_frequency=date_frequency)
        fixtures.append(
            MatrixFixture(
                name,
                kind,
                raw,
                values,
                manifest_object(
                    name,
                    kind,
                    values,
                    class_name="series",
                    type_code=raw.type_code,
                    frequency=raw.frequency,
                    first_index=start,
                ),
            )
        )
    for name, kind in SCALAR_CASES:
        value = _scalar_value(name, s, first)
        raw_scalar = famepy.scalar(
            kind, value, date_frequency="monthly" if kind == "date" else None
        )
        fixtures.append(
            MatrixFixture(
                name,
                kind,
                raw_scalar,
                value,
                manifest_object(
                    name, kind, [value], class_name="scalar", type_code=raw_scalar.type_code
                ),
            )
        )
    for prefix, kind, frequency in ENDPOINT_KINDS:
        for shape, slots in ENDPOINT_SHAPES:
            values = _endpoint_values(kind, slots, s, first)
            start = 1 if frequency == "case" else first
            raw = famepy.series(
                kind, frequency, start, values, date_frequency="monthly" if kind == "date" else None
            )
            fixtures.append(MatrixFixture(f"{prefix}_{shape}", kind, raw, values, None, slots))
    return fixtures


def matrix_object_names() -> list[str]:
    return (
        [name for name, *_ in SERIES_CASES]
        + [name for name, _ in SCALAR_CASES]
        + endpoint_case_names()
    )


def _matrix_read_ids() -> tuple[str, ...]:
    """Every case the read-back phase produces (blocked when it cannot run)."""
    ids: list[str] = [f"verify_object:{name}" for name in matrix_object_names()]
    for name, _kind, _frequency, _date in SERIES_CASES:
        ids.extend((f"read:{name}", f"values:{name}", f"kind:{name}"))
        if name not in ("empty", "s_empty"):
            ids.append(f"classifier_agreement:{name}")
    for name, kind in SCALAR_CASES:
        ids.extend((f"read:{name}", f"values:{name}"))
        if kind != "namelist":
            ids.append(f"classifier_agreement:{name}")
    for name in endpoint_case_names():
        ids.extend(
            (
                f"endpoint_read:{name}",
                f"endpoint_retained:{name}",
                f"endpoint_explicit:{name}",
            )
        )
        if not name.endswith("all_nd"):
            ids.append(f"endpoint_interior:{name}")
    ids.extend(
        (
            "empty_series_is_empty",
            "empty_quick_info",
            "subrange_read",
            "subrange_first_index",
            "period_round_trip",
        )
    )
    return tuple(ids)


def _matrix_required() -> tuple[str, ...]:
    return (
        "build_fixtures",
        "create_matrix",
        "post_matrix",
        "open_matrix",
        *(f"write:{name}" for name in matrix_object_names()),
        "extra_checks",
        *_matrix_read_ids(),
        *verify_case_ids("cross_process_matrix", matrix_object_names()),
        "replace_and_delete",
        "replace_fixture",
        "replace_existing",
        "replace_value",
        "delete_fixture",
        "delete_object",
        "delete_missing_ok",
        "deleted_object_absent",
        "post_after_replace",
        "finalize",
    )


def _object_case_ids(fixture: MatrixFixture) -> tuple[str, ...]:
    """The read-back cases one object produces."""
    name = fixture.name
    if fixture.endpoint is not None:
        return _endpoint_case_ids(name)
    if isinstance(fixture.raw, famepy.RawSeries):
        ids = (f"read:{name}", f"values:{name}", f"kind:{name}")
        return ids if name in ("empty", "s_empty") else (*ids, f"classifier_agreement:{name}")
    scalar_ids = (f"read:{name}", f"values:{name}")
    return (
        scalar_ids if fixture.kind == "namelist" else (*scalar_ids, f"classifier_agreement:{name}")
    )


def _read_series_case(
    r: Recorder, database: famepy.Database, fixture: MatrixFixture, s: famepy.Sentinels
) -> None:
    name, kind = fixture.name, fixture.kind
    raw = r.check(f"read:{name}", lambda: _read(database, name))
    if raw is None:
        _block_missing(
            r, (f"values:{name}", f"kind:{name}", f"classifier_agreement:{name}"), "read failed"
        )
        return
    r.equal(f"values:{name}", raw.values, fixture.values)
    if len(fixture.values):
        codes = classify_by_sentinel(raw.values, kind, s)
        native = [famepy.missing_type(database, kind, v) for v in raw.values]
        r.equal(f"classifier_agreement:{name}", codes.tolist(), native)
    r.equal(f"kind:{name}", famepy.quick_info(database, name).kind, kind)


def _read_scalar_case(
    r: Recorder, database: famepy.Database, fixture: MatrixFixture, s: famepy.Sentinels
) -> None:
    name, kind, value = fixture.name, fixture.kind, fixture.values
    raw = r.check(f"read:{name}", lambda: _read(database, name))
    if raw is None:
        _block_missing(r, (f"values:{name}", f"classifier_agreement:{name}"), "read failed")
        return
    if kind == "namelist":
        # The library documents the list text as members within braces
        # separated by commas and no fixed layout beyond that, so the
        # ordered members are asserted; the layout and the length are
        # recorded, never the returned bytes.
        expected_members = namelist_members(value)
        r.equal(f"values:{name}", _members_or_none(raw.value), list(expected_members))
        r.fact(f"namelist_length:{name}", len(raw.value))
        r.fact(
            f"namelist_layout:{name}",
            _namelist_layout(raw.value, expected_members),
            note="observation; the list text layout is not a documented contract",
        )
    elif kind == "string":
        r.equal(f"values:{name}", raw.value, value)
    else:
        r.equal(f"values:{name}", np.array([raw.value]), np.array([value], _DTYPES[kind]))
    if kind != "namelist":
        typed: Any = [raw.value] if kind == "string" else np.array([raw.value])
        r.equal(
            f"classifier_agreement:{name}",
            int(classify_by_sentinel(typed, kind, s)[0]),
            famepy.missing_type(database, kind, raw.value),
        )


def _endpoint_case_ids(name: str) -> tuple[str, ...]:
    ids = (f"endpoint_read:{name}", f"endpoint_retained:{name}", f"endpoint_explicit:{name}")
    return ids if name.endswith("all_nd") else (*ids, f"endpoint_interior:{name}")


def _slice(values: Any, start: int, stop: int) -> Any:
    return values[start:stop] if isinstance(values, list) else values[start:stop].copy()


def _read_endpoint_case(
    r: Recorder, database: famepy.Database, fixture: MatrixFixture, s: famepy.Sentinels
) -> dict[str, Any] | None:
    """Check an endpoint fixture and return the manifest for its persisted part.

    The library may keep the whole written range or drop missing endpoints
    (which rule applies is recorded, not assumed). Whatever it kept must lie
    inside the written range and, index by index, equal what was written
    there: an invented ordinary value, a changed missing code or a shifted
    range fails. The cross-process manifest is built from the written values
    over the retained indices, never from what was read back.
    """
    name, kind, shape = fixture.name, fixture.kind, fixture.endpoint
    assert shape is not None
    written = fixture.values
    written_first = fixture.raw.first_index
    written_last = written_first + len(written) - 1
    raw = r.check(f"endpoint_read:{name}", lambda: _read(database, name))
    if raw is None:
        _block_missing(r, _endpoint_case_ids(name), "read failed")
        return None
    if raw.is_empty:
        r.fact(f"endpoint_range:{name}", "empty")
        r.fact(f"endpoint_codes:{name}", [])
        r.equal(f"endpoint_retained:{name}", [True, raw.values], [True, _slice(written, 0, 0)])
        r.equal(f"endpoint_explicit:{name}", raw.values, _slice(written, 0, 0))
        start, stop = 0, 0
    else:
        within = written_first <= raw.first_index <= raw.last_index <= written_last
        # Offsets relative to the written first index, so the record is synthetic.
        r.fact(
            f"endpoint_range:{name}",
            [raw.first_index - written_first, raw.last_index - written_first],
        )
        r.fact(f"endpoint_codes:{name}", classify_by_sentinel(raw.values, kind, s).tolist())
        start = max(raw.first_index - written_first, 0)
        stop = min(raw.last_index - written_first + 1, len(written)) if within else 0
        retained = _slice(written, start, stop)
        r.equal(f"endpoint_retained:{name}", [within, raw.values], [True, retained])
        explicit = _read(database, name, first_index=raw.first_index, last_index=raw.last_index)
        r.equal(f"endpoint_explicit:{name}", explicit.values, retained)
    if "value" in shape:
        offset = shape.index("value")
        position = written_first + offset
        kept = (not raw.is_empty) and raw.first_index <= position <= raw.last_index
        actual: Any = None
        if kept:
            stored = raw.values[position - raw.first_index]
            actual = stored if kind == "string" else np.array([stored])
        expected: Any = written[offset]
        if kind != "string":
            expected = np.array([expected], dtype=_DTYPES[kind])
        r.equal(f"endpoint_interior:{name}", [kept, actual], [True, expected])
    return manifest_object(
        name,
        kind,
        _slice(written, start, stop),
        class_name="series",
        type_code=fixture.raw.type_code,
        frequency=fixture.raw.frequency,
        first_index=None if stop <= start else written_first + start,
    )


def group_raw_matrix(ctx: Context) -> None:
    session, r = ctx.session, ctx.recorder
    session.initialize()
    s = session.sentinels
    r.permit(s.string_nc, s.string_na, s.string_nd)
    first = ctx.first
    path = ctx.path("matrix.db")
    # Every fixture and manifest entry is built and validated first, so no
    # Python-side problem can surface after the database exists.
    fixtures = r.check("build_fixtures", lambda: build_matrix_fixtures(s, first))
    if fixtures is None:
        r.check("finalize", session.finalize)
        return
    by_name = {fixture.name: fixture for fixture in fixtures}
    created: set[str] = set()
    manifest_objects: list[dict[str, Any]] = []

    def write_all() -> None:
        with famepy.open_database(path, "create", session=session) as database:
            for fixture in fixtures:

                def write(fixture: MatrixFixture = fixture) -> None:
                    famepy.write_object(database, fixture.name, fixture.raw)

                if r.ok(f"write:{fixture.name}", write):
                    created.add(fixture.name)
                    if fixture.manifest is not None:
                        manifest_objects.append(fixture.manifest)
            r.check("post_matrix", database.post)

    r.check("create_matrix", write_all)
    _block_missing(r, [f"write:{name}" for name in by_name], "database not created")

    def verify_one(database: famepy.Database, fixture: MatrixFixture) -> None:
        if fixture.endpoint is not None:
            entry = _read_endpoint_case(r, database, fixture, s)
            if entry is not None:
                manifest_objects.append(entry)
        elif isinstance(fixture.raw, famepy.RawSeries):
            _read_series_case(r, database, fixture, s)
        else:
            _read_scalar_case(r, database, fixture, s)

    def extra_checks(database: famepy.Database) -> None:
        if "empty" in created:
            empty = _read(database, "empty")
            r.equal("empty_series_is_empty", empty.is_empty, True)
            r.equal(
                "empty_quick_info",
                famepy.quick_info(database, "empty").is_empty(s.index_nc),
                True,
            )
        if "p_series" in created:
            sub = _read(database, "p_series", first_index=first + 1, last_index=first + 2)
            r.equal("subrange_read", sub.values, _series_values("p_series", s, first)[1:3])
            r.equal("subrange_first_index", sub.first_index, first + 1)
        period = famepy.index_to_period(FREQUENCY_MONTHLY, first + 4, database=database)
        r.equal(
            "period_round_trip",
            famepy.period_to_index(FREQUENCY_MONTHLY, period, database=database),
            first + 4,
        )

    def read_all() -> None:
        with famepy.open_database(path, session=session) as database:
            # Each object's verification has its own exception boundary: a
            # failure inside it (classifier, metadata, explicit read) fails
            # that object, blocks only its unfinished cases and leaves the
            # objects after it to be checked.
            for fixture in fixtures:
                if fixture.name not in created:
                    continue

                def verify(fixture: MatrixFixture = fixture) -> None:
                    verify_one(database, fixture)

                r.check(f"verify_object:{fixture.name}", verify)
                _block_missing(r, _object_case_ids(fixture), _PREREQUISITE)
            r.check("extra_checks", lambda: extra_checks(database))

    r.check("open_matrix", read_all)
    _block_missing(r, _matrix_read_ids(), _PREREQUISITE)
    ctx.verify_in_new_process(
        "cross_process_matrix", {"database": str(path), "objects": manifest_objects}
    )
    _block_missing(r, verify_case_ids("cross_process_matrix", list(by_name)), _PREREQUISITE)

    # Replacement and deletion use their own fixtures, created here, so they
    # never depend on an object whose creation may have failed above.
    def replace_flow() -> None:
        with famepy.open_database(path, "update", session=session) as database:
            nine = famepy.scalar("precision", 9.0)
            one = famepy.scalar("precision", 1.0)
            if r.ok("replace_fixture", lambda: famepy.write_object(database, "r_scalar", nine)):
                status = 0
                try:
                    famepy.write_object(database, "r_scalar", one)
                except famepy.FameError as error:
                    status = error.status
                r.fact("existing_name_status", status, note="status when creating an existing name")
                if r.ok(
                    "replace_existing",
                    lambda: famepy.write_object(database, "r_scalar", one, replace=True),
                ):
                    r.equal(
                        "replace_value",
                        np.array([_read(database, "r_scalar").value]),
                        np.array([1.0]),
                    )
            if r.ok(
                "delete_fixture",
                lambda: famepy.write_object(
                    database, "del_scalar", famepy.scalar("precision", 4.0)
                ),
            ):
                r.check("delete_object", lambda: famepy.delete_object(database, "del_scalar"))
                r.check(
                    "delete_missing_ok",
                    lambda: famepy.delete_object(database, "del_scalar", missing_ok=True),
                )
                r.expect_error(
                    "deleted_object_absent",
                    lambda: famepy.quick_info(database, "del_scalar"),
                    (famepy.FameError,),
                )
            r.check("post_after_replace", database.post)

    r.check("replace_and_delete", replace_flow)
    _block_missing(
        r,
        (
            "replace_fixture",
            "replace_existing",
            "replace_value",
            "delete_fixture",
            "delete_object",
            "delete_missing_ok",
            "deleted_object_absent",
            "post_after_replace",
        ),
        _PREREQUISITE,
    )
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
    "filter_frequency_case",
    "filter_frequency_mixed",
    "filter_frequency_code",
    "filter_frequency_with_class",
    "filter_frequency_excludes_scalars",
    "filter_frequency_family_refused",
    "filter_frequency_invalid_refused",
    "filter_frequency_undefined",
    "filter_frequency_undefined_with_monthly",
    "options_normalized_after_listing",
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
    all_names = sorted([long_name, "CASE_S", "OTHER", "SALE", "SALES_A", "SALES_B"])

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
            famepy.write_object(database, "case_s", famepy.series("string", "case", 1, [b"a"]))
            famepy.write_object(database, "sale", famepy.scalar("precision", 1.0))
            famepy.write_object(database, "other", famepy.scalar("string", b"x"))
            famepy.write_object(database, long_name, famepy.scalar("boolean", 1))
            database.post()

    r.check("populate", populate)

    def listing() -> None:
        with famepy.open_database(path, session=session) as database:
            _discovery_cases(r, database, all_names)

    r.check("listing", listing)
    _block_missing(r, DISCOVERY_REQUIRED[:-1], _PREREQUISITE)
    r.check("finalize", session.finalize)


def _discovery_cases(r: Recorder, database: famepy.Database, all_names: list[str]) -> None:
    """One recorded case per listing call: a failure never hides the next predicate."""

    def names(**filters: Any) -> Callable[[], list[str]]:
        return lambda: sorted(i.name_text for i in famepy.list_objects(database, **filters))

    r.expect("list_all", names(), all_names)
    r.expect("wildcard_question", names(pattern="sales?"), ["SALES_A", "SALES_B"])
    r.expect("wildcard_caret", names(pattern="sales_^"), ["SALES_A", "SALES_B"])
    r.expect("filter_class_series", names(classes="series"), ["CASE_S", "SALES_A", "SALES_B"])
    r.expect("filter_type_numeric", names(types="numeric"), ["SALES_B"])
    # Frequency filtering is a metadata contract: exact frequencies only. The
    # native selection is narrowed with documented family/index words where
    # that cannot exclude a requested object, and left broad otherwise.
    r.expect("filter_frequency_monthly", names(frequencies="monthly"), ["SALES_A", "SALES_B"])
    r.expect("filter_frequency_case", names(frequencies="case"), ["CASE_S"])
    r.expect(
        "filter_frequency_mixed",
        names(frequencies=["monthly", "case"]),
        ["CASE_S", "SALES_A", "SALES_B"],
    )
    r.expect("filter_frequency_code", names(frequencies=FREQUENCY_MONTHLY), ["SALES_A", "SALES_B"])
    r.expect(
        "filter_frequency_with_class",
        names(classes="series", frequencies="monthly"),
        ["SALES_A", "SALES_B"],
    )
    r.expect(
        "filter_frequency_excludes_scalars",
        lambda: [
            info.is_series and info.frequency == FREQUENCY_MONTHLY
            for info in famepy.list_objects(database, frequencies="monthly")
        ],
        [True, True],
    )
    r.expect_error(
        "filter_frequency_family_refused",
        lambda: famepy.list_objects(database, frequencies="quarterly"),
        (ValueError,),
    )
    r.expect_error(
        "filter_frequency_invalid_refused",
        lambda: famepy.list_objects(database, frequencies="monthly;drop"),
        (ValueError,),
    )
    scalars = [name for name in all_names if name not in ("CASE_S", "SALES_A", "SALES_B")]
    r.expect("filter_frequency_undefined", names(frequencies="undefined"), scalars)
    r.expect(
        "filter_frequency_undefined_with_monthly",
        names(frequencies=["undefined", "monthly"]),
        sorted([*scalars, "SALES_A", "SALES_B"]),
    )
    # After a narrowed listing every option is back to ON: a broad listing
    # lists everything again.
    r.expect("options_normalized_after_listing", names(), all_names)
    r.fact(
        "scalar_frequency_codes",
        _observe(
            lambda: sorted({i.frequency for i in famepy.list_objects(database) if i.is_scalar})
        ),
    )
    # What the library's own selectors do to the wildcard without the package
    # filter: observations of the option semantics, isolated so that an
    # option error here is recorded and cannot abort the cases that follow.
    for label, options in (
        ("monthly_family", [(b"ITEM FREQUENCY", b"OFF"), (b"ITEM FREQUENCY MONTHLY", b"ON")]),
        ("case_index", [(b"ITEM INDEX", b"OFF"), (b"ITEM INDEX CASE", b"ON")]),
        ("date_index", [(b"ITEM INDEX", b"OFF"), (b"ITEM INDEX DATE", b"ON")]),
    ):

        def count(options: list[tuple[bytes, bytes]] = options) -> int:
            return native_listing_count(database, "?", options)

        r.fact(
            f"native_selector_count:{label}",
            _observe(count),
            note="native wildcard count under this selection alone; no package filter",
        )
    r.expect("alias_off_lists", lambda: len(famepy.list_objects(database, alias=False)) >= 6, True)

    def scalar_range_agrees() -> bool:
        listed = famepy.list_objects(database, "sale")[0]
        info = famepy.quick_info(database, "sale")
        return [listed.first_index, listed.last_index] == [info.first_index, info.last_index]

    r.expect("scalar_range_from_quick_info", scalar_range_agrees, True)
    r.expect(
        "long_name_length",
        lambda: max(len(i.name) for i in famepy.list_objects(database)),
        NAME_CAPACITY,
    )
    r.expect_error(
        "truncation_reported",
        lambda: famepy.list_objects(database, capacity=8),
        (famepy.NameTruncatedError,),
    )
    r.expect("listing_after_truncation_still_works", lambda: len(famepy.list_objects(database)), 6)


def _observe(function: Callable[[], Any]) -> Any:
    """Value of an observation, or the native status / error class when it fails."""
    try:
        return function()
    except FameError as error:
        return {"status": error.status}
    except Exception as error:  # noqa: BLE001 - observation only
        return {"error": type(error).__name__}


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
    "invalid_command_stage",
    "invalid_command_restored",
    "invalid_command_output_captured",
    "temp_files_removed",
    "command_after_failure",
    "extended_error_not_configured",
    "finalize",
)


def group_commands(ctx: Context) -> None:
    """Command cases record predicates only; raw output never leaves the child.

    A failing command records the stage that returned the status (redirect,
    command or restore) so that a redirection problem is distinguishable from
    a payload problem without any native text.
    """
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
    r.equal("invalid_command_stage", getattr(error, "stage", None), "command")
    r.equal("invalid_command_restored", getattr(error, "restore_status", -1), None)
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
    "MatrixFixture",
    "build_matrix_fixtures",
    "manifest_object",
    "run_verify",
    "verify_case_ids",
    "encode_value",
    "environment_identity",
]
