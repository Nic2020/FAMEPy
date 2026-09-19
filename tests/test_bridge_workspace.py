# SPDX-License-Identifier: MIT
"""Workspace reads and writes: flattening, names, collections, containment."""

import numpy as np
import pytest
from tsecon import MIT, MVTSeries, TSeries, Unit, Workspace, mm, qq

import famepy
from famepy import DataValidationError, bridge
from famepy.bridge import NameList, ObjectFailure, ReadReport, WriteReport


def reference_workspace():
    """The reference test workspace (fixed values instead of random draws)."""
    return Workspace(
        a=1,
        b=TSeries(qq(2020, 1), np.arange(10, dtype=np.float64)),
        s=MVTSeries(mm(2020, 1), ("q", "p"), np.arange(48, dtype=np.float64).reshape(24, 2)),
        c=Workspace(alpha=0.1, beta=0.8, n=Workspace(s="Hello World")),
    )


def _names(db):
    return sorted(info.name_text for info in famepy.list_objects(db))


# -- writing --------------------------------------------------------------------


def test_reference_flattening_and_listing(db):
    written = bridge.write_workspace(db, reference_workspace())
    assert written == ("a", "b", "s_q", "s_p", "c_alpha", "c_beta", "c_n_s")
    assert _names(db) == ["A", "B", "C_ALPHA", "C_BETA", "C_N_S", "S_P", "S_Q"]
    infos = {i.name_text: i for i in famepy.list_objects(db)}
    # Deliberate difference: a Python int is a precision scalar (reference: numeric).
    assert infos["A"].kind == "precision" and infos["A"].is_scalar
    assert infos["B"].frequency == 162 and infos["S_Q"].frequency == 129
    assert infos["C_N_S"].kind == "string"


def test_flatten_names_is_pure_and_validated():
    flat = bridge.flatten_names([reference_workspace()], prefix="w", glue="__")
    assert [name for name, _ in flat] == [
        "w__a",
        "w__b",
        "w__s__q",
        "w__s__p",
        "w__c__alpha",
        "w__c__beta",
        "w__c__n__s",
    ]
    assert [n for n, _ in bridge.flatten_names([Workspace(x=1)], prefix="")] == ["_x"]
    assert [n for n, _ in bridge.flatten_names([Workspace(x=1)], prefix=None)] == ["x"]
    assert [n for n, _ in bridge.flatten_names([{"m": {"k": 1}}])] == ["m_k"]
    two = bridge.flatten_names([Workspace(x=1), MVTSeries(mm(2020, 1), ("y",), np.ones((1, 1)))])
    assert [n for n, _ in two] == ["x", "y"]
    with pytest.raises(TypeError):
        bridge.flatten_names([1.0])
    with pytest.raises(TypeError):
        bridge.flatten_names([{1: 2}])
    with pytest.raises(TypeError):
        bridge.flatten_names([Workspace()], glue=3)
    with pytest.raises(TypeError):
        bridge.flatten_names([Workspace()], prefix=3)


def test_flattened_collisions_and_cycles_are_refused_before_any_write(session, tmp_path):
    fake = session._native.fake
    path = tmp_path / "w.db"
    fake.calls.clear()
    with pytest.raises(bridge.NameCollisionError):
        bridge.write_workspace(path, Workspace(a_b=1, a=Workspace(b=2)), mode="create")
    with pytest.raises(bridge.NameCollisionError):
        bridge.write_workspace(path, Workspace(x=1), Workspace(X=2), mode="create")
    loop = Workspace(v=1)
    loop["self"] = loop
    with pytest.raises(bridge.WorkspaceCycleError):
        bridge.write_workspace(path, loop, mode="create")
    indirect = {"a": {"b": {}}}
    indirect["a"]["b"]["back"] = indirect
    with pytest.raises(bridge.WorkspaceCycleError):
        bridge.write_workspace(path, indirect, mode="create")
    with pytest.raises(ValueError):
        bridge.write_workspace(path, Workspace(**{"": 1}), mode="create")
    with pytest.raises(TypeError):
        bridge.write_workspace(path, Workspace(bad=object()), mode="create")
    with pytest.raises(DataValidationError):
        bridge.write_workspace(path, Workspace(i=2**53 + 1), mode="create")
    with pytest.raises(ValueError):
        bridge.write_workspace(path, Workspace(a=1))
    with pytest.raises(ValueError):
        bridge.write_workspace(path, Workspace(a=1), mode="create", empty="x")
    with pytest.raises(TypeError):
        bridge.write_workspace(3, Workspace(a=1))
    assert fake.calls == []
    assert not path.exists()
    # A shared (not cyclic) sub-workspace is fine.
    shared = Workspace(k=1.0)
    bridge.write_workspace(path, Workspace(p=shared, q=shared), mode="create")
    assert bridge.read_workspace(path) == Workspace(p_k=1.0, q_k=1.0)


