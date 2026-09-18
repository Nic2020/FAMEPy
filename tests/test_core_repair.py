# SPDX-License-Identifier: MIT
"""Regressions for the second native campaign: binary evidence, contained
fixture failures, endpoint observations, mode fixtures, frequency filtering,
command stages and the result-file transport of the validation runner."""

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from fake_native import (
    FINITE_SENTINELS,
    PRIVATE_MARKER,
    SENTINELS,
    make_fake,
)

import famepy
from famepy import validation
from famepy._wildcard import native_listing_count
from famepy.validation import _groups, _report
from famepy.validation._process import (
    launch_worker,
    read_result,
    redirect_streams,
    reserve_result,
    write_result,
)

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


def _cases(record):
    return {case["id"]: case for case in record["cases"]}


# -- 1. binary-safe evidence ---------------------------------------------------


def test_string_sentinels_are_binary_and_survive_every_route(tmp_path):
    """Non-ASCII sentinel bytes and empty bytes round-trip exactly, in and across processes."""
    assert not SENTINELS.string_nc.isascii() and len(SENTINELS.string_nc) == 2
    assert not FINITE_SENTINELS.string_nc.isascii()
    fake = make_fake(persist=True)
    session = famepy.Session(native=fake).initialize()
    path = tmp_path / "bin.db"
    values = [b"", SENTINELS.string_nc, SENTINELS.string_na, SENTINELS.string_nd, b"\xff\x00"[:1]]
    with famepy.open_database(path, "create", session=session) as database:
        famepy.write_object(database, "s", famepy.series("string", "case", 1, values))
        famepy.write_object(database, "sc", famepy.scalar("string", SENTINELS.string_nd))
        database.post()
    with famepy.open_database(path, session=session) as database:
        got = famepy.read_object(database, "s")
        assert got.values == values
        assert famepy.classify_by_sentinel(got.values, "string", SENTINELS).tolist() == [
            0,
            1,
            2,
            3,
            0,
        ]
        assert famepy.read_object(database, "sc").value == SENTINELS.string_nd
    manifest = {
        "database": str(path),
        "objects": [
            _groups.manifest_object(
                "s",
                "string",
                values,
                class_name="series",
                type_code=4,
                frequency=232,
                first_index=1,
            ),
            _groups.manifest_object(
                "sc", "string", [SENTINELS.string_nd], class_name="scalar", type_code=4
            ),
        ],
    }
    text = json.dumps(manifest)
    text.encode("ascii")  # the manifest is pure ASCII: bytes travel as hex
    assert "fe01" in text and '"values": ["", "fe01"' in text
    recorder = _report.Recorder()
    _groups.run_verify(session, json.loads(text), recorder)
    assert {case.id: case.status for case in recorder.cases} == {
        "reopen:s": "pass",
        "meta:s": "pass",
        "values:s": "pass",
        "reopen:sc": "pass",
        "meta:sc": "pass",
        "values:sc": "pass",
    }
    recorded = json.dumps([case.to_json() for case in recorder.cases])
    assert '{"hex": "fe01"}' in recorded and '{"ascii": ""}' in recorded
    session.finalize()


def test_manifest_rejects_malformed_encodings():
    with pytest.raises(ValueError):
        _groups._decode_manifest("string", ["zz"])
    with pytest.raises(ValueError):
        _groups._decode_manifest("precision", ["0000"])  # too short for a float64
    assert _groups._decode_manifest("string", ["", "fe01"]) == [b"", b"\xfe\x01"]


@pytest.mark.parametrize(
    "value,ok",
    [
        ({"hex": "fe01"}, True),
        ({"hex": ""}, True),
        ({"hex": "f"}, False),
        ({"hex": "zz"}, False),
        ({"hex": "ab" * 65}, False),
        ({"hex": "AB"}, False),
        ({"length": 70, "sha256": "0" * 64}, True),
        ({"length": -1, "sha256": "0" * 64}, False),
        ({"length": 70, "sha256": "not a digest"}, False),
        ({"ascii": "/private/path"}, False),
        ({"ascii": "C:\\private"}, False),
        ({"length": 30, "unexpected_bytes": True}, True),
        ({"length": 30, "unexpected_bytes": False}, False),
        ({"length": "30", "unexpected_bytes": True}, False),
    ],
)
def test_report_schema_bounds_binary_values(value, ok):
    """Binary evidence is bounded hex or a digest; text stays restricted."""
    sanitized = validation.sanitize_case({"id": "x", "status": "pass", "actual": value})
    assert (sanitized.get("note") != "malformed case record") is ok


