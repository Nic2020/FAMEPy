# SPDX-License-Identifier: MIT
"""``python -m famepy.validation``: the single native validation entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import GROUPS, run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m famepy.validation",
        description="Run the consolidated FAMEPy validation campaign and write a sanitized report.",
    )
    parser.add_argument(
        "--scratch",
        required=True,
        help="a new or empty directory; a fresh run directory is reserved inside it",
    )
    parser.add_argument("--report", required=True, help="output JSON report path")
    parser.add_argument("--groups", help="comma-separated subset of: " + ",".join(GROUPS))
    parser.add_argument("--list", action="store_true", help="list groups and exit")
    parser.add_argument(
        "--native",
        action="store_true",
        help="explicit opt-in to run native groups against the library",
    )
    parser.add_argument("--library", help="absolute trusted CHLI library path")
    parser.add_argument("--root", help="trusted installation root for native dependencies")
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="per-operation timeout in seconds (children get four times this)",
    )
    parser.add_argument(
        "--wheel", help="the installed wheel file, hashed and compared with the imported package"
    )
    parser.add_argument(
        "--source-sha", help="hexadecimal source revision (optionally suffixed -dirty)"
    )
    parser.add_argument(
        "--abi-attestation",
        help="SHA-256 of the private per-row ABI checklist review record for this host",
    )
    parser.add_argument("--julia", help="Julia executable for differential checks")
    parser.add_argument("--julia-project", help="Julia project containing FAME.jl")
    parser.add_argument("--backend", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(GROUPS))
        return 0
    if not 0 < args.timeout <= 3600:
        parser.error("timeout must be between 0 and 3600 seconds")
    if (args.julia is None) != (args.julia_project is None):
        parser.error("--julia and --julia-project must be given together")
    options = {
        "scratch": args.scratch,
        "groups": args.groups.split(",") if args.groups else None,
        "native": args.native,
        "library": args.library,
        "root": args.root,
        "timeout": args.timeout,
        "wheel": Path(args.wheel) if args.wheel else None,
        "source_sha": args.source_sha,
        "abi_attestation": args.abi_attestation,
        "julia": {"executable": args.julia, "project": args.julia_project} if args.julia else None,
        "backend": args.backend,
    }
    try:
        report = run(options)
    except ValueError as error:
        parser.error(str(error))
    Path(args.report).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="ascii"
    )
    summary = report["summary"]
    print(f"result: {report['result']}")
    for name, group in report["groups"].items():
        counts = group.get("counts") or {}
        detail = (
            f"{counts.get('pass', 0)} pass, {counts.get('fail', 0)} fail"
            if counts
            else group.get("note") or group.get("exit_kind", "")
        )
        print(f"  {name}: {group['status']} ({detail})")
    print(f"groups: {summary['pass']} pass, {summary['fail']} fail, {summary['blocked']} blocked")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
