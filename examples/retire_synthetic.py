# SPDX-License-Identifier: MIT
"""Retire a synthetic FAME database into a DataEcon archive and verify it.

The script creates a small FAME database of every supported kind inside a
new scratch directory, plans and runs the migration with the default
(strict, mask) policies, reopens the archive and checks every object
against the values it wrote. Nothing outside the scratch directory is
touched. With an installed FAME::

    python examples/retire_synthetic.py --scratch <new-dir>

The runtime is initialized once per process (``famepy.init_chli``); the
``FAME`` environment variable must point at the installation. A test
backend can be injected for a dry run without FAME (see the tests).
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

import numpy as np
import tsecon as ts

import famepy
from famepy import bridge, migration


def synthetic_workspace() -> ts.Workspace:
    ws = ts.Workspace()
    ws["gdp"] = ts.TSeries(ts.qq(2015, 1), np.linspace(100.0, 150.0, 24))
    ws["cpi"] = ts.TSeries(ts.mm(2020, 1), [1.0, np.nan, 1.2, 1.3])
    ws["rate"] = np.float32(0.25)
    ws["release"] = ts.mm(2024, 6)
    ws["flag"] = True
    ws["source"] = "synthetic"
    ws["members"] = bridge.NameList(["GDP", "CPI"])
    ws["dates"] = bridge.DateSeries(ts.mm(2020, 1), [ts.daily("2020-01-31"), None])
    ws["labels"] = bridge.StringSeries(ts.MIT(ts.Unit(), 1), ["a", "b"])
    return ws


def expected_objects(ws: ts.Workspace) -> dict[str, migration.MigratedObject]:
    return {name: migration.expected_object(name, value) for name, value in ws.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scratch", required=True, help="a new directory for every file")
    parser.add_argument("--backend", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=False)
    if args.backend:
        module_name, _, factory = args.backend.partition(":")
        native = getattr(importlib.import_module(module_name), factory)()
        session = famepy.Session(native=native)
    else:
        session = famepy.init_chli()
    session.initialize()
    try:
        source = scratch / "synthetic.db"
        ws = synthetic_workspace()
        famepy.writefame(source, ws, mode="create")
        plan = migration.plan_migration(source, session=session)
        print(plan.summary())
        archive = scratch / "archive.daec"
        report = migration.migrate(source, archive, plan=plan, session=session)
        print(report.summary())
        if not report.complete:
            return 1
        from tsecon.dataecon import open_dataecon

        expected = expected_objects(ws)
        mismatches = 0
        with open_dataecon(archive) as db:
            print("archive status:", migration.migration_status(db))
            for name in migration.list_migrated(db):
                back = migration.read_migrated(db, name)
                if migration.describe(back) != migration.describe(expected[name]):
                    mismatches += 1
                    print("mismatch:", name)
        print(f"verified {len(expected) - mismatches} of {len(expected)} objects")
        return 1 if mismatches else 0
    finally:
        session.finalize()


if __name__ == "__main__":
    sys.exit(main())