UNEXPECTED_BYTES = (
    b"C:/synthetic/private/report.db",
    b"/synthetic/private/.ssh/id",
    b"password=synthetic-secret",
    b"token=AKIAEXAMPLE",
    b"[2026-01-01 00:00:00] synthetic log line",
    b"ordinary text that fits the character set",
    b"a\nb",
    b"\x00",
    b"\xfe\x01",
)


@pytest.mark.parametrize("payload", UNEXPECTED_BYTES)
def test_unexpected_bytes_are_reduced_to_a_length_everywhere(payload):
    """Bytes the runner did not construct are never recoverable from a report,
    whether they arrive as a value, nested in a list or dict, as an actual of
    a failed comparison, or as a fact."""
    reduced = {"length": len(payload), "unexpected_bytes": True}
    assert _report.encode_value(payload) == reduced
    assert _report.encode_value([payload, [payload]]) == [reduced, [reduced]]
    assert _report.encode_value({"k": payload}) == {"k": reduced}
    recorder = _report.Recorder()
    recorder.equal("cmp", payload, b"expected fixture")
    recorder.equal("nested", [1, payload], [1, b"fixture"])
    recorder.fact("obs", payload)
    text = json.dumps([case.to_json() for case in recorder.cases])
    assert payload.hex() not in text
    for token in ("synthetic", "password", "AKIA", "ordinary", "log line"):
        assert token not in text, token
    # The parent accepts the reduced form and nothing reversible.
    sanitized = validation.sanitize_case({"id": "x", "status": "fail", "actual": reduced})
    assert sanitized["actual"] == reduced


def test_permitted_bytes_are_exact_and_provenance_bound():
    """Expected values and registered sentinels are exportable; nothing else."""
    recorder = _report.Recorder()
    sentinel = b"\xfe\x01"
    recorder.fact("before", sentinel)
    assert recorder.cases[-1].actual == {"length": 2, "unexpected_bytes": True}
    recorder.permit(sentinel)
    recorder.fact("after", sentinel)
    assert recorder.cases[-1].actual == {"hex": "fe01"}
    # An expected fixture value makes an equal actual exportable, and only that.
    recorder.equal("same", [b"alpha", b"", b"\xfe" * 70], [b"alpha", b"", b"\xfe" * 70])
    case = recorder.cases[-1]
    assert case.actual[0] == {"ascii": "alpha"} and case.actual[1] == {"ascii": ""}
    assert set(case.actual[2]) == {"length", "sha256"}
    recorder.equal("differs", b"alphA", b"alpha")
    case = recorder.cases[-1]
    assert case.status == "fail"
    assert case.expected == {"ascii": "alpha"}
    assert case.actual == {"length": 5, "unexpected_bytes": True}
    # A permitted value stays permitted for later facts (runner-owned).
    recorder.fact("later", b"alpha")
    assert recorder.cases[-1].actual == {"ascii": "alpha"}


# -- 2/3. contained fixture failures and endpoint observations ---------------------


def test_fixtures_are_built_before_any_native_call():
    fake = make_fake(persist=False)
    session = famepy.Session(native=fake).initialize()
    fake.fake.calls.clear()
    fixtures = _groups.build_matrix_fixtures(session.sentinels, 24240)
    assert fake.fake.calls == []
    names = [fixture.name for fixture in fixtures]
    assert names == _groups.matrix_object_names()
    assert len(set(names)) == len(names)
    endpoints = [fixture for fixture in fixtures if fixture.endpoint is not None]
    assert len(endpoints) == 35
    # Interior cases keep normal values at both ends for every type.
    for fixture in fixtures:
        if fixture.name in ("p_series", "n_series", "b_series", "d_series", "s_missing_series"):
            codes = famepy.classify_by_sentinel(fixture.values, fixture.kind, session.sentinels)
            assert codes[0] == 0 and codes[-1] == 0
            assert set(codes[1:-1].tolist()) >= {1, 2, 3}
    assert all(f"write:{name}" in _groups.RAW_MATRIX_REQUIRED for name in names)
    session.finalize()


