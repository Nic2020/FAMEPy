# SPDX-License-Identifier: MIT
"""Shareable discovery report; native loading is an explicit isolated action."""

import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from typing import Any

from ._abi import GLOBALS, SIGNATURES, layout
from ._discovery import discover
from ._errors import LibraryNotFoundError, UnsupportedPlatformError, error_number
from ._probe import BOOTSTRAP, failure_kind, load_error_class


def diagnose(
    library: str | os.PathLike[str] | None = None,
    *,
    probe: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Report discovery without paths/hostnames. Optional probe loads trusted CHLI.

    The probe runs in a child process, resolves symbols and never calls CHLI.
    Finding symbols does not verify signatures, runtime version or usability.
    Child stdout/stderr (including vendor loader output) is never forwarded.
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
        "schema_version": 2,
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dependencies": dependencies,
        "candidate_layout": layout(),
        "abi_verified": False,
        "native_calls_executed": False,
    }
    try:
        candidate = discover(library)
    except UnsupportedPlatformError:
        report["status"] = "unsupported_platform"
        return report
    except LibraryNotFoundError:
        report["status"] = "library_unavailable"
        return report
    report.update(status="library_found", source=candidate.source)
    if not probe:
        return report
    try:
        result = subprocess.run(
            [sys.executable, "-c", BOOTSTRAP],
            input=json.dumps({"library": str(candidate.path)}),
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
        if any(type(value) is not bool for value in [*functions.values(), *globals_found.values()]):
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
    )
    return report