def test_write_preserves_inputs_and_replaces_by_default(db):
    ts = TSeries(mm(2020, 1), np.array([np.nan, 2.0]))
    original = ts.values.copy()
    w = Workspace(ts=ts)
    bridge.write_workspace(db, w)
    assert np.array_equal(ts.values, original, equal_nan=True) and list(w) == ["ts"]
    bridge.write_workspace(db, Workspace(ts=TSeries(qq(2020, 1), [1.0])))
    assert famepy.quick_info(db, "ts").frequency == 162
    with pytest.raises(famepy.FameError):
        bridge.write_workspace(db, Workspace(ts=1.0), replace=False)
    assert famepy.quick_info(db, "ts").frequency == 162


def test_path_owned_write_posts_and_closes(session, tmp_path):
    path = tmp_path / "p.db"
    fake = session._native.fake
    bridge.write_workspace(path, reference_workspace(), mode="create")
    assert "cfmpodb" in fake.calls and session.open_databases == ()
    assert len(bridge.read_workspace(path)) == 7
    fake.calls.clear()
    with famepy.open_database(path, "update", session=session) as database:
        bridge.write_workspace(database, Workspace(z=1.0))
        assert "cfmpodb" not in fake.calls
        with pytest.raises(ValueError):
            bridge.write_workspace(database, Workspace(z=1.0), mode="update")
    assert "z" not in bridge.read_workspace(path)  # unposted, discarded by the fake


def test_strict_write_failure_stops_without_posting(session, tmp_path):
    path = tmp_path / "f.db"
    bridge.write_workspace(path, Workspace(keep=1.0), mode="create")
    fake = session._native.fake
    fake.refuse_objects = {"BAD": 912}
    with pytest.raises(famepy.FameError) as info:
        bridge.write_workspace(path, Workspace(first=2.0, bad=3.0, later=4.0), mode="update")
    assert info.value.status == 912
    assert session.open_databases == ()
    assert bridge.read_workspace(path) == Workspace(keep=1.0)


def test_report_write_contains_failures(session, tmp_path):
    path = tmp_path / "r.db"
    fake = session._native.fake
    fake.refuse_objects = {"BAD": 912}
    report = bridge.write_workspace_report(
        path, Workspace(first=2.0, bad=3.0, later=4.0, huge=2**53 + 1), mode="create"
    )
    assert isinstance(report, WriteReport)
    assert report.written == ("first", "later") and report.posted and not report.complete
    assert [str(f) for f in report.failures] == [
        "huge: DataValidationError",
        "bad: FameError (status 912)",
    ]
    assert report.failures[1].status == 912 and report.failures[0].status is None
    assert bridge.read_workspace(path) == Workspace(first=2.0, later=4.0)
    fake.refuse_objects = {"ONLY": 912}
    report = bridge.write_workspace_report(path, Workspace(only=1.0), mode="update")
    assert report.written == () and not report.posted
    with pytest.raises(bridge.NameCollisionError):
        bridge.write_workspace_report(path, Workspace(a=1.0, A=2.0), mode="update")


def test_multiple_inputs_prefix_and_options(db):
    m = MVTSeries(mm(2020, 1), ("x", "y"), np.ones((2, 2)))
    bridge.write_workspace(db, Workspace(a=1.0), m, {"d": qq(2020, 1)}, prefix="in", glue="__")
    assert _names(db) == ["IN__A", "IN__D", "IN__X", "IN__Y"]
    with pytest.raises(TypeError):
        bridge.write_workspace(db, TSeries(mm(2020, 1), [1.0]))
    bridge.write_workspace(
        db, Workspace(obs=TSeries(mm(2020, 1), [1.0])), observed=famepy.Observed.AVERAGED
    )
    assert db.session._native.fake.handles[db.key].objects["OBS"].observed == 3