def test_one_failing_object_does_not_abort_the_matrix(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_partial_write_backend", ["lifecycle", "raw_matrix"])
    )
    assert report["result"] == "FAIL"
    record = report["groups"]["raw_matrix"]
    cases = _cases(record)
    assert cases["write:s_missing_series"]["status"] == "fail"
    assert cases["write:s_missing_series"]["status_code"] == 912
    # Every other object was still written, read back and verified across processes.
    for name in _groups.matrix_object_names():
        if name == "s_missing_series":
            continue
        assert cases[f"write:{name}"]["status"] == "pass", name
        assert cases[f"cross_process_matrix:values:{name}"]["status"] == "pass", name
    assert cases["post_matrix"]["status"] == "pass"
    # Dependents of the missing object are explicitly blocked, never silently absent.
    for case_id in (
        "read:s_missing_series",
        "values:s_missing_series",
        "kind:s_missing_series",
        "classifier_agreement:s_missing_series",
        "cross_process_matrix:reopen:s_missing_series",
    ):
        assert cases[case_id]["status"] == "blocked", case_id
        assert cases[case_id]["note"] == "prerequisite case did not pass"
    assert "write:s_missing_series" in record["required_not_passed"]
    assert "read:s_missing_series" in record["required_not_passed"]
    assert not record.get("required_missing")
    # Replacement and deletion use their own fixtures and pass regardless.
    for case_id in ("replace_existing", "replace_value", "delete_object", "deleted_object_absent"):
        assert cases[case_id]["status"] == "pass", case_id


def test_endpoint_rules_are_observed_not_assumed(tmp_path, child_env):
    """Exact storage and an ND-trimming rule both pass; only observations differ."""
    exact = validation.run(_options(tmp_path / "a", "make_validation_backend", ["raw_matrix"]))
    trimmed = validation.run(_options(tmp_path / "b", "make_nd_trimming_backend", ["raw_matrix"]))
    assert exact["result"] == "PASS", json.dumps(exact["groups"], indent=1)[:3000]
    assert trimmed["result"] == "PASS", json.dumps(trimmed["groups"], indent=1)[:3000]
    a, b = _cases(exact["groups"]["raw_matrix"]), _cases(trimmed["groups"]["raw_matrix"])
    for prefix in ("p", "n", "b", "d", "s"):
        assert a[f"endpoint_range:{prefix}_trailing_nd"]["actual"] == [0, 1]
        assert b[f"endpoint_range:{prefix}_trailing_nd"]["actual"] == [0, 0]
        assert b[f"endpoint_range:{prefix}_leading_nd"]["actual"] == [1, 1]
        assert b[f"endpoint_range:{prefix}_all_nd"]["actual"] == "empty"
        assert a[f"endpoint_codes:{prefix}_all_nd"]["actual"] == [3, 3]
        assert b[f"endpoint_codes:{prefix}_all_nd"]["actual"] == []
        # NC and NA endpoints are untouched by the trimming rule.
        assert b[f"endpoint_range:{prefix}_trailing_nc"]["actual"] == [0, 1]
        for shape in ("trailing_nd", "leading_nd", "trailing_nc", "trailing_na"):
            case = b[f"endpoint_interior:{prefix}_{shape}"]
            assert case["status"] == "pass" and "observation" not in case
        assert b[f"endpoint_range:{prefix}_all_nd"]["observation"] is True
        for shape in ("leading_nc", "leading_na"):
            assert b[f"endpoint_range:{prefix}_{shape}"]["actual"] == [0, 1]
            assert b[f"endpoint_retained:{prefix}_{shape}"]["status"] == "pass"
        for shape, _slots in _groups.ENDPOINT_SHAPES:
            retained = b[f"endpoint_retained:{prefix}_{shape}"]
            assert retained["status"] == "pass" and "observation" not in retained
            assert a[f"endpoint_retained:{prefix}_{shape}"]["status"] == "pass"
            assert a[f"cross_process_matrix:values:{prefix}_{shape}"]["status"] == "pass"
            assert b[f"cross_process_matrix:values:{prefix}_{shape}"]["status"] == "pass"
        # Interior missing values with normal endpoints are asserted exactly.
    for name in ("p_series", "n_series", "b_series", "d_series", "s_missing_series"):
        assert b[f"values:{name}"]["status"] == "pass" and "observation" not in b[f"values:{name}"]
        assert b[f"cross_process_matrix:meta:{name}"]["status"] == "pass"


