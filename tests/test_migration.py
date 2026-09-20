# SPDX-License-Identifier: MIT
"""The FAME-to-DataEcon migration: plan, loss policies, refusals, layout and readback."""

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import tsecon as ts
from canonical import scalar_object, series_object
from tsecon import MIT, TSeries, Unit
from tsecon.dataecon import open_dataecon

import famepy
from famepy import bridge, migration
from famepy._constants import MISSING_NA, MISSING_NC, MISSING_ND
from famepy._data import sentinel_value

AVAILABLE, BLOCK = migration.dataecon_available()
if not AVAILABLE and os.environ.get("FAMEPY_REQUIRE_DATAECON"):
    raise RuntimeError(f"DataEcon native extension required but unavailable: {BLOCK}")
pytestmark = pytest.mark.skipif(
    not AVAILABLE, reason=f"DataEcon native extension unavailable: {BLOCK}"
)


def _workspace():
    ws = ts.Workspace()
    ws["p"] = 1.5
    ws["n"] = np.float32(2.5)
    ws["b"] = True
    ws["dt"] = ts.mm(2020, 3)
    ws["s"] = "hello"
    ws["nl"] = bridge.NameList(["A", "B"])
    ws["ps"] = TSeries(ts.mm(2020, 1), [1.0, np.nan, 3.0])
    ws["ns"] = TSeries(ts.qq(2020, 1), np.array([1.0, 2.5], dtype=np.float32))
    ws["bs"] = TSeries(ts.mm(2020, 1), [True, False])
    ws["ds"] = bridge.DateSeries(ts.mm(2020, 1), [ts.daily("2020-02-29"), ts.daily("2021-01-01")])
    ws["dsm"] = bridge.DateSeries(ts.bdaily("2020-02-28"), [ts.qq(2020, 1), None])
    ws["ss"] = bridge.StringSeries(MIT(Unit(), 1), ["x", None, "z"])
    ws["es"] = TSeries(ts.mm(2020, 1), np.empty(0))
    ws["cs"] = TSeries(MIT(Unit(), 3), [7.0, 8.0])
    ws["wk"] = TSeries(ts.weekly("2020-02-28", 7), [4.0, 5.0])
    return ws


@pytest.fixture
def source(session, tmp_path):
    """A synthetic FAME database with every kind plus raw missing categories."""
    path = tmp_path / "source.db"
    ws = _workspace()
    s = session.sentinels
    first = bridge.mit_to_index(ts.mm(2020, 1), session=session)
    with famepy.opendb(path, "create", session=session) as db:
        famepy.writefame(db, ws)
        famepy.do_write(
            scalar_object("pna", "precision", sentinel_value("precision", MISSING_NA, s)), db
        )
        famepy.do_write(scalar_object("dsc", "date", s.index_nc, date_frequency="monthly"), db)
        famepy.do_write(
            series_object(
                "psnd",
                "precision",
                "monthly",
                first,
                np.array([1.0, sentinel_value("precision", MISSING_ND, s), 3.0]),
            ),
            db,
        )
        famepy.do_write(
            series_object(
                "bsna",
                "boolean",
                "monthly",
                first,
                np.array([1, sentinel_value("boolean", MISSING_NA, s), 0], dtype=np.int32),
            ),
            db,
        )
        famepy.do_write(
            scalar_object("bsc", "boolean", sentinel_value("boolean", MISSING_NC, s)), db
        )
        famepy.postdb(db)
    return path


def _expected(ws):
    expected = {name: migration.expected_object(name, value) for name, value in ws.items()}
    expected["es"] = migration.expected_object(
        "es", None, empty_frequency=ts.Monthly(), kind="precision"
    )
    expected["pna"] = migration.expected_object("pna", None, category=2, kind="precision")
    expected["dsc"] = migration.expected_object(
        "dsc", None, category=1, kind="date", value_frequency=ts.Monthly()
    )
    expected["psnd"] = migration.expected_object(
        "psnd", TSeries(ts.mm(2020, 1), [1.0, np.nan, 3.0]), categories=[0, 3, 0]
    )
    expected["bsna"] = migration.expected_object(
        "bsna", TSeries(ts.mm(2020, 1), [True, False, False]), categories=[0, 2, 0]
    )
    expected["bsc"] = migration.expected_object("bsc", None, category=1, kind="boolean")
    return expected


