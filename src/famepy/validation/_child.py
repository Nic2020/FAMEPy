# SPDX-License-Identifier: MIT
"""Child entry point: run one validation group in an isolated process.

Configuration arrives as JSON on stdin (library path, trusted root, backend
factory, scratch directory, Julia settings). The result is one JSON document
on stdout with sanitized cases only; the parent validates every field again.
Exit codes: 0 completed (cases may still fail), 30 configuration invalid, 31
backend construction failed, 32 group raised outside the recorder, 33
manifest invalid.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from ._report import Recorder


def _build_session(config: dict[str, Any]) -> Any:
    from famepy._runtime import Session

    factory = config.get("backend")
    if factory:
        module_name, _, attribute = factory.partition(":")
        module = importlib.import_module(module_name)
        native = getattr(module, attribute)()
        return Session(native=native)
    from famepy._discovery import discover

    candidate = discover(config.get("library"), root=config.get("root"))
    return Session(candidate)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        config = json.load(sys.stdin)
        if not isinstance(config, dict):
            raise ValueError
        group = argv[argv.index("--group") + 1]
        scratch = Path(config["scratch"])
        timeout = float(config.get("timeout", 120.0))
        if not scratch.is_dir():
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        return 30
    try:
        from ._probe_dialogs import suppress_error_dialogs

        suppress_error_dialogs()
    except Exception:  # noqa: BLE001 - dialogs are a convenience only
        pass
    recorder = Recorder()
    try:
        session = _build_session(config)
    except Exception as error:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "group": group,
                    "setup_error": type(error).__name__,
                    "status_code": getattr(error, "status", None),
                    "errno": getattr(error, "errno", None),
                    "winerror": getattr(error, "winerror", None),
                }
            )
        )
        return 31
    from ._groups import GROUP_FUNCTIONS, Context, run_verify

    def child_command(extra: list[str]) -> list[str]:
        return [sys.executable, "-m", "famepy.validation._child", *extra]

    julia = config.get("julia")
    context = Context(session, scratch, recorder, child_command, timeout, julia, config)
    exit_code = 0
    try:
        if group == "verify":
            manifest_path = Path(argv[argv.index("--manifest") + 1])
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            session.initialize()
            run_verify(session, manifest, recorder)
        else:
            GROUP_FUNCTIONS[group](context)
    except (ValueError, KeyError, IndexError, OSError) as error:
        if group == "verify":
            exit_code = 33
        else:
            recorder.add(_outside(error))
            exit_code = 32
    except Exception as error:  # noqa: BLE001
        recorder.add(_outside(error))
        exit_code = 32
    finally:
        try:
            if session.state in ("initialized", "broken"):
                session.finalize()
        except Exception as error:  # noqa: BLE001
            recorder.add(_outside(error, "finalize_in_cleanup"))
    payload = {
        "group": group,
        "cases": [case.to_json() for case in recorder.cases],
        "counts": recorder.counts(),
        "pid": os.getpid(),
    }
    print(json.dumps(payload, sort_keys=True))
    return exit_code


def _outside(error: BaseException, case_id: str = "group_exception") -> Any:
    from ._report import Case, _package_frames

    return Case(
        case_id,
        "fail",
        error_type=type(error).__name__,
        status_code=getattr(error, "status", None)
        if isinstance(getattr(error, "status", None), int)
        else None,
        frames=_package_frames(error),
    )


if __name__ == "__main__":
    raise SystemExit(main())
