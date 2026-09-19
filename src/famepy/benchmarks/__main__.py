# SPDX-License-Identifier: MIT
"""``python -m famepy.benchmarks``: bounded native benchmarks with a JSON report.

Exit status 0 only when every requested measurement completed and was
accepted; 1 when the run is blocked or any measurement failed, timed out,
was blocked or was rejected (the report lists each one under ``failures``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import SCALES, SCENARIOS, run, worker_main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m famepy.benchmarks",
        description="Time FAMEPy's read/write paths on synthetic data and write a JSON report.",
    )
    parser.add_argument("--scratch", help="a new or empty directory")
    parser.add_argument("--report", help="output JSON report path")
    parser.add_argument("--native", action="store_true", help="explicit opt-in to use the library")
    parser.add_argument("--library", help="absolute trusted CHLI library path")
    parser.add_argument("--root", help="trusted installation root for native dependencies")
    parser.add_argument("--scale", choices=sorted(SCALES), default="small")
    parser.add_argument("--repetitions", type=int, help="timed passes per scenario")
    parser.add_argument("--scenarios", help="comma-separated subset of: " + ",".join(SCENARIOS))
    parser.add_argument("--no-cold", action="store_true", help="skip the fresh-process runs")
    parser.add_argument("--profile", action="store_true", help="write cProfile stats per scenario")
    parser.add_argument("--timeout", type=float, default=600.0, help="per worker, in seconds")
    parser.add_argument("--wheel", help="the installed wheel, compared with the imported package")
    parser.add_argument("--source-sha", help="hexadecimal source revision (optionally -dirty)")
    parser.add_argument("--julia", help="Julia executable for the same-host FAME.jl comparison")
    parser.add_argument("--julia-project", help="Julia project containing FAME.jl")
    parser.add_argument(
        "--julia-selfcheck",
        action="store_true",
        help="also run the Julia negative verification cases at the small scale",
    )
    parser.add_argument("--backend", help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.worker:
        try:
            config = json.load(sys.stdin)
        except ValueError:
            return 30
        return worker_main(config) if isinstance(config, dict) else 30
    if not args.scratch or not args.report:
        parser.error("--scratch and --report are required")
    if (args.julia is None) != (args.julia_project is None):
        parser.error("--julia and --julia-project must be given together")
    options = {
        "scratch": args.scratch,
        "native": args.native,
        "library": args.library,
        "root": args.root,
        "scale": args.scale,
        "repetitions": args.repetitions,
        "scenarios": args.scenarios.split(",") if args.scenarios else None,
        "cold": not args.no_cold,
        "profile": args.profile,
        "timeout": args.timeout,
        "wheel": Path(args.wheel) if args.wheel else None,
        "source_sha": args.source_sha,
        "julia": {"executable": args.julia, "project": args.julia_project} if args.julia else None,
        "julia_selfcheck": args.julia_selfcheck,
        "backend": args.backend,
    }
    try:
        payload = run(options)
    except ValueError as error:
        parser.error(str(error))
    Path(args.report).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    if payload.get("blocked"):
        print(f"blocked: {payload['blocked']}")
        return 1
    print(f"scale: {payload['scale']}, repetitions: {payload['repetitions']}")
    print(f"vendor timing: {payload['library'].get('vendor_timing')}")
    for mode in ("warm", "cold"):
        for name, record in payload.get(mode, {}).items():
            if "error" in record:
                print(f"  {mode} {name}: failed ({record['error']})")
            elif "blocked" in record:
                print(f"  {mode} {name}: blocked ({record['blocked']})")
            else:
                phases = ", ".join(
                    f"{phase} {values['seconds']['median']}s"
                    for phase, values in record["phases"].items()
                )
                print(f"  {mode} {name}: {phases}")
    julia = payload.get("julia")
    if julia is not None:
        print("  julia: " + ("failed (" + julia["error"] + ")" if "error" in julia else "verified"))
    for case, outcome in payload.get("julia_selfcheck", {}).items():
        print(f"  julia negative {case}: {outcome['outcome']}")
    print(f"verified pairs: {len(payload['comparison']['verified_pairs'])}")
    print(f"result: {payload['result']} ({len(payload['failures'])} failed measurement(s))")
    return 0 if payload["result"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
