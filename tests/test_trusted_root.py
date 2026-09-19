# SPDX-License-Identifier: MIT
"""The trusted installation root reaches the probe child and the runner preflight."""

import ctypes as ct
import io
import json
import subprocess
import sys

import pytest

import famepy
from famepy import _probe, _runtime, diagnostics, validation


def _install(tmp_path, system=None):
    root = tmp_path / "installation"
    system = sys.platform if system is None else system
    relative = "64/chli.dll" if system == "win32" else "hli/64/libchli.so"
    library = root / relative
    library.parent.mkdir(parents=True)
    library.touch()
    return root, library


def _symbols():
    return json.dumps(
        {
            "functions": dict.fromkeys(famepy._abi.SIGNATURES, True),
            "globals": dict.fromkeys(famepy._abi.GLOBALS, True),
            "presence_only": {},
        }
    )


@pytest.mark.parametrize("system", ["win32", "linux"])
def test_diagnose_forwards_the_trusted_root_to_the_probe(tmp_path, monkeypatch, system):
    root, library = _install(tmp_path, system)
    monkeypatch.setattr(diagnostics.sys, "platform", system)
    requests = []

    def fake_run(command, **kwargs):
        requests.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, _symbols(), "")

    monkeypatch.setattr(diagnostics.subprocess, "run", fake_run)
    monkeypatch.setenv("FAME", str(root))
    monkeypatch.delenv("FAMEPY_LIBRARY", raising=False)
    report = famepy.diagnose(probe=True)
    assert report["status"] == "symbols_found" and report["trusted_root_known"] is True
    assert requests[-1] == {"library": str(library), "root": str(root)}
    monkeypatch.delenv("FAME")
    report = famepy.diagnose(library, root=root, probe=True)
    assert report["trusted_root_known"] is True and requests[-1]["root"] == str(root)
    report = famepy.diagnose(library, probe=True)
    assert report["trusted_root_known"] is False and requests[-1]["root"] is None
    assert str(root) not in json.dumps(report)


def test_probe_child_registers_root_and_library_directories(tmp_path, monkeypatch):
    root, library = _install(tmp_path)
    added = []

    class Handle:
        def close(self):
            pass

    monkeypatch.setattr(_runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        _runtime.os, "add_dll_directory", lambda d: added.append(d) or Handle(), raising=False
    )
    monkeypatch.setattr(ct, "CDLL", lambda p: object())
    monkeypatch.setattr(_probe, "GLOBALS", ())
    result = _probe.probe_library(str(library), str(root))
    assert set(result) == {"functions", "globals", "presence_only"}
    assert added == [str(library.parent), str(root)]
    added.clear()
    _probe.probe_library(str(library))
    assert added == [str(library.parent)]


def test_probe_child_rejects_a_non_string_root(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"library": "x", "root": 5})))
    assert _probe.main() == 21


def test_validation_preflight_passes_library_and_root(tmp_path, monkeypatch):
    root, library = _install(tmp_path)
    seen = {}

    def fake_diagnose(library_argument, *, root=None, probe, timeout):
        seen.update(library=library_argument, root=root, probe=probe)
        return {"status": "library_unavailable"}

    monkeypatch.setattr(famepy, "diagnose", fake_diagnose)
    validation.preflight(
        {"scratch": str(tmp_path / "s"), "library": str(library), "root": str(root)}
    )
    assert seen == {"library": str(library), "root": str(root), "probe": True}
