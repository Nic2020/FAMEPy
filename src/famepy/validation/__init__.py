# SPDX-License-Identifier: MIT
"""Consolidated native validation runner with a sanitized structured report.

One invocation reserves a new run directory inside a new or empty scratch
directory, runs a read-only preflight in the parent process and then each
selected group in its own child process with a timeout that terminates the
whole child tree. Groups after ``lifecycle`` are blocked when lifecycle does
not pass. Required native groups never silently pass without a library:
without ``--native`` they are reported as blocked, and a missing or
unloadable library blocks them with the discovery status.

The parent trusts nothing a child prints. Every case is validated against a
field and value schema; a group passes only when its child exited cleanly,
every case it reported is well formed, every required case is present with
status ``pass``, and no case failed or was blocked. The report contains
versions, platform, artifact identity, symbol presence, per-group status with
counts, exit codes and sanitized cases. It never contains paths, hostnames,
raw native or loader text, or command payloads.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import famepy
from famepy._probe import failure_kind

from ._groups import DEPENDENT_GROUPS, GROUPS, REQUIRED_CASES
from ._process import run_child
from ._report import STATUSES

SCHEMA_VERSION = 2

_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.-]{0,159}$")
_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_NOTE = re.compile(r"^[A-Za-z0-9 _,.;:()'+=-]{1,200}$")
_TEXT = re.compile(r"^[A-Za-z0-9 _,.;:+={}\[\]()<>'-]{0,256}$")
_KEY = re.compile(r"^[A-Za-z0-9_:. -]{1,64}$")
_HEX = re.compile(r"^(?:[0-9a-f]{8}|[0-9a-f]{16})$")
_FRAME = re.compile(r"^[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*\.py:[A-Za-z0-9_<>]{1,80}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{7,64}(?:-dirty)?$")
_ATTESTATION = re.compile(r"^[0-9a-f]{64}$")
_WHEEL_NAME = re.compile(r"^[Ff][Aa][Mm][Ee][Pp][Yy]-[0-9A-Za-z.!+]+-py3-none-any\.whl$")
_MAX_LIST = 512
_MAX_DEPTH = 4


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def abi_table_sha256() -> str:
    """Identity of the candidate declaration table the package binds."""
    digest = hashlib.sha256()
    for name in sorted(famepy._abi.SIGNATURES):
        spec = famepy._abi.SIGNATURES[name]
        arguments = ",".join(getattr(a, "__name__", repr(a)) for a in spec.arguments)
        digest.update(f"{name}:{spec.convention}:{arguments}\n".encode())
    for name in sorted(famepy._abi.GLOBALS):
        ctype = famepy._abi.GLOBAL_TYPES[name]
        digest.update(f"{name}:{getattr(ctype, '__name__', repr(ctype))}\n".encode())
    return digest.hexdigest()


def _wheel_identity(wheel: Path, package_dir: Path) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "wheel_sha256": None,
        "wheel_name": None,
        "wheel_name_valid": False,
        "wheel_matches_installed": None,
    }
    try:
        identity["wheel_sha256"] = _hash_file(wheel)
    except OSError:
        identity["wheel_error"] = "unreadable"
        return identity
    identity["wheel_name_valid"] = bool(_WHEEL_NAME.fullmatch(wheel.name))
    identity["wheel_name"] = wheel.name if identity["wheel_name_valid"] else None
    try:
        with zipfile.ZipFile(wheel) as archive:
            shipped = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist()
                if name.startswith("famepy/") and name.endswith(".py")
            }
    except (zipfile.BadZipFile, OSError):
        identity["wheel_error"] = "not_a_wheel"
        return identity
    installed = {
        "famepy/" + file.relative_to(package_dir).as_posix(): hashlib.sha256(
            file.read_bytes()
        ).hexdigest()
        for file in package_dir.rglob("*.py")
    }
    identity["wheel_matches_installed"] = shipped == installed
    return identity


def classify_import(package_dir: Path, cwd: Path) -> dict[str, Any]:
    """Classify where the imported package lives, without recording paths.

    A *checkout* import is recognized structurally: the package sits in a
    ``src`` directory next to a ``pyproject.toml`` (or directly beside one),
    which is how a source tree looks and how an installed distribution never
    looks. A *site-packages* import is recognized by its directory chain. The
    working directory being an ancestor of the package (for example when the
    campaign runs from a directory that contains its virtual environment) is
    reported as an observation only and never counts as a checkout import.
    """
    checkout = (
        package_dir.parent.name == "src"
        and (package_dir.parent.parent / "pyproject.toml").is_file()
    ) or (package_dir.parent / "pyproject.toml").is_file()
    parts = {part.lower() for part in package_dir.parts}
    site = ("site-packages" in parts or "dist-packages" in parts) and not checkout
    try:
        ancestor = package_dir.is_relative_to(cwd)
    except ValueError:
        ancestor = False
    return {
        "imported_from_checkout": checkout,
        "imported_from_site_packages": site,
        "working_directory_is_ancestor": ancestor,
    }


def package_identity(
    wheel: Path | None,
    source_sha: str | None,
    *,
    package_dir: Path | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Describe the imported package without revealing where it lives.

    ``source_sha`` must already be validated as a hexadecimal revision. When a
    wheel is given, its shipped sources are compared with the imported package.
    ``package_dir`` and ``cwd`` are injectable for tests only.
    """
    package_dir = Path(famepy.__file__).resolve().parent if package_dir is None else package_dir
    cwd = Path.cwd().resolve() if cwd is None else cwd
    digest = hashlib.sha256()
    for file in sorted(package_dir.rglob("*.py")):
        digest.update(file.relative_to(package_dir).as_posix().encode())
        digest.update(file.read_bytes())
    identity: dict[str, Any] = {
        "famepy_version": famepy.__version__,
        "package_sources_sha256": digest.hexdigest(),
        **classify_import(package_dir, cwd),
        "source_sha": source_sha if source_sha and _SOURCE_SHA.fullmatch(source_sha) else None,
        "wheel_sha256": None,
        "wheel_name": None,
        "wheel_name_valid": False,
        "wheel_matches_installed": None,
    }
    if wheel is not None:
        identity.update(_wheel_identity(wheel, package_dir))
    try:
        identity["distribution_version"] = importlib.metadata.version("FAMEPy")
    except importlib.metadata.PackageNotFoundError:
        identity["distribution_version"] = None
    return identity


def dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in ("FAMEPy", "TimeSeriesEconPy", "numpy"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def reserve_scratch(scratch: Path) -> tuple[dict[str, Any], Path | None]:
    """Accept only a new or empty directory and reserve a fresh run directory.

    Nothing pre-existing is ever written, replaced or removed. A symlink, a
    non-directory or a nonempty directory makes the scratch unusable and no
    child is launched. The write check creates a new exclusive file inside
    the reserved run directory only.
    """
    facts: dict[str, Any] = {"created": False, "usable": False, "reason": None}
    run_dir: Path | None = None
    try:
        if scratch.is_symlink():
            facts["reason"] = "symlink"
        elif scratch.exists():
            if not scratch.is_dir():
                facts["reason"] = "not_a_directory"
            elif any(scratch.iterdir()):
                facts["reason"] = "not_empty"
        else:
            scratch.mkdir(parents=True, exist_ok=False)
            facts["created"] = True
        if facts["reason"] is None:
            # Short identifiers keep deep scratch paths under Windows limits;
            # exclusive creation guarantees the directory is new.
            for _attempt in range(8):
                candidate = scratch / ("run-" + uuid.uuid4().hex[:8])
                try:
                    candidate.mkdir(exist_ok=False)
                except FileExistsError:
                    continue
                break
            else:
                raise OSError(errno.EEXIST, "could not reserve a run directory")
            probe = candidate / "write-check"
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                os.write(descriptor, b"ok")
            finally:
                os.close(descriptor)
            probe.unlink()
            run_dir = candidate
            facts["usable"] = True
    except OSError as error:
        facts["reason"] = "os_error"
        facts["errno"] = error.errno
        run_dir = None
        facts["usable"] = False
    return facts, run_dir


def preflight(
    options: dict[str, Any], *, scratch_facts: dict[str, Any] | None = None
) -> dict[str, Any]:
    if scratch_facts is None:
        scratch_facts, _run_dir = reserve_scratch(Path(options["scratch"]))
    attestation = options.get("abi_attestation")
    report: dict[str, Any] = {
        "environment": {
            "platform": sys.platform,
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "dependencies": dependency_versions(),
        "identity": package_identity(options.get("wheel"), options.get("source_sha")),
        "scratch": scratch_facts,
        "native_opt_in": bool(options.get("native")),
        "abi_checklist_functions": len(famepy._abi.SIGNATURES),
        "abi_presence_only": list(famepy._abi.PRESENCE_ONLY),
        "abi_table_sha256": abi_table_sha256(),
        "abi_attestation": attestation
        if isinstance(attestation, str) and _ATTESTATION.fullmatch(attestation)
        else None,
        "licensing_environment_configured": bool(os.environ.get("FAME")),
    }
    if options.get("backend"):
        report["library"] = {"status": "injected_backend", "backend": True}
        return report
    diagnosis = famepy.diagnose(
        options.get("library"),
        root=options.get("root"),
        probe=True,
        timeout=min(float(options.get("timeout", 120.0)), 300.0),
    )
    report["library"] = {
        key: diagnosis.get(key)
        for key in (
            "status",
            "source",
            "trusted_root_known",
            "probe_exit_code",
            "probe_failure_kind",
            "load_error_class",
            "load_errno",
            "load_winerror",
            "presence_only",
        )
        if key in diagnosis
    }
    if "functions" in diagnosis:
        report["library"]["missing_functions"] = sorted(
            name for name, present in diagnosis["functions"].items() if not present
        )
        report["library"]["missing_globals"] = sorted(
            name for name, present in diagnosis["globals"].items() if not present
        )
    return report


# -- child result validation ------------------------------------------------


def _clean_value(value: Any, depth: int = 0) -> tuple[bool, Any]:
    """Accept only synthetic-looking values: numbers, short safe strings, bits."""
    if value is None or isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return -(2**63) <= value < 2**63, value
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf")), value
    if isinstance(value, str):
        return bool(_TEXT.fullmatch(value)), value
    if depth >= _MAX_DEPTH:
        return False, None
    if isinstance(value, list):
        if len(value) > _MAX_LIST:
            return False, None
        cleaned = []
        for item in value:
            ok, clean = _clean_value(item, depth + 1)
            if not ok:
                return False, None
            cleaned.append(clean)
        return True, cleaned
    if isinstance(value, dict):
        if set(value) == {"bits"}:
            return isinstance(value["bits"], str) and bool(_HEX.fullmatch(value["bits"])), value
        if set(value) == {"ascii"}:
            return isinstance(value["ascii"], str) and bool(_TEXT.fullmatch(value["ascii"])), value
        if len(value) > 32:
            return False, None
        cleaned_dict: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                return False, None
            ok, clean = _clean_value(item, depth + 1)
            if not ok:
                return False, None
            cleaned_dict[key] = clean
        return True, cleaned_dict
    return False, None


def _sanitize_case(case: Any) -> dict[str, Any]:
    """Validate one child case against the schema; malformed records fail."""
    malformed = {"id": "malformed", "status": "fail", "note": "malformed case record"}
    if not isinstance(case, dict):
        return malformed
    case_id = case.get("id")
    if not isinstance(case_id, str) or not _ID.fullmatch(case_id):
        return malformed
    status = case.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        return {**malformed, "id": case_id}
    clean: dict[str, Any] = {"id": case_id, "status": status}
    for key in ("error_type", "note"):
        if key in case:
            value = case[key]
            pattern = _ERROR_TYPE if key == "error_type" else _NOTE
            if not isinstance(value, str) or not pattern.fullmatch(value):
                return {**malformed, "id": case_id}
            clean[key] = value
    for key in ("status_code", "errno"):
        if key in case:
            value = case[key]
            if not isinstance(value, int) or isinstance(value, bool) or abs(value) >= 2**32:
                return {**malformed, "id": case_id}
            clean[key] = value
    for key in ("expected", "actual"):
        if key in case:
            ok, value = _clean_value(case[key])
            if not ok:
                return {**malformed, "id": case_id}
            clean[key] = value
    if "frames" in case:
        frames = case["frames"]
        if (
            not isinstance(frames, list)
            or len(frames) > 8
            or not all(isinstance(f, str) and _FRAME.fullmatch(f) for f in frames)
        ):
            return {**malformed, "id": case_id}
        clean["frames"] = frames
    if "observation" in case:
        if case["observation"] is not True:
            return {**malformed, "id": case_id}
        clean["observation"] = True
    return clean


def _group_status(record: dict[str, Any], required: tuple[str, ...]) -> str:
    cases = record["cases"]
    statuses = {case["id"]: case["status"] for case in cases}
    if len(statuses) != len(cases):
        record["duplicate_cases"] = True
        return "fail"
    observations = [
        case["id"] for case in cases if case["id"] in required and case.get("observation")
    ]
    if observations:
        record["required_observations"] = observations
        return "fail"
    if any(
        case["id"] == "malformed" or case.get("note") == "malformed case record" for case in cases
    ):
        record["malformed_cases"] = sum(
            1 for c in cases if c.get("note") == "malformed case record"
        )
    missing = [case_id for case_id in required if case_id not in statuses]
    if missing:
        record["required_missing"] = missing
    not_passed = [case_id for case_id in required if statuses.get(case_id) not in (None, "pass")]
    if not_passed:
        record["required_not_passed"] = not_passed
    if record.get("exit_code") != 0 or record.get("malformed_cases") or missing:
        return "fail"
    if record["counts"]["fail"]:
        return "fail"
    if any(statuses.get(case_id) == "unsupported" for case_id in required):
        return "fail"
    if record["counts"]["blocked"]:
        return "blocked"
    return "pass"


def _run_child(group: str, options: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    scratch = run_dir / group
    scratch.mkdir(exist_ok=False)
    config: dict[str, Any] = {
        "scratch": str(scratch),
        "library": options.get("library"),
        "root": options.get("root"),
        "backend": options.get("backend"),
        "timeout": float(options.get("timeout", 120.0)),
        "julia": options.get("julia"),
    }
    command = [sys.executable, "-m", "famepy.validation._child", "--group", group]
    started = time.monotonic()
    record: dict[str, Any] = {"status": "fail", "cases": [], "counts": {}}
    try:
        result = run_child(command, json.dumps(config), float(options.get("timeout", 120.0)) * 4)
    except subprocess.TimeoutExpired:
        record.update(timed_out=True, exit_code=None, exit_kind="timeout")
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        return record
    except OSError as error:
        record.update(exit_code=None, exit_kind="start_failed", errno=error.errno)
        return record
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    record["exit_code"] = result.returncode
    record["timed_out"] = False
    if result.returncode not in (0, 32):
        record["exit_kind"] = {
            30: "invalid_configuration",
            31: "backend_setup_failed",
            33: "manifest_invalid",
        }.get(result.returncode, failure_kind(result.returncode, sys.platform))
        if result.returncode == 31:
            try:
                details = json.loads(result.stdout)
                for key in ("setup_error", "status_code", "errno", "winerror"):
                    value = details.get(key)
                    if isinstance(value, str) and _ERROR_TYPE.fullmatch(value):
                        record[key] = value
                    elif isinstance(value, int) and not isinstance(value, bool):
                        record[key] = value
            except (ValueError, AttributeError):
                pass
        return record
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("group") != group:
            raise ValueError
        cases = payload["cases"]
        if not isinstance(cases, list):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        record["exit_kind"] = "invalid_output"
        return record
    if not cases:
        record["exit_kind"] = "empty_cases"
        return record
    record["cases"] = [_sanitize_case(case) for case in cases]
    record["counts"] = {
        status: sum(1 for c in record["cases"] if c.get("status") == status) for status in STATUSES
    }
    if result.returncode == 32:
        record["exit_kind"] = "group_exception"
    record["status"] = _group_status(record, REQUIRED_CASES[group])
    return record


def run(options: dict[str, Any]) -> dict[str, Any]:
    """Run preflight and selected groups; return the report dictionary."""
    selected = list(options.get("groups") or GROUPS)
    unknown = [g for g in selected if g not in GROUPS]
    if unknown:
        raise ValueError(f"Unknown groups: {', '.join(unknown)}")
    source_sha = options.get("source_sha")
    if source_sha is not None and not _SOURCE_SHA.fullmatch(str(source_sha)):
        raise ValueError("source-sha must be a hexadecimal revision, optionally suffixed -dirty")
    attestation = options.get("abi_attestation")
    if attestation is not None and not _ATTESTATION.fullmatch(str(attestation)):
        raise ValueError("abi-attestation must be a 64-character hexadecimal digest")
    scratch_facts, run_dir = reserve_scratch(Path(options["scratch"]))
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "preflight": preflight(options, scratch_facts=scratch_facts),
        "groups": {},
    }
    library_ok = report["preflight"]["library"].get("status") in (
        "symbols_found",
        "injected_backend",
    )
    blocked_reason = None
    if not options.get("native"):
        blocked_reason = "native tests require the explicit --native opt-in"
    elif run_dir is None or not scratch_facts.get("usable"):
        blocked_reason = "scratch directory must be a new or empty directory"
    elif not library_ok:
        blocked_reason = "library discovery or symbol probe did not succeed"
    if blocked_reason is None and not options.get("backend"):
        identity = report["preflight"]["identity"]
        if identity.get("imported_from_checkout") or not identity.get(
            "imported_from_site_packages"
        ):
            blocked_reason = "native validation requires an installed package"
        elif not identity.get("source_sha"):
            blocked_reason = "native validation requires a source revision"
        elif not identity.get("wheel_name_valid") or not identity.get("wheel_matches_installed"):
            blocked_reason = "native validation requires the matching installed wheel"
        elif report["preflight"]["abi_attestation"] is None:
            blocked_reason = "native validation requires the reviewed ABI checklist digest"
    lifecycle_ok = False
    for group in GROUPS:
        if group not in selected:
            continue
        if blocked_reason is not None:
            report["groups"][group] = {"status": "blocked", "note": blocked_reason}
            continue
        if group in DEPENDENT_GROUPS and "lifecycle" in selected and not lifecycle_ok:
            report["groups"][group] = {"status": "blocked", "note": "lifecycle group did not pass"}
            continue
        assert run_dir is not None
        record = _run_child(group, options, run_dir)
        report["groups"][group] = record
        if group == "lifecycle":
            lifecycle_ok = record["status"] == "pass"
    summary: dict[str, Any] = {
        status: sum(1 for g in report["groups"].values() if g.get("status") == status)
        for status in STATUSES
    }
    summary["cases"] = {
        status: sum(g.get("counts", {}).get(status, 0) for g in report["groups"].values())
        for status in STATUSES
    }
    report["summary"] = summary
    groups = report["groups"]
    if groups and all(g.get("status") == "pass" for g in groups.values()):
        report["result"] = "PASS"
    elif summary["fail"] == 0:
        report["result"] = "BLOCKED"
    else:
        report["result"] = "FAIL"
    return report


__all__ = [
    "GROUPS",
    "REQUIRED_CASES",
    "SCHEMA_VERSION",
    "abi_table_sha256",
    "classify_import",
    "package_identity",
    "preflight",
    "reserve_scratch",
    "run",
]
