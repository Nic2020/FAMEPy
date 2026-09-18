# SPDX-License-Identifier: MIT
"""Shareable discovery report; native loading is an explicit isolated action."""

import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from typing import Any

from ._abi import GLOBALS, PRESENCE_ONLY, SIGNATURES, layout
from ._discovery import discover
from ._errors import LibraryNotFoundError, UnsupportedPlatformError, error_number
from ._probe import BOOTSTRAP, failure_kind, load_error_class


def diagnose(
    library: str | os.PathLike[str] | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
    probe: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Report discovery without paths/hostnames. Optional probe loads trusted CHLI.

    The probe runs in a child process, resolves symbols and never calls CHLI.
    Finding symbols does not verify signatures, runtime version or usability.
    Child stdout/stderr (including vendor loader output) is never forwarded.
    The trusted root (from ``root`` or discovery through ``FAME``) is carried
    to the child so that it registers the same dependency directories as a
    Session would.
    """
    if not 0 < timeout <= 300:
        raise ValueError("timeout must be greater than zero and at most 300 seconds.")
    dependencies: dict[str, str | None] = {}
    for name in ("FAMEPy", "TimeSeriesEconPy", "numpy"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    report: dict[str, Any] = {
        "schema_version": 3,
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dependencies": dependencies,
        "candidate_layout": layout(),
        "abi_verified": False,
        "native_calls_executed": False,
    }
    try:
        candidate = discover(library, root=root)
    except UnsupportedPlatformError:
        report["status"] = "unsupported_platform"
        return report
    except LibraryNotFoundError:
        report["status"] = "library_unavailable"
        return report
    report.update(
        status="library_found",
        source=candidate.source,
        trusted_root_known=candidate.root is not None,
    )
    if not probe:
        return report
    try:
        result = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP],
            input=json.dumps(
                {
                    "library": str(candidate.path),
                    "root": None if candidate.root is None else str(candidate.root),
                }
            ),
            capture_output=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        report["status"] = "probe_timeout"
        return report
    except OSError:
        report["status"] = "probe_start_failed"
        return report
    report["probe_exit_code"] = result.returncode
    if result.returncode:
        report["status"] = "probe_failed"
        report["probe_failure_kind"] = failure_kind(result.returncode, sys.platform)
        if result.returncode == 23:
            try:
                details = json.loads(result.stdout)
            except ValueError:
                details = None
            details = details if isinstance(details, dict) else {}
            errno = error_number(details.get("errno"))
            winerror = error_number(details.get("winerror"))
            report.update(
                load_errno=errno,
                load_winerror=winerror,
                load_error_class=load_error_class(errno, winerror),
            )
        return report
    try:
        child = json.loads(result.stdout)
        # Reconstruct an allowlisted report: never copy arbitrary child text.
        if not isinstance(child, dict):
            raise ValueError
        functions = {name: child["functions"][name] for name in SIGNATURES}
        globals_found = {name: child["globals"][name] for name in GLOBALS}
        presence = child.get("presence_only", {})
        if not isinstance(presence, dict):
            raise ValueError
        presence_only = {name: presence.get(name, False) for name in PRESENCE_ONLY}
        values = [*functions.values(), *globals_found.values(), *presence_only.values()]
        if any(type(value) is not bool for value in values):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        report["status"] = "probe_invalid_output"
        return report
    report.update(
        status="symbols_found"
        if all(functions.values()) and all(globals_found.values())
        else "symbols_missing",
        functions=functions,
        globals=globals_found,
        presence_only=presence_only,
    )
    return report
