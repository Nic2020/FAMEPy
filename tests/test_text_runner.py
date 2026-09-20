# SPDX-License-Identifier: MIT
"""The ``text`` validation group: fake-backed run, exports, and the reference protocol."""

import json
import os
import sys
from pathlib import Path

import pytest
from canonical import write
from fake_native import make_fake

import famepy
from famepy import validation
from famepy.validation import _groups, _report, _text_group
from famepy.validation._process import WorkerResult

TESTS = Path(__file__).resolve().parent


def _environment():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(TESTS) + os.pathsep + environment.get("PYTHONPATH", "")
    environment.pop("FAME", None)
    return environment


@pytest.fixture
def child_env(monkeypatch):
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)


def _options(tmp_path, backend, groups, **extra):
    options = {
        "scratch": str(tmp_path / "s"),
        "native": True,
        "timeout": 60.0,
        "backend": f"fake_native:{backend}",
        "groups": groups,
    }
    options.update(extra)
    return options


# -- the corpus and the script ----------------------------------------------------


def test_corpus_is_fixed_and_independently_listed():
    labels = [f.label for f in _text_group.TEXT_CORPUS]
    assert len(labels) == len(set(labels)) == 9
    assert "ascii" in _text_group.SUPPORTED_LABELS
    assert "internal_multibyte" in _text_group.SUPPORTED_LABELS
    assert "terminal_multibyte" not in _text_group.SUPPORTED_LABELS
    for fixture in _text_group.TEXT_CORPUS:
        assert fixture.reference in ("supported", "limitation", "observed", "python")
        expected = _text_group.expected_bytes(fixture)
        if fixture.is_vector:
            assert [None if v is None else v.encode() for v in fixture.value] == expected
        else:
            assert fixture.value.encode() == expected
            assert len(expected) <= _report.MAX_HEX_BYTES
        assert fixture.name.isascii() and fixture.name.startswith("TXT_")
    # A value whose last character is multibyte is a reference limitation,
    # never a required reference read; every label is required on the
    # Python side.
    for fixture in _text_group.TEXT_CORPUS:
        if not fixture.is_vector and not fixture.value[-1].isascii():
            assert fixture.reference == "limitation"
        assert f"utf8:{fixture.label}" in _text_group.TEXT_REQUIRED
        assert f"raw:{fixture.label}" in _text_group.TEXT_REQUIRED
    assert "julia_text" not in _text_group.TEXT_REQUIRED
    assert set(_text_group.TEXT_REQUIRED).isdisjoint(_text_group.TEXT_JULIA_REQUIRED)
    assert "julia_text_tree_pinned" not in _text_group.TEXT_JULIA_REQUIRED
    for label in _text_group.REFERENCE_LABELS:
        assert f"python_reads_julia_raw:{label}" in _text_group.TEXT_JULIA_REQUIRED
        assert f"julia_write:{label}" in _text_group.TEXT_JULIA_REQUIRED
    assert "julia_read:terminal_multibyte" not in _text_group.TEXT_JULIA_REQUIRED
    assert "julia_read:internal_multibyte" in _text_group.TEXT_JULIA_REQUIRED


def test_script_is_ascii_and_carries_the_corpus_as_escapes():
    script = _text_group.TEXT_SCRIPT
    assert script.isascii()
    assert '"TXT_INTERNAL_MULTIBYTE", "caf\\U000000e9 au lait"' in script
    assert "TXT_VECTOR_MISSING" not in script  # Python-only label
    assert "readfame" not in script and "writefame" not in script
    assert "do_write" in script and "do_read!" in script and "quick_info" in script
    assert "StringIndexError" not in script  # the class name is reported, not matched
    assert "\\" not in _text_group._julia_literal("plain")
    assert _text_group._julia_literal('a"b$c\\d') == '"a\\"b\\$c\\\\d"'
    assert "text" in validation.GROUPS and validation.REQUIRED_CASES["text"]


# -- the group with the fake backend ------------------------------------------------


