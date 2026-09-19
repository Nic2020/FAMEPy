# SPDX-License-Identifier: MIT
"""Migration qualification group: FAME source to DataEcon archive and back.

The fixture workspace covers every FAME kind as scalar and series, raw
missing categories (NC, NA and ND), a case-indexed series, a weekly index,
an empty series and date series with and without missing observations. It
is written to a synthetic FAME database, migrated with the default (strict,
mask) options, read back in this process and, through a nested child that
never touches FAME, in a fresh process; every object's description must
equal the description built from the fixture itself. Negative cases assert
that an existing destination (a foreign file, a finished archive or an
incomplete one), a refused plan, a name collision, a known-invalid first
date and a stale plan leave nothing created or changed, that the lossy
policy refuses what it cannot represent, that a contained failure marks
the archive ``incomplete``, and that structural corruptions (values,
shifted mask, wrong carrier frequency, payload in an empty carrier) are
detected in this process and in a fresh one.

When the DataEcon native extension is unavailable the group records that
environment block as a single blocked case and blocks every required case;
no fake write is substituted.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import numpy as np

import famepy
from famepy import bridge, migration
from famepy._constants import MISSING_NA, MISSING_ND
from famepy._data import sentinel_value

from ._bridge_groups import _block, contained_write
from ._report import Recorder

if TYPE_CHECKING:
    from ._groups import Context


def _tsecon() -> Any:
    import tsecon

    return tsecon


FIXTURE_NAMES: tuple[str, ...] = (
    "m_precision",
    "m_numeric",
    "m_boolean",
    "m_date",
    "m_string",
    "m_namelist",
    "m_pseries",
    "m_nseries",
    "m_bseries",
    "m_dates",
    "m_dates_missing",
    "m_strings",
    "m_empty",
    "m_case",
    "m_weekly",
    "m_business",
)
RAW_NAMES: tuple[str, ...] = ("m_precision_na", "m_date_nc", "m_pseries_nd", "m_bseries_na")
ALL_NAMES: tuple[str, ...] = FIXTURE_NAMES + RAW_NAMES
CORRUPTED_NAMES: tuple[str, ...] = ("m_pseries", "m_pseries_nd", "m_weekly", "m_empty")
NEGATIVE_CASES: tuple[str, ...] = (
    "existing_destination_refused",
    "existing_destination_unchanged",
    "refused_plan_creates_nothing",
    "refused_plan_no_file",
    "collision_refused",
    "collision_creates_nothing",
    "invalid_firstdate_refused",
    "invalid_firstdate_no_file",
    "stale_plan_refused",
    "stale_plan_no_file",
    "lossy_migrate",
    "lossy_policy_refuses_boolean",
    "lossy_loss_reported",
    "lossy_status_incomplete",
    "partial_migrate",
    "contained_failure_reported",
    "contained_status_incomplete",
    "incomplete_archive_refused",
    "incomplete_archive_unchanged",
    "incomplete_archive_status_kept",
    "finished_archive_refused",
    "finished_archive_unchanged",
    "corruption_detected",
    "shifted_mask_detected",
    "wrong_frequency_detected",
    "nonempty_empty_detected",
    "cross_process_corruption:status",
    *(f"cross_process_corruption:object:{name}" for name in CORRUPTED_NAMES),
)


def _required() -> tuple[str, ...]:
    ids = ["dataecon_available", "write_fixtures"]
    ids.extend(f"write:{name}" for name in ALL_NAMES)
    ids.extend(["plan", "plan_actions", "migrate", "migration_complete", "status_attributes"])
    ids.extend(f"readback:{name}" for name in ALL_NAMES)
    ids.append("cross_process_migration:status")
    ids.extend(f"cross_process_migration:object:{name}" for name in ALL_NAMES)
    ids.extend(NEGATIVE_CASES)
    ids.append("finalize")
    return tuple(ids)


MIGRATION_REQUIRED = _required()


def fixture_workspace() -> Any:
    """The bridge-valued fixtures (raw-category fixtures are written separately)."""
    ts = _tsecon()
    ws = ts.Workspace()
    ws["m_precision"] = 1.5
    ws["m_numeric"] = np.float32(2.5)
    ws["m_boolean"] = True
    ws["m_date"] = ts.mm(2020, 3)
    ws["m_string"] = "hello"
    ws["m_namelist"] = bridge.NameList(["A", "B"])
    ws["m_pseries"] = ts.TSeries(ts.mm(2020, 1), [1.0, np.nan, 3.0])
    ws["m_nseries"] = ts.TSeries(ts.qq(2020, 1), np.array([1.0, 2.5], dtype=np.float32))
    ws["m_bseries"] = ts.TSeries(ts.mm(2020, 1), [True, False])
    ws["m_dates"] = bridge.DateSeries(
        ts.mm(2020, 1), [ts.daily("2020-02-29"), ts.daily("2021-01-01")]
    )
    ws["m_dates_missing"] = bridge.DateSeries(
        ts.bdaily("2020-02-28"), [ts.qq(2020, 1), None, ts.qq(2021, 4)]
    )
    ws["m_strings"] = bridge.StringSeries(ts.MIT(ts.Unit(), 1), ["x", None, "z"])
    ws["m_empty"] = ts.TSeries(ts.mm(2020, 1), np.empty(0, dtype=np.float64))
    ws["m_case"] = ts.TSeries(ts.MIT(ts.Unit(), 3), [7.0, 8.0])
    ws["m_weekly"] = ts.TSeries(ts.weekly("2020-02-28", 7), [4.0, 5.0, 6.0])
    ws["m_business"] = ts.TSeries(ts.bdaily("2020-02-28"), [1.0, np.nan, 2.0])
    return ws


def raw_fixtures(sentinels: Any, first: int) -> list[tuple[str, Any, Any]]:
    """``(name, raw object, expected migrated object)`` for raw missing categories."""
    ts = _tsecon()
    na = sentinel_value("precision", MISSING_NA, sentinels)
    nd = sentinel_value("precision", MISSING_ND, sentinels)
    bna = sentinel_value("boolean", MISSING_NA, sentinels)
    monthly = ts.mm(2020, 1)
    return [
        (
            "m_precision_na",
            famepy.scalar("precision", na),
            migration.expected_object("m_precision_na", None, category=2, kind="precision"),
        ),
        (
            "m_date_nc",
            famepy.scalar("date", sentinels.index_nc, date_frequency="monthly"),
            migration.expected_object(
                "m_date_nc", None, category=1, kind="date", value_frequency=ts.Monthly()
            ),
        ),
        (
            "m_pseries_nd",
            famepy.series("precision", "monthly", first, np.array([1.0, nd, 3.0])),
            migration.expected_object(
                "m_pseries_nd", ts.TSeries(monthly, [1.0, np.nan, 3.0]), categories=[0, 3, 0]
            ),
        ),
        (
            "m_bseries_na",
            famepy.series("boolean", "monthly", first, np.array([1, bna, 0], dtype=np.int32)),
            migration.expected_object(
                "m_bseries_na", ts.TSeries(monthly, [True, False, False]), categories=[0, 2, 0]
            ),
        ),
    ]


def expected_objects(sentinels: Any, first: int) -> dict[str, Any]:
    ts = _tsecon()
    ws = fixture_workspace()
    expected: dict[str, Any] = {}
    for name, value in ws.items():
        if name == "m_empty":
            expected[name] = migration.expected_object(
                name, None, empty_frequency=ts.Monthly(), kind="precision"
            )
        else:
            expected[name] = migration.expected_object(name, value)
    for name, _raw, obj in raw_fixtures(sentinels, first):
        expected[name] = obj
    return expected


def _describe_or_none(db: Any, name: str, catalog: str = "/") -> Any:
    return migration.describe(migration.read_migrated(db, name, catalog=catalog))


def _corruption_detected(db: Any, name: str, catalog: str, description: Any) -> bool:
    """True when the object no longer reads back as the fixture describes it."""
    try:
        found = _describe_or_none(db, name, catalog)
    except migration.LayoutError:
        return True
    return bool(found != description)


def _restore_attributes(db: Any, path: str, attributes: dict[str, str]) -> None:
    """Put back the attributes a carrier rewrite dropped, so only the carrier disagrees."""
    current = db.get_attributes(path)
    for key, value in attributes.items():
        if key not in current:
            db.set_attribute(path, key, value)


def group_migration(ctx: Context) -> None:
    from tsecon.dataecon import open_dataecon

    session, r = ctx.session, ctx.recorder
    available, block = migration.dataecon_available()
    if not available:
        r.blocked("dataecon_available", f"DataEcon native extension unavailable: {block}")
        _block(r, list(MIGRATION_REQUIRED))
        return
    r.equal("dataecon_available", available, True)
    session.initialize()
    sentinels = session.sentinels
    first = ctx.first
    source = ctx.path("source.db")
    written = contained_write(
        r,
        "write_fixtures",
        source,
        fixture_workspace(),
        case_of="write:{}".format,
        mode="create",
    )
    with famepy.open_database(source, "update", session=session) as database:
        for name, raw, _expected in raw_fixtures(sentinels, first):
            if r.ok(f"write:{name}", partial(famepy.write_object, database, name, raw)):
                written.add(name)
        database.post()
    expected = expected_objects(sentinels, first)
    present = {w.upper() for w in written}
    missing_objects = [name for name in ALL_NAMES if name.upper() not in present]
    dependents = [f"readback:{n}" for n in missing_objects] + [
        f"cross_process_migration:object:{n}" for n in missing_objects
    ]
    _block(r, dependents)

    plan = r.check("plan", lambda: migration.plan_migration(source, session=session))
    if plan is None:
        _block(r, list(MIGRATION_REQUIRED))
        r.check("finalize", session.finalize)
        return
    r.equal(
        "plan_actions",
        [len(plan.refused), len(plan.skipped), sorted(e.name for e in plan.stored)],
        [0, 0, sorted(n.upper() for n in ALL_NAMES if n not in missing_objects)],
    )
    destination = ctx.path("archive.daec")
    report = r.check(
        "migrate", lambda: migration.migrate(source, destination, plan=plan, session=session)
    )
    if report is None:
        _block(r, list(MIGRATION_REQUIRED))
        r.check("finalize", session.finalize)
        return
    r.equal(
        "migration_complete",
        [report.status, report.complete, len(report.failed), len(report.stored)],
        ["complete", True, 0, len(plan.stored)],
    )
    with open_dataecon(destination) as db:
        status = migration.migration_status(db)
        r.equal(
            "status_attributes",
            [
                status.get("layout"),
                status.get("status"),
                status.get("planned"),
                status.get("written"),
            ],
            [migration.LAYOUT_VERSION, "complete", str(len(plan.stored)), str(len(plan.stored))],
        )
        for name in ALL_NAMES:
            if name in missing_objects:
                continue
            r.expect(
                f"readback:{name}",
                partial(_describe_or_none, db, name),
                migration.describe(expected[name]),
            )
    manifest = {
        "database": str(destination),
        "catalog": "/",
        "status": "complete",
        "objects": [
            {"name": name, "description": migration.describe(expected[name])}
            for name in ALL_NAMES
            if name not in missing_objects
        ],
    }
    ctx.verify_migration_in_new_process("cross_process_migration", manifest)
    negative_cases(ctx, source, destination, expected)
    r.check("finalize", session.finalize)


def negative_cases(ctx: Context, source: Any, destination: Any, expected: dict[str, Any]) -> None:
    from tsecon.dataecon import open_dataecon

    ts = _tsecon()
    session, r = ctx.session, ctx.recorder
    refused = (migration.MigrationRefused,)
    # An existing destination file is never opened or touched.
    existing = ctx.path("existing.daec")
    existing.write_bytes(b"not a database, left alone")
    before = existing.read_bytes()
    r.expect_error(
        "existing_destination_refused",
        lambda: migration.migrate(source, existing, session=session),
        refused,
    )
    r.equal("existing_destination_unchanged", existing.read_bytes() == before, True)
    # A refused plan creates no destination: an unsupported frequency in the source.
    unsupported = ctx.path("unsupported.db")
    with famepy.open_database(unsupported, "create", session=session) as database:
        famepy.write_object(
            database, "u_tenday", famepy.series("precision", "tenday", 1, np.array([1.0]))
        )
        famepy.write_object(database, "u_ok", famepy.scalar("precision", 2.0))
        database.post()
    absent = ctx.path("absent.daec")
    r.expect_error(
        "refused_plan_creates_nothing",
        lambda: migration.migrate(unsupported, absent, session=session),
        refused,
    )
    r.equal("refused_plan_no_file", absent.exists(), False)
    # Two objects on one destination name are both refused before anything is created.
    collide = migration.MigrationOptions(namecase=lambda name: "same")
    r.expect_error(
        "collision_refused",
        lambda: migration.migrate(source, absent, options=collide, session=session),
        refused,
    )
    r.equal("collision_creates_nothing", absent.exists(), False)
    # A known-invalid input (an explicit first date of another frequency) is
    # refused by the plan, before any file exists.
    wrong = migration.MigrationOptions(empty_firstdates={"m_empty": ts.qq(2020, 1)})
    r.expect_error(
        "invalid_firstdate_refused",
        lambda: migration.migrate(
            source, absent, patterns=("m_empty", "m_precision"), options=wrong, session=session
        ),
        refused,
    )
    r.equal("invalid_firstdate_no_file", absent.exists(), False)
    # A plan built before the source changed is refused, not trusted.
    stale_source = ctx.path("stale.db")
    with famepy.open_database(stale_source, "create", session=session) as database:
        famepy.write_object(database, "s_first", famepy.scalar("precision", 1.0))
        database.post()
    stale_plan = migration.plan_migration(stale_source, session=session)
    with famepy.open_database(stale_source, "update", session=session) as database:
        famepy.write_object(database, "s_second", famepy.scalar("precision", 2.0))
        database.post()
    r.expect_error(
        "stale_plan_refused",
        lambda: migration.migrate(stale_source, absent, plan=stale_plan, session=session),
        refused,
    )
    r.equal("stale_plan_no_file", absent.exists(), False)
    # The lossy policy refuses a Boolean series with missing observations and
    # marks the archive incomplete; floating missing categories collapse to NaN.
    lossy = migration.MigrationOptions(missing="nan")
    lossy_path = ctx.path("lossy.daec")
    lossy_report = r.check(
        "lossy_migrate",
        lambda: migration.migrate(
            source,
            lossy_path,
            patterns=("m_bseries_na", "m_pseries_nd"),
            options=lossy,
            session=session,
        ),
    )
    if lossy_report is not None:
        outcome = {entry.name: (entry.action, entry.error_type) for entry in lossy_report.entries}
        r.equal(
            "lossy_policy_refuses_boolean",
            outcome.get("M_BSERIES_NA"),
            ["failed", "MigrationLossError"],
        )
        losses = [entry.losses for entry in lossy_report.entries if entry.name == "M_PSERIES_ND"]
        r.equal("lossy_loss_reported", bool(losses and losses[0]), True)
        with open_dataecon(lossy_path) as db:
            r.equal(
                "lossy_status_incomplete",
                [lossy_report.status, migration.migration_status(db).get("status")],
                ["incomplete", "incomplete"],
            )
    else:
        _block(
            r, ["lossy_policy_refuses_boolean", "lossy_loss_reported", "lossy_status_incomplete"]
        )
    # A contained per-object failure (a string that cannot cross the ASCII
    # boundary) is reported and leaves the archive marked incomplete; a later
    # run can neither complete nor change that archive.
    text_source = ctx.path("text.db")
    with famepy.open_database(text_source, "create", session=session) as database:
        famepy.write_object(database, "t_bad", famepy.scalar("string", b"caf\xe9"))
        famepy.write_object(database, "t_good", famepy.scalar("string", b"cafe"))
        database.post()
    partial_path = ctx.path("partial.daec")
    partial = r.check(
        "partial_migrate",
        lambda: migration.migrate(
            text_source, partial_path, patterns=("t_bad", "t_good"), session=session
        ),
    )
    if partial is not None:
        outcome = {entry.name: entry.action for entry in partial.entries}
        r.equal(
            "contained_failure_reported",
            [outcome.get("T_BAD"), outcome.get("T_GOOD"), partial.complete],
            ["failed", "stored", False],
        )
        with open_dataecon(partial_path) as db:
            status = migration.migration_status(db)
            r.equal(
                "contained_status_incomplete",
                [status.get("status"), status.get("written"), status.get("planned")],
                ["incomplete", "1", "2"],
            )
        before = partial_path.read_bytes()
        r.expect_error(
            "incomplete_archive_refused",
            lambda: migration.migrate(
                text_source, partial_path, patterns=("t_good",), session=session
            ),
            refused,
        )
        r.equal("incomplete_archive_unchanged", partial_path.read_bytes() == before, True)
        with open_dataecon(partial_path) as db:
            r.equal(
                "incomplete_archive_status_kept",
                migration.migration_status(db).get("status"),
                "incomplete",
            )
    else:
        _block(
            r,
            [
                "contained_failure_reported",
                "contained_status_incomplete",
                "incomplete_archive_refused",
                "incomplete_archive_unchanged",
                "incomplete_archive_status_kept",
            ],
        )
    # The finished archive is not a destination for any further run.
    before = destination.read_bytes()
    r.expect_error(
        "finished_archive_refused",
        lambda: migration.migrate(source, destination, patterns=("m_precision",), session=session),
        refused,
    )
    r.equal("finished_archive_unchanged", destination.read_bytes() == before, True)
    # Structural corruptions through the public writers, each with the
    # original attributes kept, are detected here and in a fresh process.
    with open_dataecon(destination, "a") as db:
        kept = {name: db.get_attributes("/" + name) for name in CORRUPTED_NAMES}
        db.write_series(
            "/m_pseries", ts.TSeries(ts.mm(2020, 1), [1.0, np.nan, 4.0]), overwrite=True
        )
        db.write_series(
            "/" + migration.MASK_CATALOG + "/m_pseries_nd",
            ts.TSeries(ts.mm(1990, 1), np.array([0, 3, 0], np.int8)),
            overwrite=True,
        )
        weekly = db.read_series("/m_weekly")
        db.write_series(
            "/m_weekly",
            ts.TSeries(ts.MIT(ts.Yearly(), int(weekly.firstdate)), weekly.values.copy()),
            overwrite=True,
        )
        db.write_array("/m_empty", np.array([99.0]), overwrite=True)
        for name in CORRUPTED_NAMES:
            _restore_attributes(db, "/" + name, kept[name])
    with open_dataecon(destination) as db:
        r.expect(
            "corruption_detected",
            lambda: _describe_or_none(db, "m_pseries") == migration.describe(expected["m_pseries"]),
            False,
        )
        r.expect_error(
            "shifted_mask_detected",
            lambda: migration.read_migrated(db, "m_pseries_nd"),
            (migration.LayoutError,),
        )
        r.expect_error(
            "wrong_frequency_detected",
            lambda: migration.read_migrated(db, "m_weekly"),
            (migration.LayoutError,),
        )
        r.expect_error(
            "nonempty_empty_detected",
            lambda: migration.read_migrated(db, "m_empty"),
            (migration.LayoutError,),
        )
    manifest = {
        "database": str(destination),
        "catalog": "/",
        "status": "complete",
        "objects": [
            {"name": name, "description": migration.describe(expected[name]), "corrupted": True}
            for name in CORRUPTED_NAMES
        ],
    }
    ctx.verify_migration_in_new_process("cross_process_corruption", manifest)


def run_verify_migration(manifest: dict[str, Any], recorder: Recorder) -> None:
    """Child-side check of a DataEcon archive against fixture descriptions.

    Runs without FAME: the archive is opened read-only, the layout status is
    compared, and every listed object's description must equal the
    description the parent built from its fixture; an object the manifest
    marks ``corrupted`` must instead fail to read back as that description.
    """
    from tsecon.dataecon import open_dataecon

    catalog = manifest["catalog"]
    with open_dataecon(manifest["database"]) as db:
        recorder.expect(
            "status",
            lambda: migration.migration_status(db, catalog=catalog).get("status"),
            manifest["status"],
        )
        for entry in manifest["objects"]:
            name = entry["name"]
            if entry.get("corrupted"):
                recorder.expect(
                    f"object:{name}",
                    partial(_corruption_detected, db, name, catalog, entry["description"]),
                    True,
                )
                continue
            recorder.expect(
                f"object:{name}",
                partial(_describe_or_none, db, name, catalog),
                entry["description"],
            )


__all__ = [
    "ALL_NAMES",
    "CORRUPTED_NAMES",
    "FIXTURE_NAMES",
    "MIGRATION_REQUIRED",
    "NEGATIVE_CASES",
    "RAW_NAMES",
    "expected_objects",
    "fixture_workspace",
    "group_migration",
    "raw_fixtures",
    "run_verify_migration",
]