def test_endpoint_corruption_is_detected_not_adopted(tmp_path, child_env):
    """An invented value, a changed missing code or a shifted range fails, and
    the cross-process manifest never carries the corrupted values."""
    report = validation.run(_options(tmp_path, "make_endpoint_corrupting_backend", ["raw_matrix"]))
    assert report["result"] == "FAIL"
    record = report["groups"]["raw_matrix"]
    cases = _cases(record)
    # Invented ordinary value in place of the trailing ND.
    assert cases["endpoint_retained:p_trailing_nd"]["status"] == "fail"
    assert cases["endpoint_codes:p_trailing_nd"]["actual"] == [0, 0]
    assert cases["endpoint_interior:p_trailing_nd"]["status"] == "pass"
    assert cases["endpoint_explicit:p_trailing_nd"]["status"] == "fail"
    # Changed missing code in an all-ND series (no neighbour to protect it).
    assert cases["endpoint_retained:n_all_nd"]["status"] == "fail"
    assert cases["endpoint_codes:n_all_nd"]["actual"] == [2, 3]
    # Range reported outside the written range.
    assert cases["endpoint_retained:d_leading_nc"]["status"] == "fail"
    assert cases["endpoint_retained:d_leading_nc"]["actual"][0] is False
    assert cases["endpoint_range:d_leading_nc"]["actual"] == [1, 2]
    for case_id in (
        "endpoint_retained:p_trailing_nd",
        "endpoint_retained:n_all_nd",
        "endpoint_retained:d_leading_nc",
    ):
        assert case_id in record["required_not_passed"]
    # The manifest is built from the written values, so the verification child
    # compares against the fixture and also fails on the corrupted objects.
    assert cases["cross_process_matrix:values:p_trailing_nd"]["status"] == "fail"
    assert cases["cross_process_matrix:values:n_all_nd"]["status"] == "fail"
    # Untouched objects still pass; the corrupted value is not exported.
    assert cases["endpoint_retained:p_leading_nd"]["status"] == "pass"
    assert cases["values:p_series"]["status"] == "pass"


def test_endpoint_interior_assertion_catches_a_lost_normal_value(tmp_path, child_env):
    """A backend that also drops the normal neighbour of a trailing ND must fail."""
    report = validation.run(_options(tmp_path, "make_value_dropping_backend", ["raw_matrix"]))
    assert report["result"] == "FAIL"
    cases = _cases(report["groups"]["raw_matrix"])
    assert cases["endpoint_interior:p_trailing_nd"]["status"] == "fail"
    assert cases["endpoint_interior:p_trailing_nd"]["expected"] == [True, [1.0]]
    # The retained (empty) range is consistent with the written values, so only
    # the interior assertion catches the lost neighbour.
    assert cases["endpoint_retained:p_trailing_nd"]["status"] == "pass"


def test_a_failure_inside_one_object_does_not_abort_later_objects(tmp_path, child_env):
    """A classifier failure fails its object only; later objects are still verified."""
    report = validation.run(_options(tmp_path, "make_classifier_failing_backend", ["raw_matrix"]))
    assert report["result"] == "FAIL"
    record = report["groups"]["raw_matrix"]
    cases = _cases(record)
    # The third numeric classification happens inside n_series' verification.
    assert cases["verify_object:n_series"]["status"] == "fail"
    assert cases["verify_object:n_series"]["status_code"] == 913
    assert cases["read:n_series"]["status"] == "pass"
    assert cases["values:n_series"]["status"] == "pass"
    assert cases["classifier_agreement:n_series"]["status"] == "blocked"
    assert cases["kind:n_series"]["status"] == "blocked"
    # Everything after it, including an unrelated scalar, was still checked.
    for case_id in (
        "verify_object:b_series",
        "values:b_series",
        "classifier_agreement:b_series",
        "verify_object:p_scalar",
        "values:p_scalar",
        "classifier_agreement:n_scalar",
        "endpoint_retained:s_all_nd",
        "extra_checks",
        "period_round_trip",
        "cross_process_matrix:values:p_scalar",
        "replace_value",
    ):
        assert cases[case_id]["status"] == "pass", case_id
    assert record["required_not_passed"] == [
        "verify_object:n_series",
        "kind:n_series",
        "classifier_agreement:n_series",
    ]


# -- 4. write / direct-write fixtures -----------------------------------------------


def test_mode_fixtures_keep_the_existing_database_and_the_new_path_apart(tmp_path, child_env):
    """The connection modes are checked on both an existing fixture and an absent path."""
    report = validation.run(_options(tmp_path, "make_validation_backend", ["database"]))
    assert report["result"] == "PASS"
    cases = _cases(report["groups"]["database"])
    for mode in ("write", "direct_write"):
        assert cases[f"mode_{mode}_fixture"]["status"] == "pass"
        assert cases[f"mode_{mode}_fixture_unchanged"]["actual"] == ["BASE"]
        assert cases[f"mode_{mode}_new_path_absent"]["actual"] is False
    # The mode contract itself is covered in test_compatibility.