def test_text_group_passes_with_the_fake(tmp_path, child_env):
    report = validation.run(_options(tmp_path, "make_validation_backend", ["lifecycle", "text"]))
    assert report["result"] == "PASS", json.dumps(report["groups"]["text"], indent=1)[:4000]
    record = report["groups"]["text"]
    cases = {case["id"]: case for case in record["cases"]}
    assert set(validation.REQUIRED_CASES["text"]) <= set(cases)
    assert record["counts"]["fail"] == 0 and record["counts"]["blocked"] == 0
    assert cases["julia_text"]["status"] == "unsupported"
    assert "julia_required" not in record
    assert cases["raw:terminal_multibyte"]["expected"] == {"hex": "636166c3a9"}
    assert cases["ascii:terminal_multibyte"]["error_type"] == "TextEncodingError"
    assert cases["utf8:multibyte_only"]["actual"] is True
    assert cases["cross_process:values:TXT_VECTOR_MISSING"]["status"] == "pass"
    assert cases["refuse_invalid_policy_no_file"]["actual"] is False
    assert cases["missing_before_decode"]["actual"] == [[0, 1, 0], True]
    text = json.dumps(report)
    assert str(tmp_path) not in text and "Traceback" not in text
    # Only ASCII leaves the child: corpus text never appears decoded.
    assert text.isascii() and "caf" not in text.replace("cafe", "")


def test_configured_julia_makes_the_text_cases_required(tmp_path, monkeypatch):
    base = [{"id": case_id, "status": "pass"} for case_id in validation.REQUIRED_CASES["text"]]
    payloads = iter(
        [
            {"group": "text", "cases": base, "counts": {}},
            {"group": "text", "cases": base, "counts": {}},
            {
                "group": "text",
                "cases": [
                    *base,
                    *[{"id": c, "status": "pass"} for c in _text_group.TEXT_JULIA_REQUIRED],
                    {"id": "julia_text_tree_pinned", "status": "unsupported"},
                    {
                        "id": "julia_read:terminal_multibyte",
                        "status": "pass",
                        "observation": True,
                        "actual": ["StringIndexError", None, None, None],
                    },
                ],
                "counts": {},
            },
        ]
    )

    def launch(command, config, timeout, *, tokens, nested=False):
        return WorkerResult(0, next(payloads), None, tokens=tokens)

    monkeypatch.setattr(validation, "launch_worker", launch)
    options = _options(tmp_path, "make_validation_backend", ["text"])
    assert validation.run(options)["groups"]["text"]["status"] == "pass"
    julia = {"executable": "julia", "project": "project"}
    record = validation.run({**options, "scratch": str(tmp_path / "s2"), "julia": julia})
    record = record["groups"]["text"]
    assert record["status"] == "fail" and record["julia_required"] is True
    assert "julia_reads_python:internal_multibyte" in record["required_missing"]
    assert "python_reads_julia_raw:supplementary_terminal" in record["required_missing"]
    record = validation.run({**options, "scratch": str(tmp_path / "s3"), "julia": julia})
    assert record["groups"]["text"]["status"] == "pass"


# -- the reference protocol with a stand-in -----------------------------------------

STAND_IN = r"""
import json, os, sys
sys.path.insert(0, os.environ["FAMEPY_TESTS"])
from canonical import value, write
from fake_native import make_fake
import famepy
from famepy.validation._text_group import TEXT_CORPUS, write_form
script, python_path, julia_path, result_path, token = sys.argv[1:6]
mode = os.environ.get("STAND_IN_MODE", "faithful")
print("stray text {not json}")
session = famepy.Session(native=make_fake(persist=True)).initialize()
result = {"group": "text", "token": token, "complete": True, "fame_tree_hash": "b" * 40}
def slice_like_the_reference(value):
    # The wrapper slices its buffer by the byte length on a character index,
    # which fails exactly when the last character is multibyte.
    data = value.encode()
    if data and data[-1] >= 0x80:
        raise IndexError
    return value
with famepy.opendb(julia_path, "create", session=session) as db:
    for fixture in TEXT_CORPUS:
        if fixture.reference == "python":
            continue
        item = write_form(fixture)
        if mode == "corrupt" and fixture.label == "ascii":
            item = "Hello, FAME 2027"
        write(db, fixture.name, item, text="utf-8")
        result["write:" + fixture.label] = "ok"
    famepy.postdb(db)
for prefix, path in (("read", julia_path), ("python", python_path)):
    with famepy.opendb(path, session=session) as db:
        for fixture in TEXT_CORPUS:
            if fixture.reference == "python":
                continue
            label = fixture.label
            try:
                got = value(db, fixture.name, text="utf-8")
                got = list(got.values) if fixture.is_vector else got
                items = got if fixture.is_vector else [got]
                for item in items:
                    slice_like_the_reference(item)
            except IndexError:
                result[prefix + ":" + label] = "StringIndexError"
                continue
            result[prefix + ":" + label] = "ok"
            result[prefix + "_valid:" + label] = True
            result[prefix + "_units:" + label] = sum(len(i.encode()) for i in items)
            result[prefix + "_equal:" + label] = got == fixture.value
session.finalize()
if mode == "wrong_group":
    result["group"] = "julia"
part = result_path + ".part"
with open(part, "w", encoding="ascii") as stream:
    json.dump(result, stream)
os.replace(part, result_path)
if mode == "nonzero":
    sys.exit(7)
"""