def test_plan_lists_every_object_and_touches_nothing(session, source, tmp_path):
    calls_before = len(session._native.fake.calls)
    plan = migration.plan_migration(source, session=session)
    assert not plan.refused and not plan.skipped
    assert {e.name for e in plan.stored} == {
        n.upper() for n in (*_workspace(), "pna", "dsc", "psnd", "bsna", "bsc")
    }
    entry = next(e for e in plan.stored if e.name == "DSM")
    assert (entry.kind, entry.frequency, entry.value_frequency, entry.destination) == (
        "date",
        "business",
        "quarterly_december",
        "dsm",
    )
    assert next(e for e in plan.stored if e.name == "ES").empty is True
    assert "cfmnwob" not in session._native.fake.calls[calls_before:]
    assert session.open_databases == ()
    assert "to store" in plan.summary() and migration.OMISSIONS == plan.omissions
    # The lossy policy names its losses per object before anything is written.
    lossy = migration.plan_migration(
        source, options=migration.MigrationOptions(missing="nan"), session=session
    )
    losses = {e.name: e.losses for e in lossy.entries}
    assert losses["PS"] == ("missing categories collapse to NaN",)
    assert losses["BS"] == ("a boolean series with missing observations is refused",)
    assert losses["ES"] == ()


def test_default_migration_round_trips_every_object(session, source, tmp_path):
    destination = tmp_path / "archive.daec"
    report = migration.migrate(source, destination, session=session)
    assert report.complete and report.status == "complete", report.summary()
    assert not report.failed and not report.lossy
    assert session.open_databases == ()
    expected = _expected(_workspace())
    with open_dataecon(destination) as db:
        assert migration.migration_status(db) == {
            "layout": "1",
            "status": "complete",
            "planned": str(len(expected)),
            "written": str(len(expected)),
        }
        names = migration.list_migrated(db)
        assert set(names) == set(expected)
        for name in names:
            back = migration.read_migrated(db, name)
            assert migration.describe(back) == migration.describe(expected[name]), name
        # Representations and sidecars follow the documented layout.
        assert migration.read_migrated(db, "dsm").representation == "codes"
        assert migration.read_migrated(db, "ds").representation == "native"
        assert migration.read_migrated(db, "ss").representation == "text"
        assert migration.read_migrated(db, "nl").representation == "members"
        assert migration.read_migrated(db, "es").empty is True
        assert migration.read_migrated(db, "es").value is None
        masks = {info.name for info in db.list_objects("/" + migration.MASK_CATALOG)}
        assert masks == {"ps", "dsm", "ss", "psnd", "bsna"}
        mask = db.read_series("/famepy_migration_masks/psnd")
        assert mask.values.dtype == np.int8 and list(mask.values) == [0, 3, 0]
        attributes = db.get_attributes("/dsm")
        assert attributes["famepy.migration.kind"] == "date"
        assert attributes["famepy.migration.value_frequency"] == "quarterly_december"
        assert attributes["famepy.migration.mask"] == "1"
        assert db.get_attributes("/es")["famepy.migration.empty"] == "unknown_firstdate"
        # Case indexing, weekly indexing and float32 bits survive exactly.
        assert migration.read_migrated(db, "cs").value.firstdate == MIT(Unit(), 3)
        assert migration.read_migrated(db, "wk").value.firstdate == ts.weekly("2020-02-28", 7)
        assert migration.read_migrated(db, "n").value.dtype == np.float32
        # A missing scalar reads as None with its category, never as a value.
        pna = migration.read_migrated(db, "pna")
        assert pna.value is None and pna.categories == 2
        dsc = migration.read_migrated(db, "dsc")
        assert dsc.value is None and dsc.categories == 1 and dsc.value_frequency == "monthly"