# -- 5. frequency filtering --------------------------------------------------------


@pytest.fixture
def mixed(session, tmp_path):
    database = famepy.open_database(tmp_path / "mixed.db", "create", session=session)
    first = 24240
    famepy.write_object(database, "m_a", famepy.series("precision", "monthly", first, np.zeros(2)))
    famepy.write_object(
        database, "m_b", famepy.series("numeric", "monthly", first, np.zeros(1, np.float32))
    )
    famepy.write_object(
        database, "q_a", famepy.series("precision", "quarterly_december", 100, np.zeros(2))
    )
    famepy.write_object(database, "c_s", famepy.series("string", "case", 1, [b"x"]))
    famepy.write_object(database, "sc", famepy.scalar("precision", 1.0))
    famepy.write_object(database, "st", famepy.scalar("string", b"t"))
    yield database
    database.close()


def _names(database, pattern="?", **filters):
    return sorted(info.name_text for info in famepy.list_objects(database, pattern, **filters))


def test_frequency_filter_returns_exact_sets(mixed):
    assert _names(mixed) == ["C_S", "M_A", "M_B", "Q_A", "SC", "ST"]
    assert _names(mixed, frequencies="monthly") == ["M_A", "M_B"]
    assert _names(mixed, frequencies="quarterly_december") == ["Q_A"]
    assert _names(mixed, frequencies=["monthly", "case"]) == ["C_S", "M_A", "M_B"]
    assert _names(mixed, frequencies="monthly,case") == ["C_S", "M_A", "M_B"]
    assert _names(mixed, frequencies=129) == ["M_A", "M_B"]
    assert _names(mixed, frequencies=[129, "case"]) == ["C_S", "M_A", "M_B"]
    assert _names(mixed, frequencies="undefined") == ["SC", "ST"]
    assert _names(mixed, frequencies="monthly", classes="scalar") == []
    assert _names(mixed, frequencies="monthly", types="numeric") == ["M_B"]
    assert _names(mixed, "m?", frequencies="monthly") == ["M_A", "M_B"]
    assert _names(mixed, frequencies=[]) == ["C_S", "M_A", "M_B", "Q_A", "SC", "ST"]
    assert _names(mixed, frequencies="daily") == []


@pytest.mark.parametrize("value", ["quarterly", "monthly;drop", "fortnightly", 7, True, "annual"])
def test_frequency_filter_refuses_families_and_invalid_input(mixed, value):
    fake = mixed.session._native.fake
    fake.calls.clear()
    with pytest.raises(ValueError):
        famepy.list_objects(mixed, frequencies=value)
    assert fake.calls == []  # refused before any native call


def test_frequency_option_is_still_set_and_normalized(mixed):
    fake = mixed.session._native.fake
    fake.commands.clear()
    famepy.list_objects(mixed, frequencies=["case", "monthly"])
    options = [name for name in fake.calls if name == "cfmsopt"]
    assert len(options) >= 5
    # After the listing every option is back to ON, including the frequency selection.
    assert fake.options[b"ITEM FREQUENCY"] == b"ON"
    assert not any(key.startswith(b"ITEM FREQUENCY ") for key in fake.options)
    # The observation helper applies the family word alone, without the
    # package filter: the fake models it as documented (date-indexed series
    # only), so the case series and the scalars stay listed.
    count = native_listing_count(
        mixed, "?", [(b"ITEM FREQUENCY", b"OFF"), (b"ITEM FREQUENCY MONTHLY", b"ON")]
    )
    assert count == 5
    assert fake.options[b"ITEM FREQUENCY"] == b"ON"


def test_option_errors_are_not_hidden(mixed):
    fake = mixed.session._native.fake
    fake.fail_next["cfmsopt"] = 67
    with pytest.raises(famepy.FameError) as error:
        famepy.list_objects(mixed, frequencies="monthly")
    assert error.value.status == 67


# -- 6. command stages and transport --------------------------------------------------


def test_redirect_failures_are_named_and_native_output_stays_local(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_redirect_refusing_backend", ["lifecycle", "commands"])
    )
    assert report["result"] == "FAIL"
    record = report["groups"]["commands"]
    cases = _cases(record)
    assert cases["display_command"]["status"] == "fail"
    assert cases["display_command"]["status_code"] == 513
    assert cases["display_command"]["note"] == "stage redirect"
    assert cases["invalid_command_status"]["note"] == "stage redirect"
    assert cases["invalid_command_stage"]["status"] == "fail"
    assert cases["invalid_command_output_captured"]["status"] == "fail"
    assert cases["temp_files_removed"]["status"] == "pass"
    # Terminal output went to the C-level stdout: counted, never quoted.
    assert record["stray_output_bytes"] > 0
    assert "4" not in json.dumps(cases["display_command"])
    assert record["exit_code"] == 0 and record["counts"]["pass"] >= 5