def _text_context(tmp_path, monkeypatch, mode="faithful"):
    stand_in = tmp_path / "text_stand_in.py"
    stand_in.write_text(STAND_IN, encoding="ascii")
    monkeypatch.setenv("FAMEPY_TESTS", str(TESTS))
    monkeypatch.setenv("STAND_IN_MODE", mode)
    fake = make_fake(persist=True)
    session = famepy.Session(native=fake).initialize()
    python_path = tmp_path / "text.db"
    with famepy.opendb(python_path, "create", session=session) as db:
        for fixture in _text_group.TEXT_CORPUS:
            write(db, fixture.name, _text_group.write_form(fixture), text="utf-8")
        famepy.postdb(db)
    recorder = _report.Recorder()
    julia = {"executable": sys.executable, "project": str(stand_in)}
    ctx = _groups.Context(session, tmp_path, recorder, lambda e: [], 60.0, julia, {})
    original = _text_group.run_child

    def run(command, input_text, timeout, **kwargs):
        script_index = command.index("--startup-file=no") + 1
        stand_in = command[1].removeprefix("--project=")
        return original(
            [sys.executable, stand_in, *command[script_index:]], input_text, timeout, **kwargs
        )

    monkeypatch.setattr(_text_group, "run_child", run)
    return session, recorder, ctx, python_path


def test_reference_protocol_with_a_faithful_stand_in(tmp_path, monkeypatch):
    session, recorder, ctx, python_path = _text_context(tmp_path, monkeypatch)
    _text_group.run_julia_text(ctx, python_path)
    session.finalize()
    cases = {case.id: case for case in recorder.cases}
    statuses = {case_id: case.status for case_id, case in cases.items()}
    assert statuses["julia_text_result"] == "pass"
    assert statuses["julia_text_tree_pinned"] == "unsupported"
    for case_id in _text_group.TEXT_JULIA_REQUIRED:
        assert statuses[case_id] == "pass", case_id
    # The reference limitation is an observation with the class name, never
    # a failure and never a required case.
    limitation = cases["julia_read:terminal_multibyte"]
    assert limitation.observation and limitation.actual[0] == "StringIndexError"
    assert "reference limitation" in limitation.note
    observed = cases["julia_read:supplementary_internal"]
    assert observed.observation and observed.actual[0] == "ok"
    assert cases["julia_read_equal:internal_multibyte"].actual == [True, 13, True]
    assert "unpinned" in cases["python_reads_julia_raw:ascii"].note
    assert cases["python_reads_julia_raw:vector_mixed"].expected == [
        {"ascii": "alpha"},
        {"hex": "636166c3a9"},
    ]
    exported = json.dumps([case.to_json() for case in recorder.cases])
    assert exported.isascii() and str(tmp_path) not in exported


@pytest.mark.parametrize(
    "mode,failing,expected_status",
    [
        ("corrupt", "python_reads_julia_raw:ascii", "fail"),
        ("nonzero", "julia_text_result", "fail"),
        ("wrong_group", "julia_text_result", "fail"),
    ],
)
def test_reference_protocol_rejects_bad_runs(tmp_path, monkeypatch, mode, failing, expected_status):
    session, recorder, ctx, python_path = _text_context(tmp_path, monkeypatch, mode)
    _text_group.run_julia_text(ctx, python_path)
    session.finalize()
    statuses = {case.id: case.status for case in recorder.cases}
    assert statuses[failing] == expected_status
    if mode == "corrupt":
        assert statuses["python_reads_julia_utf8:ascii"] == "fail"
        assert statuses["python_reads_julia_raw:internal_multibyte"] == "pass"
        recorded = next(c for c in recorder.cases if c.id == "python_reads_julia_raw:ascii")
        # What the stand-in stored differs from the fixture: only its length leaves.
        assert recorded.actual == {"length": 16, "unexpected_bytes": True}
    else:
        assert not any(case_id.startswith("python_reads_julia") for case_id in statuses)


