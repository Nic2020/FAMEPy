# SPDX-License-Identifier: MIT
"""Command-line diagnostics. No database operation is implemented here."""

import argparse
import json
import sys

from .diagnostics import diagnose


def main() -> int:
    if sys.argv[1:] == ["_probe"]:
        from ._probe import main as probe_main

        return probe_main()
    parser = argparse.ArgumentParser(description="FAMEPy discovery (does not initialize FAME)")
    parser.add_argument("--probe", action="store_true", help="load trusted CHLI in a subprocess")
    parser.add_argument("--library", help="absolute trusted CHLI library path")
    parser.add_argument("--root", help="trusted installation root for native dependencies")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    try:
        report = diagnose(args.library, root=args.root, probe=args.probe, timeout=args.timeout)
    except ValueError:
        parser.error("timeout must be greater than zero and at most 300 seconds")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"library_found", "symbols_found"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