# -- reading --------------------------------------------------------------------


@pytest.fixture
def data_db(session, tmp_path):
    path = tmp_path / "data.db"
    bridge.write_workspace(path, reference_workspace(), mode="create")
    return path


def test_reference_read_cases(data_db):
    everything = bridge.read_workspace(data_db)
    assert list(everything) == ["a", "b", "c_alpha", "c_beta", "c_n_s", "s_p", "s_q"]
    assert everything.a == 1.0 and everything.c_n_s == "Hello World"
    assert everything.b.frequency == qq(2020, 1).frequency and len(everything.b) == 10
    assert everything.s_q.values.tolist() == [float(v) for v in range(0, 48, 2)]

    two = bridge.read_workspace(data_db, "a", "b")
    assert list(two) == ["a", "b"]
    assert list(bridge.read_workspace(data_db, "b", "a")) == ["b", "a"]

    stripped = bridge.read_workspace(data_db, prefix="c")
    assert list(stripped) == ["a", "b", "alpha", "beta", "n_s", "s_p", "s_q"]

    wild = bridge.read_workspace(data_db, "s?")
    assert list(wild) == ["s_p", "s_q"]
    assert list(bridge.read_workspace(data_db, "s?", prefix="s")) == ["p", "q"]
    assert list(bridge.read_workspace(data_db, "s^p")) == ["s_p"]

    collected = bridge.read_workspace(data_db, "c?", collect="c")
    assert list(collected) == ["c"] and list(collected.c) == ["alpha", "beta", "n_s"]

    nested = bridge.read_workspace(data_db, collect=[("c", ["n"]), "s"])
    assert list(nested) == ["a", "b", "c", "s"]
    assert list(nested.c) == ["alpha", "beta", "n"] and list(nested.c.n) == ["s"]
    assert nested.c.n.s == "Hello World"
    assert list(nested.s) == ["p", "q"]
    assert bridge.read_workspace(data_db, collect={"c": ["n"], "s": []}) == nested
    assert bridge.read_workspace(data_db, collect=("c", "s")) != nested
    assert bridge.read_workspace(data_db, collect=("c", "s")) == bridge.read_workspace(
        data_db, collect=["c", "s"]
    )
    with pytest.raises(TypeError):
        bridge.read_workspace(data_db, collect=3)
    with pytest.raises(TypeError):
        bridge.read_workspace(data_db, collect=[("c", 3)])
    with pytest.raises(ValueError):
        bridge.read_workspace(data_db, collect=[("", ["n"])])


def test_collect_wildcard_uses_first_part_and_namecase(data_db):
    w = bridge.read_workspace(data_db, "c?", "s?", collect="?")
    assert list(w) == ["c", "s"] and list(w.c) == ["alpha", "beta", "n_s"]
    upper = bridge.read_workspace(data_db, "s?", collect="*", namecase=str.upper)
    assert list(upper) == ["S"] and list(upper.S) == ["P", "Q"]
    # A collect prefix given explicitly keeps its spelling as the key.
    assert list(bridge.read_workspace(data_db, "c?", collect="C")) == ["C"]
    # Prefix applies before collect.
    w = bridge.read_workspace(data_db, "c?", prefix="c", collect="n")
    assert list(w) == ["alpha", "beta", "n"] and list(w.n) == ["s"]


def test_namecase_and_key_rules(data_db):
    custom = bridge.read_workspace(data_db, "a", namecase=lambda n: "x_" + n.lower())
    assert list(custom) == ["x_a"]
    with pytest.raises(ValueError):
        bridge.read_workspace(data_db, "a", namecase=lambda n: "")
    with pytest.raises(ValueError):
        bridge.read_workspace(data_db, "a", namecase=lambda n: 3)
    with pytest.raises(TypeError):
        bridge.read_workspace(data_db, "a", namecase=3)
    with pytest.raises(TypeError):
        bridge.read_workspace(data_db, "a", glue=None)
    with pytest.raises(TypeError):
        bridge.read_workspace(data_db, "a", collect=[3])
    with pytest.raises(ValueError):
        bridge.read_workspace(data_db, "a", collect="")
    assert list(bridge.read_workspace(data_db, "A", "b")) == ["a", "b"]
    assert list(bridge.read_workspace(data_db, "a", "A", "a")) == ["a"]
    assert list(bridge.read_workspace(data_db, "s?", "s_p")) == ["s_p", "s_q"]


