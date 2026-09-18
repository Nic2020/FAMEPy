# SPDX-License-Identifier: MIT
import ctypes as ct
import importlib
import io
import json
import os
import subprocess
import sys

import pytest

from famepy import LibraryLoadError, diagnose
from famepy._abi import GLOBALS, SIGNATURES
from famepy._discovery import discover
from famepy._probe import BOOTSTRAP, failure_kind, main
from famepy._runtime import Runtime


@pytest.fixture
def candidate(tmp_path):
    path = tmp_path / "synthetic-library.dll"
    path.write_bytes(b"not a library")
    return path


@pytest.mark.parametrize("number", [126, 193, 1114, 5, 999])
def test_runtime_retains_error_numbers_without_messages(candidate, monkeypatch, number):
    failure = OSError(13, "private-loader-path")
    failure.winerror = number

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ct, "CDLL", fail)
    with pytest.raises(LibraryLoadError) as result:
        Runtime(discover(candidate)).load()
    assert result.value.errno == 13
    assert result.value.winerror == number
    assert "private" not in str(result.value)
    assert result.value.__suppress_context__


@pytest.mark.parametrize(
    "code,kind",
    [
        (20, "package_import_failed"),
        (21, "invalid_request"),
        (22, "discovery_failed"),
        (23, "library_load_failed"),
        (24, "child_exception"),
        (25, "child_setup_failed"),
        (1, "unclassified_exit"),
    ],
)
def test_parent_reports_known_failure_kind(candidate, monkeypatch, code, kind):
    module = importlib.import_module("famepy.diagnostics")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, code, "private stdout", "private stderr"),
    )
    report = diagnose(candidate, probe=True)
    assert report["probe_failure_kind"] == kind
    assert report["probe_exit_code"] == code
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "number,expected",
    [
        (126, "library_or_dependency_not_found"),
        (193, "bad_image"),
        (1114, "initialization_failed"),
        (5, "access_denied"),
        (999, "other"),
        (None, "other"),
        (True, "other"),
        ("private text", "other"),
        (2**80, "other"),
    ],
)
def test_parent_allowlists_loader_details(candidate, monkeypatch, number, expected):
    module = importlib.import_module("famepy.diagnostics")
    payload = json.dumps(
        {
            "winerror": number,
            "errno": None,
            "path": "private path",
            "load_error_class": "private text",
        }
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 23, payload, "private stderr"),
    )
    report = diagnose(candidate, probe=True)
    assert report["load_error_class"] == expected
    assert report["load_errno"] is None
    assert "private" not in json.dumps(report)


def test_numeric_posix_error_is_preserved_without_guessing(candidate, monkeypatch):
    module = importlib.import_module("famepy.diagnostics")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 23, '{"errno": 8, "winerror": null}', ""),
    )
    report = diagnose(candidate, probe=True)
    assert report["load_errno"] == 8
    assert report["load_error_class"] == "other"


@pytest.mark.parametrize(
    "code,system,kind",
    [
        (-11, "linux", "signal_exit"),
        (1, "linux", "unclassified_exit"),
        (0xC0000005, "win32", "windows_exception_exit"),
        (-1073741819, "win32", "windows_exception_exit"),
    ],
)
def test_abnormal_exit_classification(code, system, kind):
    assert failure_kind(code, system) == kind


def test_actual_child_import_failure_is_not_load_failure(candidate, monkeypatch, tmp_path):
    module = importlib.import_module("famepy.diagnostics")
    original_run = subprocess.run

    def isolated(command, **kwargs):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        return original_run(
            [command[0], "-S", *command[1:]], env=environment, cwd=tmp_path, **kwargs
        )

    monkeypatch.setattr(module.subprocess, "run", isolated)
    report = diagnose(candidate, probe=True)
    assert report["probe_failure_kind"] == "package_import_failed"
    assert report["probe_exit_code"] == 20


def test_actual_child_load_failure_preserves_os_numbers(candidate):
    report = diagnose(candidate, probe=True)
    assert report["probe_failure_kind"] == "library_load_failed"
    assert report["probe_exit_code"] == 23
    if sys.platform == "win32":
        assert report["load_winerror"] == 193
        assert report["load_error_class"] == "bad_image"
    assert str(candidate) not in json.dumps(report)


@pytest.mark.parametrize("payload", ["not json", "null", "[]", "{}", '{"library":3}'])
def test_child_rejects_malformed_request_before_loading(payload, monkeypatch):
    module = importlib.import_module("famepy._probe")
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(module, "suppress_error_dialogs", lambda: pytest.fail("unexpected setup"))
    assert main() == 21


@pytest.mark.parametrize("where,code", [("setup", 25), ("probe", 24)])
def test_child_contains_unexpected_exceptions(candidate, monkeypatch, capsys, where, code):
    module = importlib.import_module("famepy._probe")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"library": str(candidate)})))
    monkeypatch.setattr(module, "suppress_error_dialogs", lambda: None)

    def fail(*args):
        raise RuntimeError("private exception")

    monkeypatch.setattr(
        module, "suppress_error_dialogs" if where == "setup" else "probe_library", fail
    )
    assert main() == code
    assert "private" not in capsys.readouterr().out


def test_actual_child_reports_discovery_failure(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", BOOTSTRAP],
        input=json.dumps({"library": str(tmp_path / "absent.dll")}),
        encoding="utf-8",
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 22
    assert result.stdout == result.stderr == ""


def test_success_map_validation_and_unknown_fields(candidate, monkeypatch):
    module = importlib.import_module("famepy.diagnostics")
    payload = {
        "functions": dict.fromkeys(SIGNATURES, True),
        "globals": dict.fromkeys(GLOBALS, True),
        "path": "private",
    }

    def result(*a, **k):
        return subprocess.CompletedProcess(a, 0, json.dumps(payload), "private")

    monkeypatch.setattr(module.subprocess, "run", result)
    report = diagnose(candidate, probe=True)
    assert report["status"] == "symbols_found"
    assert report["abi_verified"] is False
    assert "private" not in json.dumps(report)
    payload["globals"]["FSTRNA"] = "private"
    report = diagnose(candidate, probe=True)
    assert report["status"] == "probe_invalid_output"
    assert "private" not in json.dumps(report)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process error mode")
def test_error_mode_is_set_only_in_child():
    kernel = ct.WinDLL("kernel32")
    get_mode = kernel.GetErrorMode
    get_mode.argtypes = []
    get_mode.restype = ct.c_uint
    before = get_mode()
    code = (
        "import ctypes; from famepy._probe import suppress_error_dialogs; "
        "suppress_error_dialogs(); "
        "print(ctypes.WinDLL('kernel32').GetErrorMode() & 0x8003)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0
    assert int(result.stdout) == 0x8003
    assert get_mode() == before