def test_adopt_rejects_malformed_outcomes(tmp_path):
    fake = make_fake(persist=True)
    session = famepy.Session(native=fake).initialize()
    julia_path = tmp_path / "julia_text.db"
    with famepy.opendb(julia_path, "create", session=session) as db:
        for fixture in _text_group.TEXT_CORPUS:
            write(db, fixture.name, _text_group.write_form(fixture), text="utf-8")
        famepy.postdb(db)
    recorder = _report.Recorder()
    ctx = _groups.Context(session, tmp_path, recorder, lambda e: [], 60.0, None, {})
    payload = {
        "write:ascii": "ok; rm -rf",  # not an identifier
        "read:ascii": "ok",
        "read_valid:ascii": "true",  # not a boolean
        "read_units:ascii": 16.0,  # not an integer
        "read_equal:ascii": True,
        "python:ascii": "ok",
        "python_valid:ascii": True,
        "python_units:ascii": 16,
        "python_equal:ascii": True,
    }
    _text_group.adopt_julia_text(ctx, payload, julia_path, None)
    session.finalize()
    cases = {case.id: case for case in recorder.cases}
    assert cases["julia_write:ascii"].status == "fail" and cases["julia_write:ascii"].actual is None
    assert cases["julia_read:ascii"].status == "pass"
    assert cases["julia_read_equal:ascii"].status == "fail"
    assert cases["julia_read_equal:ascii"].actual == [None, None, True]
    assert cases["julia_reads_python_equal:ascii"].status == "pass"
    assert cases["julia_write:internal_multibyte"].status == "fail"
    assert cases["python_reads_julia_raw:internal_multibyte"].status == "pass"
    exported = json.dumps([case.to_json() for case in recorder.cases])
    assert "rm -rf" not in exported


def test_text_group_catches_a_truncating_library(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_text_truncating_backend", ["lifecycle", "text"])
    )
    assert report["result"] == "FAIL"
    record = report["groups"]["text"]
    cases = {case["id"]: case for case in record["cases"]}
    failing = {case_id for case_id, case in cases.items() if case["status"] == "fail"}
    for label in ("terminal_multibyte", "multibyte_only", "supplementary_terminal", "vector_mixed"):
        assert {f"raw:{label}", f"utf8:{label}", f"bytes:{label}"} <= failing, label
        assert f"cross_process:values:TXT_{label.upper()}" in failing
    for label in ("ascii", "internal_multibyte", "supplementary_internal", "vector_ascii"):
        assert cases[f"raw:{label}"]["status"] == "pass", label
        assert cases[f"utf8:{label}"]["status"] == "pass", label
    # The corrupted bytes never leave the child: only their length is reported.
    assert cases["raw:terminal_multibyte"]["actual"] == {"length": 4, "unexpected_bytes": True}
    assert cases["corpus_bytes_agree"]["status"] == "pass"
    assert set(record["required_not_passed"]) <= failing


def test_absence_check_does_not_accept_unrelated_native_error(tmp_path, monkeypatch):
    session = famepy.Session(native=make_fake(persist=True))
    recorder = _report.Recorder()

    def unexpected_child(*args, **kwargs):
        pytest.fail("This in-process assertion must not launch a verification child.")

    ctx = _groups.Context(session, tmp_path, recorder, unexpected_child, 60.0, None, {})
    verification_requests = []

    def record_verification(case_id, manifest):
        verification_requests.append((case_id, manifest))

    # Cross-process verification has separate end-to-end tests. Isolate this
    # assertion explicitly instead of relying on how an empty command fails.
    monkeypatch.setattr(ctx, "verify_in_new_process", record_verification)
    monkeypatch.setattr(_groups, "launch_worker", unexpected_child)
    original = famepy.quick_info

    def failing_lookup(database, name):
        if name in ("REFUSED_A", "REFUSED_B", "REFUSED_C"):
            raise famepy.HLIError(97, operation="quick_info")
        return original(database, name)

    monkeypatch.setattr(famepy, "quick_info", failing_lookup)
    _text_group.group_text(ctx)
    case = next(c for c in recorder.cases if c.id == "refusals_left_no_object")
    assert case.status == "fail"
    assert case.error_type == "HLIError"
    assert [c for c in recorder.cases if c.status == "fail"] == [case]
    assert len(verification_requests) == 1
    assert verification_requests[0][0] == "cross_process"
    assert len(verification_requests[0][1]["objects"]) == len(_text_group.TEXT_CORPUS)