def test_collisions_after_transformation_are_refused(session, tmp_path):
    path = tmp_path / "c.db"
    bridge.write_workspace(path, Workspace(c_x=1.0, x=2.0, c=3.0, d_y=4.0), mode="create")
    with pytest.raises(bridge.NameCollisionError, match="C_X and X"):
        bridge.read_workspace(path, "c_x", "x", prefix="c")
    with pytest.raises(bridge.NameCollisionError, match="nested workspace"):
        bridge.read_workspace(path, "c", "c_x", collect="c")
    with pytest.raises(bridge.NameCollisionError):
        bridge.read_workspace(path, namecase=lambda n: "same")
    # The read had not started: nothing partial is returned in report mode either.
    with pytest.raises(bridge.NameCollisionError):
        bridge.read_workspace_report(path, "c_x", "x", prefix="c")
    assert list(bridge.read_workspace(path, "c_x", "d_y", collect="?")) == ["c", "d"]


def test_absent_explicit_name_and_empty_wildcard(data_db):
    with pytest.raises(famepy.FameError) as info:
        bridge.read_workspace(data_db, "nothere")
    assert info.value.status == 13
    with pytest.raises(famepy.FameError):
        bridge.read_workspace_report(data_db, "nothere")
    assert bridge.read_workspace(data_db, "zzz?") == Workspace()
    assert list(bridge.read_workspace(data_db, "s?", frequencies="monthly")) == ["s_p", "s_q"]
    assert bridge.read_workspace(data_db, "s?", frequencies="daily") == Workspace()
    assert list(bridge.read_workspace(data_db, "?", classes="scalar")) == [
        "a",
        "c_alpha",
        "c_beta",
        "c_n_s",
    ]
    # Filters never apply to explicit names.
    assert list(bridge.read_workspace(data_db, "b", classes="scalar")) == ["b"]
    with pytest.raises(ValueError):
        bridge.read_workspace(data_db, "?", frequencies="quarterly")


def test_report_read_contains_conversion_and_native_failures(session, tmp_path):
    path = tmp_path / "rep.db"
    with famepy.open_database(path, "create", session=session) as db:
        bridge.write_workspace(db, Workspace(ok=1.0, empty=TSeries(mm(2020, 1), [])))
        famepy.write_object(db, "ten", famepy.series("precision", "tenday", 3, np.zeros(2)))
        famepy.write_object(db, "bm", famepy.scalar("boolean", session.sentinels.boolean_nc))
        famepy.write_object(db, "nlbad", famepy.scalar("namelist", b"{A,,B}"))
        db.post()
    with pytest.raises(bridge.MissingValueError):
        bridge.read_workspace(path)
    with pytest.raises(bridge.EmptySeriesError):
        bridge.read_workspace(path, "empty", "ok")
    report = bridge.read_workspace_report(path)
    assert isinstance(report, ReadReport) and not report.complete
    assert list(report.workspace) == ["ok"] and report.raw == ()
    assert [str(f) for f in report.failures] == [
        "bm: MissingValueError",
        "empty: EmptySeriesError",
        "nlbad: DataValidationError",
        "ten: UnsupportedFrequencyError",
    ]
    assert all(isinstance(f, ObjectFailure) for f in report.failures)
    fallback = bridge.read_workspace_report(path, raw_fallback=True)
    assert fallback.complete and list(fallback.workspace) == ["bm", "empty", "nlbad", "ok", "ten"]
    assert fallback.raw == ("BM", "EMPTY", "NLBAD", "TEN")
    assert isinstance(fallback.workspace.ten, famepy.RawSeries)
    assert fallback.workspace.ten.frequency == 32
    strict_fallback = bridge.read_workspace(path, raw_fallback=True)
    assert isinstance(strict_fallback.bm, famepy.RawScalar)
    # A native read failure is contained but never replaced by a carrier.
    session._native.fake.fail_after = {"fame_get_precisions": [0, 777]}
    report = bridge.read_workspace_report(path, "ok", "ten", raw_fallback=True)
    assert [str(f) for f in report.failures] == ["ok: FameError (status 777)"]
    assert report.failures[0].key == ("ok",) and report.failures[0].error_type == "FameError"
    assert list(report.workspace) == ["ten"]