def test_explicit_empty_first_date_is_stored_and_wrong_one_refused(session, source, tmp_path):
    options = migration.MigrationOptions(empty_firstdates={"ES": ts.mm(2021, 1)})
    report = migration.migrate(
        source, tmp_path / "e.daec", patterns=("es",), options=options, session=session
    )
    assert report.complete
    with open_dataecon(tmp_path / "e.daec") as db:
        back = migration.read_migrated(db, "es")
        assert back.empty and isinstance(back.value, TSeries)
        assert back.value.firstdate == ts.mm(2021, 1) and len(back.value) == 0
        assert db.get_attributes("/es")["famepy.migration.empty"] == "explicit_firstdate"
    # A first date of another frequency, or one for a nonempty object, is a
    # known-invalid input: the plan refuses it and no file is created.
    wrong = migration.MigrationOptions(empty_firstdates={"es": ts.qq(2021, 1)})
    plan = migration.plan_migration(source, patterns=("es", "p"), options=wrong, session=session)
    assert [(e.name, e.action, e.reason) for e in plan.entries] == [
        ("ES", "refuse", "explicit first date has another frequency than the series"),
        ("P", "store", None),
    ]
    with pytest.raises(migration.MigrationRefused, match="refused by the plan"):
        migration.migrate(source, tmp_path / "w.daec", plan=plan, session=session)
    assert not (tmp_path / "w.daec").exists()
    nonempty = migration.MigrationOptions(empty_firstdates={"ps": ts.mm(2021, 1)})
    plan = migration.plan_migration(source, patterns=("ps",), options=nonempty, session=session)
    assert plan.entries[0].reason == "first date supplied for a nonempty object"
    refuse = migration.MigrationOptions(empty="refuse")
    with pytest.raises(migration.MigrationRefused):
        migration.migrate(source, tmp_path / "r.daec", options=refuse, session=session)
    assert not (tmp_path / "r.daec").exists()


def test_lossy_policy_needs_the_explicit_option_and_reports_per_object(session, source, tmp_path):
    lossy = migration.MigrationOptions(missing="nan")
    report = migration.migrate(
        source,
        tmp_path / "l.daec",
        patterns=("ps", "psnd", "bs", "bsna", "dsm", "pna", "bsc"),
        options=lossy,
        session=session,
    )
    outcomes = {e.name: (e.action, e.error_type, e.losses) for e in report.entries}
    assert outcomes["PS"] == ("stored", None, ("missing categories collapse to NaN",))
    assert outcomes["PSND"] == ("stored", None, ("missing categories collapse to NaN",))
    assert outcomes["PNA"] == ("stored", None, ("missing category collapses to NaN",))
    assert outcomes["BS"] == ("stored", None, ())
    assert outcomes["BSNA"][:2] == ("failed", "MigrationLossError")
    assert outcomes["DSM"][:2] == ("failed", "MigrationLossError")
    assert outcomes["BSC"][:2] == ("failed", "MigrationLossError")
    assert not report.complete and len(report.lossy) == 3
    with open_dataecon(tmp_path / "l.daec") as db:
        assert migration.migration_status(db)["status"] == "incomplete"
        assert db.list_objects("/" + migration.MASK_CATALOG) == []
        back = migration.read_migrated(db, "psnd")
        assert list(back.categories) == [0, 0, 0] and np.isnan(back.value.values[1])
        assert "famepy.migration.missing" not in db.get_attributes("/pna")


def _partials(directory):
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".partial"))


def test_existing_destination_is_never_opened_or_changed(session, source, tmp_path):
    existing = tmp_path / "existing.daec"
    existing.write_bytes(b"left alone")
    with pytest.raises(migration.MigrationRefused, match="already exists"):
        migration.migrate(source, existing, session=session)
    assert existing.read_bytes() == b"left alone" and _partials(tmp_path) == []
    # A finished archive is refused as a destination whatever the selection.
    archive = tmp_path / "a.daec"
    assert migration.migrate(source, archive, patterns=("p",), session=session).complete
    before = archive.read_bytes()
    for patterns in (("p",), ("n",)):
        with pytest.raises(migration.MigrationRefused, match="already exists"):
            migration.migrate(source, archive, patterns=patterns, session=session)
    assert archive.read_bytes() == before and _partials(tmp_path) == []
    with open_dataecon(archive) as db:
        assert migration.list_migrated(db) == ["p"]
    # A foreign DataEcon file holding an object under the mask catalog name is
    # not a destination either; its content is untouched.
    foreign = tmp_path / "foreign.daec"
    with open_dataecon(foreign, "w") as db:
        db.new_catalog("/" + migration.MASK_CATALOG)
        db.write_series(
            "/" + migration.MASK_CATALOG + "/p", TSeries(ts.mm(2000, 1), np.array([77], np.int8))
        )
    before = foreign.read_bytes()
    with pytest.raises(migration.MigrationRefused, match="already exists"):
        migration.migrate(source, foreign, patterns=("p",), session=session)
    assert foreign.read_bytes() == before
    with open_dataecon(foreign) as db:
        assert db.read_series("/" + migration.MASK_CATALOG + "/p").values.tolist() == [77]
    # The source itself is not a destination.
    with pytest.raises(migration.MigrationRefused, match="is the source"):
        migration.migrate(source, source, session=session)


