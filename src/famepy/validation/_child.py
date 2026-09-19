# SPDX-License-Identifier: MIT
"""Child entry point: run one validation group in an isolated process.

Configuration arrives as JSON on stdin (library path, trusted root, backend
factory, scratch directory, Julia settings, result path, log path, token).
The result is one JSON document written to the result path with sanitized
cases only; the parent validates every field again. The child's stdout and
stderr descriptors are redirected to the log path before anything native
runs, so text the library prints never mixes with the result. Without a
result path (a worker run by hand) the document is printed instead.

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

from ._process import redirect_streams, write_result
from ._report import Recorder


def _build_session(config: dict[str, Any], share: Any = None) -> Any:
    """A new Session wrapper from the configuration; never initializes it."""
    from famepy._runtime import Session

    factory = config.get("backend")
    if factory:
        if share is not None:
            return Session(native=share._native)
        module_name, _, attribute = factory.partition(":")
        module = importlib.import_module(module_name)
        native = getattr(module, attribute)()
        return Session(native=native)
    from famepy._discovery import discover

    candidate = discover(config.get("library"), root=config.get("root"))
    return Session(candidate)


def _deliver(config: dict[str, Any], payload: dict[str, Any]) -> None:
    if not write_result(config, payload):
        print(json.dumps(payload, sort_keys=True))


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
        redirect_streams(config)
    except (ValueError, KeyError, IndexError, TypeError, OSError):
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
        _deliver(
            config,
            {
                "group": group,
                "setup_error": type(error).__name__,
                "status_code": getattr(error, "status", None),
                "errno": getattr(error, "errno", None),
                "winerror": getattr(error, "winerror", None),
            },
        )
        return 31
    from ._groups import GROUP_FUNCTIONS, Context, run_verify

    def child_command(extra: list[str]) -> list[str]:
        return [sys.executable, "-m", "famepy.validation._child", *extra]

    julia = config.get("julia")
    context = Context(session, scratch, recorder, child_command, timeout, julia, config)
    # A second wrapper over the same configuration (the same library candidate,
    # or for injected backends the same backend instance) for rejection checks.
    context.new_session = lambda: _build_session(config, share=session)
    exit_code = 0
    try:
        if group == "verify":
            manifest_path = Path(argv[argv.index("--manifest") + 1])
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            session.initialize()
            run_verify(session, manifest, recorder)
        elif group == "verify_migration":
            from ._migration_group import run_verify_migration

            manifest_path = Path(argv[argv.index("--manifest") + 1])
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            run_verify_migration(manifest, recorder)
        else:
            GROUP_FUNCTIONS[group](context)
    except (ValueError, KeyError, IndexError, OSError) as error:
        if group in ("verify", "verify_migration"):
            exit_code = 33
        else:
            recorder.add(_outside(error))
            exit_code = 32
    except Exception as error:  # noqa: BLE001
        recorder.add(_outside(error))
        exit_code = 32
    finally:
        try:
            if session.is_initialized:
                session.finalize()
        except Exception as error:  # noqa: BLE001
            recorder.add(_outside(error, "finalize_in_cleanup"))
    payload = {
        "group": group,
        "cases": [case.to_json() for case in recorder.cases],
        "counts": recorder.counts(),
        "pid": os.getpid(),
    }
    _deliver(config, payload)
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