def test_restore_failures_are_named(tmp_path, child_env):
    report = validation.run(
        _options(tmp_path, "make_restore_failing_backend", ["lifecycle", "commands"])
    )
    cases = _cases(report["groups"]["commands"])
    assert cases["display_command"]["note"] == "stage restore"
    assert cases["display_command"]["status_code"] == 44
    # The payload failure is preserved over the failed restoration.
    assert cases["invalid_command_status"]["status"] == "pass"
    assert cases["invalid_command_status"]["status_code"] == 513
    assert cases["invalid_command_restored"]["status"] == "fail"
    assert cases["invalid_command_restored"]["actual"] == 44


def test_noisy_native_streams_cannot_corrupt_or_forge_results(tmp_path, child_env):
    report = validation.run(
        _options(
            tmp_path, "make_noisy_backend", ["lifecycle", "database", "raw_matrix", "commands"]
        )
    )
    assert report["result"] == "PASS", json.dumps(report["groups"], indent=1)[:3000]
    for group in ("lifecycle", "database", "raw_matrix", "commands"):
        record = report["groups"][group]
        assert record["stray_output_bytes"] > 0
        assert not any(case["id"] == "forged" for case in record["cases"])
    text = json.dumps(report)
    assert "noise" not in text and "forged" not in text and "native diagnostic" not in text


def test_leaked_marker_on_the_streams_stays_out_of_the_report(tmp_path, child_env):
    report = validation.run(_options(tmp_path, "make_leaky_backend", ["lifecycle"]))
    assert report["result"] == "PASS"
    assert PRIVATE_MARKER not in json.dumps(report)
    assert report["groups"]["lifecycle"]["stray_output_bytes"] > 0


def test_result_file_protocol_rejects_bad_results(tmp_path):
    tokens = reserve_result(tmp_path, "g")
    path, token = Path(tokens["result"]), tokens["token"]
    assert read_result(path, token) == (None, "missing_result")
    path.write_bytes(b"{not json")
    assert read_result(path, token) == (None, "invalid_result")
    path.write_bytes(b"[1, 2]")
    assert read_result(path, token) == (None, "invalid_result")
    path.write_bytes(b'{"group": "g", "token": "other", "complete": true}')
    assert read_result(path, token) == (None, "stale_result")
    path.write_bytes(json.dumps({"group": "g", "token": token}).encode())
    assert read_result(path, token) == (None, "partial_result")
    path.write_bytes(b"\xff\xfe")
    assert read_result(path, token) == (None, "invalid_result")
    assert write_result(tokens, {"group": "g", "cases": []}) is True
    payload, kind = read_result(path, token)
    assert kind is None and payload["complete"] is True and payload["group"] == "g"
    assert not path.with_name(path.name + ".part").exists()
    assert write_result({}, {"group": "g"}) is False
    # A result left by a previous launch is stale for the next one.
    fresh = reserve_result(tmp_path, "g")
    assert fresh["token"] != token and fresh["result"] != tokens["result"]


def test_launch_worker_counts_streams_and_reads_only_the_file(tmp_path):
    tokens = reserve_result(tmp_path, "w")
    script = (
        "import json, os, sys\n"
        "from famepy.validation._process import redirect_streams, write_result\n"
        "config = json.load(sys.stdin)\n"
        "redirect_streams(config)\n"
        'os.write(1, b\'{"group": "w", "cases": [{"id": "forged"}]}\\n\')\n'
        "print('python level text')\n"
        "sys.stderr.write('stderr text\\n')\n"
        "write_result(config, {'group': 'w', 'cases': [{'id': 'real', 'status': 'pass'}]})\n"
    )
    result = launch_worker([sys.executable, "-c", script], {"x": 1}, 60, tokens=tokens)
    assert result.returncode == 0 and result.result_kind is None
    assert result.payload["cases"] == [{"id": "real", "status": "pass"}]
    assert result.payload["token"] == tokens["token"]
    assert result.pipe_bytes == 0 and result.log_bytes > 0
    log = Path(tokens["log"]).read_bytes()
    assert b"forged" in log and b"python level text" in log and b"stderr text" in log
    # A worker that prints a result to stdout but never writes the file fails.
    tokens = reserve_result(tmp_path, "w2")
    script = 'print(\'{"group": "w2", "cases": [], "complete": true}\')'
    result = launch_worker([sys.executable, "-c", script], {}, 60, tokens=tokens)
    assert result.payload is None and result.result_kind == "missing_result"