def test_missing_and_empty_policies_pass_through(session, tmp_path):
    path = tmp_path / "pol.db"
    bridge.write_workspace(
        path,
        Workspace(one=TSeries(mm(2020, 1), [np.nan]), nl=NameList(["a"]), s="text"),
        mode="create",
        empty="preserve",
    )
    assert len(bridge.read_workspace(path).one) == 1
    assert len(bridge.read_workspace(path, empty="reference").one) == 0
    with pytest.raises(bridge.MissingValueError):
        bridge.read_workspace(path, missing="strict")
    with pytest.raises(ValueError):
        bridge.read_workspace(path, missing="drop")
    with pytest.raises(ValueError):
        bridge.read_workspace(path, text="utf8")
    assert bridge.read_workspace(path, "s", text="bytes").s == b"text"
    assert bridge.read_workspace(path, "nl").nl == NameList(["A"])
    assert session.open_databases == ()
    with pytest.raises(TypeError):
        bridge.read_workspace(3)


def test_mvtseries_is_not_reconstructed(session, tmp_path):
    path = tmp_path / "m.db"
    m = MVTSeries(mm(2020, 1), ("q", "p"), np.arange(4, dtype=np.float64).reshape(2, 2))
    bridge.write_workspace(path, Workspace(s=m), mode="create")
    back = bridge.read_workspace(path)
    assert list(back) == ["s_p", "s_q"] and all(isinstance(v, TSeries) for v in back.values())
    nested = bridge.read_workspace(path, collect="s")
    assert isinstance(nested.s, Workspace)
    rebuilt = MVTSeries(mm(2020, 1), q=nested.s.q, p=nested.s.p)
    assert np.array_equal(rebuilt.values, m.values)


def test_case_string_series_and_scalars_in_workspace(session, tmp_path):
    path = tmp_path / "t.db"
    data = Workspace(
        svec=["qmazing", "qmazing", "p", "phantastic"],
        stup=("qmazing", "qmazing", "p", "phantastic"),
        nl_empty="{}",
        nl_1="{A}",
        nl="{A,HELLO,B,WORLD}",
    )
    bridge.write_workspace(path, data, mode="create")
    back = bridge.read_workspace(path)
    assert list(back) == ["nl", "nl_1", "nl_empty", "stup", "svec"]
    assert list(back.svec.values) == data.svec and back.svec.firstdate == MIT(Unit(), 1)
    assert list(back.stup.values) == list(data.stup)
    assert back.nl_empty == NameList() and back.nl_1 == NameList(["A"])
    assert back.nl.members == ("A", "HELLO", "B", "WORLD")


def test_unsupported_class_is_contained_without_a_carrier(session, tmp_path):
    from fake_native import FakeObject

    path = tmp_path / "formula.db"
    bridge.write_workspace(path, Workspace(ok=1.0), mode="create")
    with famepy.open_database(path, "update", session=session) as database:
        # The fake refuses to create formulas natively; inject one directly.
        handle = session._native.fake.handles[database.key]
        handle.objects["F"] = FakeObject(3, 5, 0, 0, 0, 1, 0, [1.0])
        database.post()
    with pytest.raises(famepy.UnsupportedOperationError):
        bridge.read_workspace(path)
    report = bridge.read_workspace_report(path, raw_fallback=True)
    assert list(report.workspace) == ["ok"] and report.raw == ()
    assert [str(f) for f in report.failures] == ["f: UnsupportedOperationError"]


# -- Regression: invalid input never opens, creates, truncates or mutates ----


def _kept_database(session, tmp_path, name):
    path = tmp_path / f"{name}.db"
    bridge.write_value(path, "kept", 42.0, mode="create")
    return path, path.read_bytes()