def test_incomplete_archive_is_never_completed_by_a_later_run(session, tmp_path):
    src = tmp_path / "t.db"
    with famepy.opendb(src, "create", session=session) as db:
        famepy.do_write(scalar_object("bad", "string", b"caf\xe9"), db)
        famepy.do_write(scalar_object("good", "string", b"cafe"), db)
        famepy.postdb(db)
    archive = tmp_path / "t.daec"
    first = migration.migrate(src, archive, patterns=("bad",), session=session)
    assert first.status == "incomplete"
    before = archive.read_bytes()
    with pytest.raises(migration.MigrationRefused, match="already exists"):
        migration.migrate(src, archive, patterns=("good",), session=session)
    assert archive.read_bytes() == before
    with open_dataecon(archive) as db:
        status = migration.migration_status(db)
        assert status["status"] == "incomplete" and status["written"] == "0"
        assert migration.list_migrated(db) == []


def test_destination_claim_is_exclusive_and_failed_runs_leave_partials(
    session, source, tmp_path, monkeypatch
):
    from famepy.migration import _migrate

    # A file that appears between the early check and the claim is refused too.
    target = tmp_path / "raced.daec"

    def check(destination, src):
        target.write_bytes(b"appeared")
        return str(destination)

    monkeypatch.setattr(_migrate, "_check_destination", check)
    with pytest.raises(migration.MigrationRefused, match="already exists"):
        migration.migrate(source, target, patterns=("p",), session=session)
    assert target.read_bytes() == b"appeared" and _partials(tmp_path) == []
    monkeypatch.undo()

    # An error outside the per-object containment releases the claim and
    # leaves the partial file, marked incomplete, next to it.
    def explode(db, database, entry, options):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(_migrate, "_migrate_one", explode)
    broken = tmp_path / "broken.daec"
    with pytest.raises(RuntimeError, match="synthetic"):
        migration.migrate(source, broken, patterns=("p",), session=session)
    assert not broken.exists()
    partials = _partials(tmp_path)
    assert len(partials) == 1 and partials[0].startswith("broken.daec.")
    with open_dataecon(tmp_path / partials[0]) as db:
        assert migration.migration_status(db)["status"] == "incomplete"
    monkeypatch.undo()
    assert migration.migrate(source, broken, patterns=("p",), session=session).complete


def test_stale_or_mismatched_plans_are_refused(session, source, tmp_path):
    plan = migration.plan_migration(source, session=session)
    with pytest.raises(ValueError, match="patterns disagree"):
        migration.migrate(source, tmp_path / "x.daec", patterns=("p",), plan=plan, session=session)
    with pytest.raises(ValueError, match="options disagree"):
        migration.migrate(
            source,
            tmp_path / "x.daec",
            options=migration.MigrationOptions(),
            plan=plan,
            session=session,
        )
    with famepy.opendb(source, "update", session=session) as db:
        famepy.do_write(scalar_object("extra", "string", b"added later"), db)
        famepy.postdb(db)
    with pytest.raises(migration.MigrationRefused, match="no longer matches"):
        migration.migrate(source, tmp_path / "x.daec", plan=plan, session=session)
    assert not (tmp_path / "x.daec").exists() and _partials(tmp_path) == []
    fresh = migration.plan_migration(source, session=session)
    assert migration.migrate(source, tmp_path / "x.daec", plan=fresh, session=session).complete