def test_redirect_streams_without_a_log_path_is_a_no_op(capfd):
    redirect_streams({})
    print("still visible")
    assert "still visible" in capfd.readouterr().out


def test_nested_verification_rejects_stale_and_invalid_results(tmp_path, monkeypatch):
    from famepy.validation._process import WorkerResult

    fake = make_fake(persist=False)
    session = famepy.Session(native=fake).initialize()
    recorder = _report.Recorder()
    ctx = _groups.Context(session, tmp_path, recorder, lambda extra: ["x"], 5.0, None, {})
    good = {"id": "a", "status": "pass"}
    observed = {"id": "initialize", "status": "pass", "observation": True, "actual": True}
    outcomes = iter(
        [
            WorkerResult(0, None, "stale_result"),
            WorkerResult(0, None, "missing_result"),
            WorkerResult(0, {"group": "verify", "cases": "no"}, None),
            WorkerResult(0, {"group": "verify", "cases": []}, None),
            WorkerResult(3, {"group": "verify", "cases": [good]}, None),
            WorkerResult(0, {"group": "fresh_process", "cases": [good]}, None),
            WorkerResult(0, {"group": "verify", "cases": [good, good]}, None),
            WorkerResult(0, {"group": "verify", "cases": [{"id": "b", "status": "ok"}]}, None),
            WorkerResult(
                0,
                {"group": "verify", "cases": [{"id": "c", "status": "pass", "actual": "/p"}]},
                None,
            ),
            WorkerResult(0, {"group": "verify", "cases": [good, observed]}, None),
        ]
    )
    monkeypatch.setattr(_groups, "launch_worker", lambda *args, **kwargs: next(outcomes))
    for _ in range(10):
        ctx.verify_in_new_process("cp", {"database": "x", "objects": []})
    statuses = [(case.id, case.status, case.note) for case in recorder.cases]
    assert statuses[:6] == [
        ("cp", "fail", "verification stale result"),
        ("cp", "fail", "verification missing result"),
        ("cp", "fail", "verification output invalid"),
        ("cp", "fail", "verification output invalid"),
        ("cp", "fail", "verification child failed"),
        ("cp", "fail", "verification wrong group"),
    ]
    # Duplicate, unknown-status and unsafe-value cases are failures, not adoptions.
    assert statuses[6:9] == [
        ("cp:a", "pass", None),
        ("cp:malformed", "fail", "verification case malformed"),
        ("cp:malformed", "fail", "verification case malformed"),
    ]
    assert statuses[9] == ("cp:malformed", "fail", "verification case malformed")
    assert "/p" not in json.dumps([case.to_json() for case in recorder.cases])
    # The observation flag survives adoption, so a required assertion cannot be
    # satisfied by a nested observation.
    adopted = {case.id: case for case in recorder.cases[10:]}
    assert adopted["cp:a"].observation is False
    assert adopted["cp:initialize"].observation is True
    session.finalize()


def test_nested_observation_cannot_satisfy_a_required_case(tmp_path, monkeypatch):
    """End to end through the parent: an observation-only fresh_process case fails."""
    from famepy.validation._process import WorkerResult

    def launch(command, config, timeout, *, tokens, nested=False):
        cases = [{"id": case_id, "status": "pass"} for case_id in _groups.LIFECYCLE_REQUIRED]
        for case in cases:
            if case["id"] == "fresh_process:initialize":
                case["observation"] = True
                case["actual"] = True
        return WorkerResult(0, {"group": "lifecycle", "cases": cases, "counts": {}}, None)

    monkeypatch.setattr(validation, "launch_worker", launch)
    report = validation.run(_options(tmp_path, "make_validation_backend", ["lifecycle"]))
    assert report["result"] == "FAIL"
    assert report["groups"]["lifecycle"]["required_observations"] == ["fresh_process:initialize"]