def _attempts(path, mode):
    ts = TSeries(mm(2020, 1), [1.0])
    return [
        ("value_basis", lambda: bridge.write_value(path, "x", 1.0, mode=mode, basis=999)),
        ("value_observed", lambda: bridge.write_value(path, "x", 1.0, mode=mode, observed="no")),
        ("value_bad_mode", lambda: bridge.write_value(path, "x", 1.0, mode="rewrite")),
        ("tseries_basis", lambda: bridge.write_tseries(path, "x", ts, mode=mode, basis=True)),
        ("scalar_type", lambda: bridge.write_scalar(path, "x", object(), mode=mode)),
        (
            "workspace_observed",
            lambda: bridge.write_workspace(path, {"x": 1.0}, mode=mode, observed=999),
        ),
        ("workspace_basis", lambda: bridge.write_workspace(path, {"x": 1.0}, mode=mode, basis=7)),
        ("workspace_value", lambda: bridge.write_workspace(path, {"x": object()}, mode=mode)),
        ("workspace_mode", lambda: bridge.write_workspace(path, {"x": 1.0}, mode="rewrite")),
        (
            "workspace_collision",
            lambda: bridge.write_workspace(path, {"x": 1.0}, {"X": 2.0}, mode=mode),
        ),
        (
            "report_observed",
            lambda: bridge.write_workspace_report(path, {"x": 1.0}, mode=mode, observed=999),
        ),
        ("report_mode", lambda: bridge.write_workspace_report(path, {"x": 1.0}, mode="rewrite")),
        ("case_date_value", lambda: bridge.write_value(path, "x", MIT(Unit(), 1), mode=mode)),
        (
            "case_date_workspace",
            lambda: bridge.write_workspace(path, {"x": MIT(Unit(), 1)}, mode=mode),
        ),
    ]


@pytest.mark.parametrize("mode", ["overwrite", "update", "create"])
def test_invalid_path_writes_never_open_or_change_the_file(session, tmp_path, mode):
    path, before = _kept_database(session, tmp_path, mode)
    fake = session._native.fake
    for label, attempt in _attempts(path, mode):
        fake.calls.clear()
        with pytest.raises((ValueError, TypeError)):
            attempt()
        assert fake.calls == [], label
        assert path.read_bytes() == before, label
    assert bridge.read_workspace(path) == Workspace(kept=42.0)


def test_report_with_no_convertible_object_does_not_open(session, tmp_path):
    path, before = _kept_database(session, tmp_path, "report")
    fake = session._native.fake
    fake.calls.clear()
    report = bridge.write_workspace_report(path, {"x": object(), "y": 2**53 + 1}, mode="overwrite")
    assert report.written == () and not report.posted and not report.complete
    assert [str(f) for f in report.failures] == ["x: TypeError", "y: DataValidationError"]
    assert fake.calls == [] and path.read_bytes() == before
    # Nothing to write at all: defined as a no-op, not an empty overwrite.
    assert bridge.write_workspace(path, Workspace(), mode="overwrite") == ()
    report = bridge.write_workspace_report(path, {}, mode="overwrite")
    assert report == WriteReport((), (), False) and report.complete
    assert fake.calls == [] and path.read_bytes() == before
    assert bridge.read_workspace(path) == Workspace(kept=42.0)
    # One convertible object among failures does open and write, as documented.
    report = bridge.write_workspace_report(path, {"x": object(), "z": 3.0}, mode="update")
    assert report.written == ("z",) and report.posted
    assert bridge.read_workspace(path) == Workspace(kept=42.0, z=3.0)


def test_invalid_attributes_on_a_handle_make_no_mutating_call(db):
    bridge.write_value(db, "kept", 1.0)
    fake = db.session._native.fake
    fake.calls.clear()
    with pytest.raises(ValueError):
        bridge.write_value(db, "kept", 2.0, replace=True, basis=999)
    with pytest.raises(ValueError):
        bridge.write_workspace(db, Workspace(kept=2.0), observed="sideways")
    with pytest.raises(ValueError):
        famepy.write_object(db, "kept", famepy.scalar("precision", 2.0), replace=True, basis=3)
    assert fake.calls == []
    assert bridge.read_value(db, "kept") == 1.0
    bridge.write_value(db, "b", TSeries(mm(2020, 1), [1.0]), basis="business", observed="averaged")
    obj = fake.handles[db.key].objects["B"]
    assert (obj.basis, obj.observed) == (2, 3)
    assert famepy._data.attribute_codes(None, None) == (None, None)
    assert famepy._data.attribute_codes(2, famepy.Observed.HIGH) == (2, 7)
    with pytest.raises(ValueError):
        famepy._data.attribute_codes(basis=object())