def test_refused_plans_create_nothing(session, tmp_path):
    unsupported = tmp_path / "u.db"
    with famepy.opendb(unsupported, "create", session=session) as db:
        famepy.do_write(series_object("tenday", "precision", "tenday", 1, np.array([1.0])), db)
        famepy.do_write(scalar_object("ok", "precision", 2.0), db)
        famepy.do_write(scalar_object("ok2", "precision", 3.0), db)
        famepy.postdb(db)
    plan = migration.plan_migration(unsupported, session=session)
    assert [(e.name, e.action, e.reason) for e in plan.entries] == [
        ("OK", "store", None),
        ("OK2", "store", None),
        ("TENDAY", "refuse", "unsupported index frequency"),
    ]
    with pytest.raises(migration.MigrationRefused, match="refused by the plan"):
        migration.migrate(unsupported, tmp_path / "x.daec", plan=plan, session=session)
    assert not (tmp_path / "x.daec").exists()
    skip = migration.MigrationOptions(unsupported="skip")
    report = migration.migrate(unsupported, tmp_path / "y.daec", options=skip, session=session)
    assert not report.complete and report.status == "incomplete"
    assert [(e.name, e.action) for e in report.entries] == [
        ("TENDAY", "skipped"),
        ("OK", "stored"),
        ("OK2", "stored"),
    ]
    # Two objects landing on one name are both refused; the mask catalog name too.
    collide = migration.MigrationOptions(namecase=lambda name: "same")
    plan = migration.plan_migration(
        unsupported, patterns=("ok", "ok2"), options=collide, session=session
    )
    assert [(e.action, e.reason) for e in plan.entries] == [
        ("refuse", "destination name collision")
    ] * 2
    reserved = migration.MigrationOptions(namecase=lambda name: migration.MASK_CATALOG)
    plan = migration.plan_migration(
        unsupported, patterns=("ok",), options=reserved, session=session
    )
    assert plan.entries[0].reason == "invalid destination name"
    with pytest.raises(migration.MigrationRefused):
        migration.migrate(unsupported, tmp_path / "z.daec", plan=plan, session=session)
    assert not (tmp_path / "z.daec").exists()


def test_options_are_validated():
    with pytest.raises(ValueError, match="missing"):
        migration.MigrationOptions(missing="drop")
    with pytest.raises(ValueError, match="unsupported"):
        migration.MigrationOptions(unsupported="ignore")
    with pytest.raises(ValueError, match="catalog"):
        migration.MigrationOptions(catalog="relative")
    with pytest.raises(ValueError, match="trailing slash"):
        migration.MigrationOptions(catalog="/archive/")
    with pytest.raises(ValueError, match="empty components"):
        migration.MigrationOptions(catalog="/archive//x")
    with pytest.raises(ValueError, match="reserved"):
        migration.MigrationOptions(catalog="/" + migration.MASK_CATALOG + "/x")
    with pytest.raises(TypeError):
        migration.MigrationOptions(existing="append")
    with pytest.raises(TypeError):
        migration.MigrationOptions(empty_firstdates={"x": 3})
    with pytest.raises(TypeError):
        migration.MigrationOptions(namecase="lower")


def test_conversion_failure_is_contained_and_marked(session, tmp_path):
    """A non-ASCII string cannot cross the text boundary; the archive says so."""
    source = tmp_path / "t.db"
    with famepy.opendb(source, "create", session=session) as db:
        famepy.do_write(scalar_object("bad", "string", b"caf\xe9"), db)
        famepy.do_write(scalar_object("good", "string", b"cafe"), db)
        famepy.postdb(db)
    report = migration.migrate(source, tmp_path / "t.daec", session=session)
    outcomes = {e.name: (e.action, e.error_type) for e in report.entries}
    assert outcomes == {"BAD": ("failed", "TextEncodingError"), "GOOD": ("stored", None)}
    assert not report.complete and "failed" in report.summary()
    with open_dataecon(tmp_path / "t.daec") as db:
        assert migration.migration_status(db)["status"] == "incomplete"
        assert migration.list_migrated(db) == ["good"]


def _restore(db, path, attributes):
    """Put back the attributes a carrier rewrite dropped (a set attribute stays)."""
    current = db.get_attributes(path)
    for key, value in attributes.items():
        if key not in current:
            db.set_attribute(path, key, value)


def test_readback_detects_corruption_and_foreign_objects(session, source, tmp_path):
    destination = tmp_path / "c.daec"
    migration.migrate(source, destination, patterns=("ps", "dsm", "s", "es", "wk"), session=session)
    expected = _expected(_workspace())
    with open_dataecon(destination, "a") as db:
        attributes = db.get_attributes("/ps")
        db.write_series("/ps", TSeries(ts.mm(2020, 1), [1.0, np.nan, 4.0]), overwrite=True)
        _restore(db, "/ps", attributes)
        db.write_series(
            "/famepy_migration_masks/dsm",
            TSeries(ts.bdaily("2020-02-28"), np.array([0], np.int8)),
            overwrite=True,
        )
        db.write_scalar("foreign", 3.0)
    with open_dataecon(destination) as db:
        assert migration.describe(migration.read_migrated(db, "ps")) != migration.describe(
            expected["ps"]
        )
        with pytest.raises(migration.LayoutError, match="mask"):
            migration.read_migrated(db, "dsm")
        with pytest.raises(migration.LayoutError, match="kind attribute"):
            migration.read_migrated(db, "foreign")
        assert migration.list_migrated(db) == ["dsm", "es", "ps", "s", "wk"]