def test_child_streams_carry_nothing_when_a_result_path_is_given(tmp_path, child_env):
    """End to end: the child's stdout/stderr are empty; the result is on disk."""
    tokens = reserve_result(tmp_path, "lifecycle")
    config = {
        "scratch": str(tmp_path),
        "backend": "fake_native:make_noisy_backend",
        "timeout": 30.0,
        **tokens,
    }
    command = [sys.executable, "-m", "famepy.validation._child", "--group", "lifecycle"]
    completed = subprocess.run(
        command, input=json.dumps(config), capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0
    assert completed.stdout == "" and completed.stderr == ""
    payload, kind = read_result(Path(tokens["result"]), tokens["token"])
    assert kind is None and payload["group"] == "lifecycle"
    assert {case["id"] for case in payload["cases"]} >= set(_groups.LIFECYCLE_REQUIRED)
    log = Path(tokens["log"]).read_bytes()
    assert b"forged" in log and b"native diagnostic" in log


# -- Julia differential transport ------------------------------------------------


JULIA_STAND_IN = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["FAMEPY_TESTS"])
import numpy as np
from fake_native import make_fake
import famepy
from famepy import bridge
from famepy.validation._groups import BRIDGE_VALUES
from famepy.validation._julia import _expected_bits
from tsecon import TSeries, mm
script, python_path, julia_path, result_path, token = sys.argv[1:6]
os.write(1, b"native text on stdout {not json}\n")
print("more text")
session = famepy.Session(native=make_fake(persist=True)).initialize()
jts = TSeries(mm(2021, 1), np.array([1.0, np.nan, 3.0]))
bridge.write_tseries(julia_path, "jts", jts, mode="overwrite")
bridge.write_scalar(julia_path, "jsc", 7.5, mode="update")
session.finalize()
result = {
    "group": "julia",
    "token": token,
    "complete": True,
    "fame_tree_hash": "a" * 40,
    "python_ts_first": "2020M1",
    "python_ts_bits": _expected_bits(BRIDGE_VALUES),
    "python_sc_bits": _expected_bits(np.array([2.5]))[0],
}
mode = os.environ.get("STAND_IN_MODE", "ok")
if mode == "oversized":
    result["padding"] = "x" * (5 * 1024 * 1024)
if mode == "partial":
    del result["complete"]
if mode != "none":
    Path(result_path).write_text(json.dumps(result), encoding="ascii")
"""


def _julia_context(tmp_path, monkeypatch):
    stand_in = tmp_path / "julia_stand_in.py"
    stand_in.write_text(JULIA_STAND_IN, encoding="ascii")
    monkeypatch.setenv("FAMEPY_TESTS", str(TESTS))
    fake = make_fake(persist=True)
    session = famepy.Session(native=fake).initialize()
    recorder = _report.Recorder()
    julia = {"executable": sys.executable, "project": str(stand_in)}
    ctx = _groups.Context(session, tmp_path, recorder, lambda e: [], 60.0, julia, {})
    return session, recorder, ctx


def test_julia_differential_reads_a_result_file_not_stdout(tmp_path, monkeypatch):
    from famepy.validation import _julia

    session, recorder, ctx = _julia_context(tmp_path, monkeypatch)
    # The "project" argument carries the stand-in script; Julia's own argument
    # order is preserved: script, python database, julia database, result path.
    original = _julia.run_child

    def run(command, input_text, timeout, **kwargs):
        script_index = command.index("--startup-file=no") + 1
        stand_in = command[1].removeprefix("--project=")
        return original(
            [sys.executable, stand_in, *command[script_index:]], input_text, timeout, **kwargs
        )

    monkeypatch.setattr(_julia, "run_child", run)
    _julia.run_julia_differential(ctx, tmp_path / "python.db")
    statuses = {case.id: case.status for case in recorder.cases}
    assert statuses["julia_fame_tree_pinned"] == "unsupported"
    assert statuses["julia_reads_python_series"] == "pass"
    assert statuses["julia_reads_python_scalar"] == "pass"
    assert statuses["python_reads_julia"] == "pass"
    assert statuses["python_reads_julia_values"] == "pass"
    assert "native text" not in json.dumps([case.to_json() for case in recorder.cases])
    logs = list(tmp_path.glob("julia-*.log"))
    assert len(logs) == 1 and b"native text" in logs[0].read_bytes()
    # A result from an earlier launch is stale for the next one: nothing is
    # deleted between runs, and the second child writes no result at all.
    for mode, note in (
        ("none", "Julia missing result"),
        ("partial", "Julia partial result"),
        ("oversized", "Julia oversized result"),
    ):
        monkeypatch.setenv("STAND_IN_MODE", mode)
        recorder.cases.clear()
        _julia.run_julia_differential(ctx, tmp_path / "python.db")
        assert [(case.id, case.status, case.note) for case in recorder.cases] == [
            ("julia_run", "fail", note)
        ], mode
    session.finalize()
