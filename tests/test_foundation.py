# SPDX-License-Identifier: MIT
import ctypes as ct
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import famepy
from famepy import FameError, LibraryNotFoundError, check_status
from famepy._binding import Binding
from famepy._discovery import discover
from famepy._errors import UnsupportedPlatformError
from famepy._runtime import Runtime


@pytest.mark.parametrize("status", [13, 67, 513, -1, 2**31 - 1])
def test_error_status_retained_without_payload(status):
    with pytest.raises(FameError) as error:
        check_status(status)
    assert error.value.status == status
    assert str(status) in str(error.value)


def test_status_validation():
    assert check_status(0) is None
    for value in [True, False, 1.5, "0", None]:
        with pytest.raises(TypeError):
            check_status(value)
    for value in [2**31, -(2**31) - 1]:
        with pytest.raises(OverflowError):
            check_status(value)


@pytest.mark.parametrize(
    "system,suffix",
    [
        ("win32", "64/chli.dll"),
        ("linux", "hli/64/libchli.so"),
    ],
)
def test_discovery_precedence_and_redaction(tmp_path, system, suffix):
    root = tmp_path / "private-installation"
    path = root / suffix
    path.parent.mkdir(parents=True)
    path.touch()
    options = dict(system=system, machine="x86_64", pointer_bits=64)
    candidate = discover(environ={"FAME": str(root)}, **options)
    assert candidate.path == path
    assert str(root) not in repr(candidate)
    assert discover(path, environ={"FAMEPY_LIBRARY": "missing"}, **options).source == "argument"
    assert discover(environ={"FAMEPY_LIBRARY": str(path)}, **options).path == path
    with pytest.raises(LibraryNotFoundError):
        discover(tmp_path / "absent", environ={"FAME": str(root)}, **options)
    with pytest.raises(LibraryNotFoundError):
        discover(environ={"FAME": str(root), "FAMEPY_LIBRARY": ""}, **options)


@pytest.mark.parametrize("value", ["", "relative.dll", "secret-directory/missing.dll"])
def test_discovery_errors_do_not_echo_input(value):
    with pytest.raises(LibraryNotFoundError) as error:
        discover(value, system="linux", machine="x86_64", pointer_bits=64)
    assert not value or value not in str(error.value)


@pytest.mark.parametrize(
    "system,machine,bits",
    [
        ("darwin", "x86_64", 64),
        ("linux", "aarch64", 64),
        ("win32", "AMD64", 32),
    ],
)
def test_unsupported_hosts(system, machine, bits):
    with pytest.raises(UnsupportedPlatformError):
        discover(environ={}, system=system, machine=machine, pointer_bits=bits)


def test_import_does_not_load_chli():
    command = (
        "import ctypes; ctypes.CDLL=lambda *a,**k: 1/0; import famepy; print(famepy.__version__)"
    )
    result = subprocess.run([sys.executable, "-c", command], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == famepy.__version__


def test_runtime_lazy_cached_and_failure_redacted(tmp_path, monkeypatch):
    path = tmp_path / "private-native.dll"
    path.touch()
    candidate = discover(path)
    owner = Runtime(candidate)
    calls = []
    sentinel = object()
    monkeypatch.setattr(ct, "CDLL", lambda path: calls.append(path) or sentinel)
    assert calls == []
    assert owner.load() is sentinel
    assert owner.load() is sentinel
    assert len(calls) == 1
    monkeypatch.setattr(ct, "CDLL", lambda _: (_ for _ in ()).throw(OSError("secret-loader")))
    with pytest.raises(LibraryNotFoundError) as error:
        Runtime(candidate).load()
    assert "secret-loader" not in str(error.value)
    assert error.value.__suppress_context__


def test_runtime_rejects_inherited_owner(tmp_path, monkeypatch):
    path = tmp_path / "test.dll"
    path.touch()
    owner = Runtime(discover(path))
    monkeypatch.setattr("famepy._runtime.os.getpid", lambda: -1)
    with pytest.raises(RuntimeError, match="after fork"):
        owner.load()


def test_fake_backend_status_and_signature():
    class Function:
        def __call__(self, status, key, name, mode):
            ct.cast(status, ct.POINTER(ct.c_int32))[0] = 13

    class Backend:
        cfmopdb = Function()

    with pytest.raises(FameError, match="13"):
        Binding(Backend()).call("cfmopdb", ct.byref(ct.c_int32()), b"synthetic", 1)
    assert Backend.cfmopdb.restype is None
    assert len(Backend.cfmopdb.argtypes) == 4


def test_diagnose_absent_and_cli(monkeypatch, tmp_path):
    monkeypatch.delenv("FAME", raising=False)
    monkeypatch.delenv("FAMEPY_LIBRARY", raising=False)
    report = famepy.diagnose()
    assert report["status"] == "library_unavailable"
    assert report["abi_verified"] is False
    result = subprocess.run(
        [sys.executable, "-m", "famepy"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "library_unavailable"


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf")])
def test_diagnostic_timeout_validation(timeout):
    with pytest.raises(ValueError):
        famepy.diagnose(timeout=timeout)


def test_diagnose_no_load_and_probe_failure_redaction(tmp_path, monkeypatch):
    path = tmp_path / "secret-path.dll"
    path.touch()
    module = importlib.import_module("famepy.diagnostics")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected load")),
    )
    report = famepy.diagnose(path)
    assert report["status"] == "library_found"
    assert str(path) not in json.dumps(report)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, "secret-output", "private-loader-text"),
    )
    report = famepy.diagnose(path, probe=True)
    assert report["status"] == "probe_failed"
    assert "secret" not in json.dumps(report)
    assert "private" not in json.dumps(report)


def test_probe_timeout_and_malformed_output(tmp_path, monkeypatch):
    path = tmp_path / "test.dll"
    path.touch()
    module = importlib.import_module("famepy.diagnostics")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private-command", 1, output="private-output")

    monkeypatch.setattr(module.subprocess, "run", timeout)
    assert famepy.diagnose(path, probe=True)["status"] == "probe_timeout"
    for payload in ["private-output", "null", "[]", "{}", '{"functions":null}']:
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *a, payload=payload, **k: subprocess.CompletedProcess(
                a, 0, payload, "private-stderr"
            ),
        )
        report = famepy.diagnose(path, probe=True)
        assert report["status"] == "probe_invalid_output"
        assert "private" not in json.dumps(report)


def test_real_subprocess_rejects_nonlibrary_without_paths(tmp_path):
    path = tmp_path / "private-file.dll"
    path.write_text("not native code")
    report = famepy.diagnose(path, probe=True)
    assert report["status"] == "probe_failed"
    assert str(tmp_path) not in json.dumps(report)


def test_declared_dependency_is_importable():
    import tsecon

    assert tsecon.Workspace is not None


def test_package_location_is_installed():
    assert Path(famepy.__file__).is_file()
    expected = os.environ.get("FAMEPY_EXPECTED_ROOT")
    if expected is not None:
        assert Path(famepy.__file__).resolve().is_relative_to(Path(expected).resolve())