def _shift_mask(db, ts_, orig):
    mask = TSeries(ts_.mm(1990, 1), np.array([0, 1, 0], np.int8))
    db.write_series("/famepy_migration_masks/ps", mask, overwrite=True)


def _yearly_carrier(db, ts_, orig):
    yearly = TSeries(MIT(ts_.Yearly(), int(orig.value.firstdate)), orig.value.values.copy())
    db.write_series("/ps", yearly, overwrite=True)


def _undeclared_mask(db, ts_, orig):
    mask = TSeries(orig.value.firstdate, np.array([0, 1], np.int8))
    db.write_series("/famepy_migration_masks/wk", mask)


CORRUPTIONS = [
    ("ps", _shift_mask, "start with the object"),
    ("ps", _yearly_carrier, "another frequency than its label"),
    ("es", lambda db, t, o: db.write_array("/es", np.array([99.0]), overwrite=True), "stores 1"),
    (
        "es",
        lambda db, t, o: db.write_array("/es", np.array([], np.float32), overwrite=True),
        "zero-length precision array",
    ),
    (
        "ps",
        lambda db, t, o: db.write_series(
            "/ps", TSeries(t.mm(2020, 1), [1.0, 2.0, 3.0]), overwrite=True
        ),
        "value at a missing observation",
    ),
    (
        "ps",
        lambda db, t, o: db.set_attribute("/ps", "famepy.migration.note", "x"),
        "undefined layout",
    ),
    (
        "ps",
        lambda db, t, o: db.set_attribute("/ps", "famepy.migration.mask", "2"),
        "undefined mask",
    ),
    ("wk", _undeclared_mask, "mask it does not declare"),
    (
        "pna",
        lambda db, t, o: db.set_attribute("/pna", "famepy.migration.missing", "XX"),
        "missing tag",
    ),
    ("pna", lambda db, t, o: db.write_scalar("/pna", 2.5, overwrite=True), "tagged missing but"),
    (
        "dt",
        lambda db, t, o: db.write_scalar("/dt", t.qq(2020, 1), overwrite=True),
        "value frequency",
    ),
    (
        "ds",
        lambda db, t, o: db.set_attribute("/ds", "famepy.migration.representation", "codes"),
        "moment codes",
    ),
    (
        "ss",
        lambda db, t, o: db.write_array("/ss", ["x", "y", "z"], overwrite=True),
        "text at a missing",
    ),
    ("b", lambda db, t, o: db.write_scalar("/b", 2.0, overwrite=True), "not a stored Boolean"),
    ("nl", lambda db, t, o: db.write_scalar("/nl", "A", overwrite=True), "scalar, not a array"),
]


@pytest.mark.parametrize("name,corrupt,message", CORRUPTIONS)
def test_structural_corruption_is_detected_with_attributes_restored(
    session, source, tmp_path, name, corrupt, message
):
    """Each corruption keeps the original attributes, so only the carrier disagrees."""
    destination = tmp_path / "s.daec"
    names = ("ps", "es", "wk", "pna", "dt", "ds", "ss", "b", "nl")
    migration.migrate(source, destination, patterns=names, session=session)
    with open_dataecon(destination, "a") as db:
        original = migration.read_migrated(db, name)
        attributes = db.get_attributes("/" + name)
        corrupt(db, ts, original)
        _restore(db, "/" + name, attributes)
    with open_dataecon(destination) as db:
        with pytest.raises(migration.LayoutError, match=message):
            migration.read_migrated(db, name)