# -- Regression: collect matches the native name and removes the full prefix --


def _dest(name, collect, *, namecase=str.lower, glue="_", prefix=None):
    from famepy.bridge._workspace import _destination, _normalize_collect

    return _destination(
        name, glue=glue, namecase=namecase, prefix=prefix, collect=_normalize_collect(collect)
    )


def test_collect_prefix_containing_the_glue_removes_the_whole_prefix():
    assert _dest("A_B_C", "a_b") == ("a_b", "c")
    assert _dest("A_B_C_D", [("a_b", ["c"])]) == ("a_b", "c", "d")
    assert _dest("A_B", "a_b") == ("a_b",)  # nothing left to nest: stays a leaf
    assert _dest("A_B_", "a_b") == ("a_b_",)  # nothing after the glue: stays a leaf
    assert _dest("A_BC", "a_b") == ("a_bc",)  # no glue after the prefix: no match


def test_collect_wildcard_transforms_the_key_separately_from_matching():
    upper = lambda s: "x_" + s.lower()  # noqa: E731
    assert _dest("A_B", "*", namecase=upper) == ("x_a", "x_b")
    assert _dest("A_B_C", "?", namecase=upper) == ("x_a", "x_b_c")
    assert _dest("A", "*", namecase=upper) == ("x_a",)
    assert _dest("_A", "*") == ("_a",)  # an empty first part never collects
    assert _dest("A_B_C", [("*", ["*"])]) == ("a", "b", "c")


def test_collect_with_multi_character_glue_and_prefix_order():
    assert _dest("IN__A__B", "a", glue="__", prefix="in") == ("a", "b")
    assert _dest("IN__A__B", "in", glue="__") == ("in", "a__b")
    assert _dest("IN__A__B", [("in", ["a"])], glue="__") == ("in", "a", "b")
    with pytest.raises(ValueError):
        _dest("AB", "a", glue="")
    assert _dest("AB", [], glue="") == ("ab",)
    assert _dest("PAB", [], glue="", prefix="p") == ("ab",)


def test_recursive_collect_specification_is_refused():
    loop = ["a"]
    loop.append(("b", loop))
    with pytest.raises(ValueError, match="contains itself"):
        _dest("A_B", loop)
    mapping = {"a": []}
    mapping["a"].append(mapping)
    with pytest.raises(ValueError, match="contains itself"):
        _dest("A_B", mapping)
    shared = ["n"]
    assert _dest("C_N_S", [("c", shared), ("d", shared)]) == ("c", "n", "s")


def test_collect_shapes_are_complete_and_inputs_unchanged(session, tmp_path):
    path = tmp_path / "shape.db"
    source = Workspace(
        a_b=Workspace(c=1.0, d=2.0), a=Workspace(e=3.0), x=4.0, q=Workspace(r=Workspace(s=5.0))
    )
    snapshot = Workspace(
        a_b=Workspace(c=1.0, d=2.0), a=Workspace(e=3.0), x=4.0, q=Workspace(r=Workspace(s=5.0))
    )
    bridge.write_workspace(path, source, mode="create")
    assert source == snapshot
    flat = bridge.read_workspace(path)
    assert flat == Workspace(a_b_c=1.0, a_b_d=2.0, a_e=3.0, q_r_s=5.0, x=4.0)
    nested = bridge.read_workspace(path, collect=["a_b", "a", ("q", ["r"])])
    assert nested == Workspace(
        a_b=Workspace(c=1.0, d=2.0), a=Workspace(e=3.0), q=Workspace(r=Workspace(s=5.0)), x=4.0
    )
    # Order of collect entries decides which prefix claims an ambiguous name.
    other = bridge.read_workspace(path, "a?", collect=["a", "a_b"])
    assert other == Workspace(a=Workspace(b_c=1.0, b_d=2.0, e=3.0))
    with pytest.raises(bridge.NameCollisionError):
        bridge.read_workspace(path, collect=["a_b", "a", "q"], namecase=lambda n: "k")
    with pytest.raises(bridge.NameCollisionError):
        bridge.write_workspace(path, Workspace(a_b=1.0, A=Workspace(B=2.0)), mode="update")