def test_layout_version_and_labels_cannot_be_laundered(session, source, tmp_path):
    destination = tmp_path / "v.daec"
    migration.migrate(source, destination, patterns=("p",), session=session)
    with open_dataecon(destination, "a") as db:
        db.set_attribute("/", "famepy.migration.layout", "2")
    with open_dataecon(destination) as db:
        with pytest.raises(migration.LayoutError, match="layout '2'"):
            migration.read_migrated(db, "p")
        with pytest.raises(migration.LayoutError, match="layout '2'"):
            migration.list_migrated(db)
        with pytest.raises(migration.LayoutError, match="layout '2'"):
            migration.migration_status(db)
    plain = tmp_path / "plain.daec"
    with open_dataecon(plain, "w") as db:
        db.write_scalar("/p", 1.5)
    with open_dataecon(plain) as db:
        with pytest.raises(migration.LayoutError, match="lacks layout attributes"):
            migration.read_migrated(db, "p")
    # A migrated object cannot carry labels its carrier contradicts.
    series = TSeries(ts.mm(2020, 1), [1.0])
    with pytest.raises(migration.LayoutError, match="index frequency label"):
        migration.MigratedObject("x", "precision", "series", series, [0], "yearly")
    with pytest.raises(migration.LayoutError, match="one category per observation"):
        migration.MigratedObject("x", "precision", "series", series, [0, 0], "monthly")
    with pytest.raises(migration.LayoutError, match="value frequency label"):
        migration.MigratedObject("x", "date", "scalar", ts.mm(2020, 1), 0, None, "yearly")


def test_source_opens_read_only_and_never_posts(session, source, tmp_path):
    fake = session._native.fake
    fake.calls.clear()
    migration.migrate(source, tmp_path / "ro.daec", patterns=("p", "ps"), session=session)
    assert "cfmpodb" not in fake.calls and "cfmnwob" not in fake.calls
    assert fake.calls.count("cfmopdb") == 2  # plan, then the copy
    with pytest.raises(TypeError):
        migration.migrate(source, 3, session=session)


def test_expected_object_mirrors_the_layout():
    empty = migration.expected_object("e", None, empty_frequency=ts.Daily(), kind="string")
    assert empty.empty and empty.frequency == "daily" and empty.representation == "empty"
    with pytest.raises(ValueError):
        migration.expected_object("e", None, empty_frequency=ts.Daily())
    dates = migration.expected_object(
        "d", bridge.DateSeries(ts.mm(2020, 1), [ts.qq(2020, 1), None])
    )
    assert dates.representation == "codes" and list(dates.categories) == [0, 1]
    strings = migration.expected_object("s", bridge.StringSeries(ts.mm(2020, 1), [b"a", None]))
    assert strings.value.values == ("a", None) and strings.representation == "text"
    scalar = migration.expected_object("b", np.bool_(True))
    assert scalar.kind == "boolean" and scalar.value is True
    with pytest.raises(ValueError):
        migration.expected_object("x", object())
    with pytest.raises(ValueError):
        migration.expected_object("x", TSeries(ts.mm(2020, 1), [1.0]), categories=[0, 0])
    nan = migration.expected_object("f", TSeries(ts.mm(2020, 1), [np.nan]), missing="nan")
    assert list(nan.categories) == [0]


def test_example_script_migrates_and_verifies_synthetic_data(tmp_path):
    """The shipped example runs end to end against the injected backend."""
    example = Path(__file__).resolve().parent.parent / "examples" / "retire_synthetic.py"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = (
        str(Path(__file__).resolve().parent) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    environment.pop("FAME", None)
    result = subprocess.run(
        [
            sys.executable,
            str(example),
            "--scratch",
            str(tmp_path / "example"),
            "--backend",
            "fake_native:make_fake",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "9 to store" in result.stdout and "verified 9 of 9 objects" in result.stdout
    assert (tmp_path / "example" / "archive.daec").is_file()


@pytest.mark.parametrize("planned,written", [("2", "0"), ("0", "1")])
def test_complete_catalog_rejects_inconsistent_counts(session, source, tmp_path, planned, written):
    from famepy.migration._layout import attribute

    destination = tmp_path / "counts.daec"
    migration.migrate(source, destination, patterns=("p",), session=session)
    with open_dataecon(destination, "a") as db:
        db.set_attribute("/", attribute("planned"), planned)
        db.set_attribute("/", attribute("written"), written)
        with pytest.raises(migration.LayoutError, match="counts"):
            migration.migration_status(db)


def test_empty_carrier_rejects_multidimensional_payload(session, source, tmp_path):
    destination = tmp_path / "shape.daec"
    migration.migrate(source, destination, patterns=("es",), session=session)
    with open_dataecon(destination, "a") as db:
        attributes = db.get_attributes("/es")
        db.write_array("/es", np.empty((0, 3), dtype=np.float64), overwrite=True)
        for key, value in attributes.items():
            db.set_attribute("/es", key, value)
        with pytest.raises(migration.LayoutError):
            migration.read_migrated(db, "es")
