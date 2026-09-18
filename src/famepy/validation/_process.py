# SPDX-License-Identifier: MIT
"""Child process execution with a timeout that terminates the whole tree.

Workers report through a *result file* that the launcher reserves, never
through their standard streams: the native library may write to the C-level
stdout/stderr at any time (for example terminal output when a redirection is
not active), and a report parsed from a pipe could be contaminated. The child
redirects both descriptors to a local log inside its scratch directory, writes
its JSON result to a temporary file and renames it into place, and echoes a
token the launcher chose. A missing, unreadable, malformed, partial, stale or
wrong-group result is reported as such and can never pass.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str


@dataclass
class WorkerResult:
    """What the launcher learned about a worker, with the raw streams dropped."""

    returncode: int
    payload: dict[str, Any] | None = None
    result_kind: str | None = None
    pipe_bytes: int = 0
    log_bytes: int = 0
    tokens: dict[str, str] = field(default_factory=dict)


def _kill_tree(process: subprocess.Popen[bytes], *, owns_group: bool = True) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif owns_group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def run_child(
    command: list[str],
    input_text: str,
    timeout: float,
    *,
    nested: bool = False,
    output_path: Path | None = None,
) -> Completed:
    """Run ``command`` with ASCII stdin; on timeout kill it and its descendants.

    The top-level worker owns the process group. Nested verification workers
    (which do not spawn further workers) stay in that group so the outer timeout
    also reaches them. A nested timeout kills only that verification worker on
    POSIX; the outer group remains responsible for any library-created children.
    With ``output_path`` the child's stdout and stderr go to that local file
    (a bounded diagnostic channel that is never parsed) and the returned
    ``stdout`` is empty.
    """
    # A new process group (Windows) or session (POSIX) lets the timeout
    # path terminate every descendant, not just the immediate child.
    sink = None if output_path is None else open(output_path, "ab")  # noqa: SIM115
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if sink is None else sink,
            stderr=subprocess.PIPE if sink is None else sink,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=sys.platform != "win32" and not nested,
        )
    finally:
        if sink is not None:
            sink.close()
    try:
        stdout, _stderr = process.communicate(input_text.encode("ascii"), timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process, owns_group=not nested)
        try:
            process.communicate(timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise
    text = "" if stdout is None else stdout.decode("ascii", errors="replace")
    return Completed(process.returncode, text)


def reserve_result(directory: Path, label: str) -> dict[str, str]:
    """Reserve the result file, local log and token for one worker launch.

    The token is fresh per launch, so a result left behind by an earlier
    worker (even one for the same group) is stale and rejected.
    """
    token = uuid.uuid4().hex
    stem = directory / f"{label}-{token[:8]}"
    return {
        "result": str(stem.with_suffix(".result.json")),
        "log": str(stem.with_suffix(".log")),
        "token": token,
    }


def launch_worker(
    command: list[str],
    config: dict[str, Any],
    timeout: float,
    *,
    tokens: dict[str, str],
    nested: bool = False,
) -> WorkerResult:
    """Run a worker and read its result file; the pipes are counted, never parsed.

    ``tokens`` comes from ``reserve_result`` and is merged into the configuration
    the worker receives on stdin. ``subprocess.TimeoutExpired`` and ``OSError``
    propagate for the caller to classify.
    """
    worker_config = {**config, **tokens}
    completed = run_child(command, json.dumps(worker_config), timeout, nested=nested)
    result = WorkerResult(completed.returncode, tokens=tokens)
    result.pipe_bytes = len(completed.stdout)
    try:
        result.log_bytes = Path(tokens["log"]).stat().st_size
    except OSError:
        result.log_bytes = 0
    result.payload, result.result_kind = read_result(Path(tokens["result"]), tokens["token"])
    return result


MAX_RESULT_BYTES = 4 * 1024 * 1024


def read_result(path: Path, token: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load a worker result file; the second item names why it is unusable."""
    try:
        if path.stat().st_size > MAX_RESULT_BYTES:
            return None, "oversized_result"
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "missing_result"
    except OSError:
        return None, "unreadable_result"
    try:
        payload = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None, "invalid_result"
    if not isinstance(payload, dict):
        return None, "invalid_result"
    if payload.get("token") != token:
        return None, "stale_result"
    if payload.get("complete") is not True:
        return None, "partial_result"
    return payload, None


def write_result(config: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Child side: write the result atomically with the launcher's token.

    Returns False when the configuration carries no result path (a worker run
    by hand); the caller then prints the payload instead.
    """
    target = config.get("result")
    token = config.get("token")
    if not isinstance(target, str) or not isinstance(token, str):
        return False
    path = Path(target)
    document = {**payload, "token": token, "complete": True}
    text = json.dumps(document, sort_keys=True)
    text.encode("ascii")  # a result is ASCII by construction; anything else is a bug
    temporary = path.with_name(path.name + ".part")
    with open(temporary, "w", encoding="ascii", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return True


def redirect_streams(config: dict[str, Any]) -> None:
    """Child side: send the C-level stdout/stderr to the local log file.

    Everything the native library or the interpreter prints stays in the
    scratch directory; the launcher only counts its size.
    """
    target = config.get("log")
    if not isinstance(target, str):
        return
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        os.close(descriptor)
    sys.stdout = open(1, "w", encoding="ascii", errors="replace", closefd=False)  # noqa: SIM115
    sys.stderr = open(2, "w", encoding="ascii", errors="replace", closefd=False)  # noqa: SIM115


__all__ = [
    "MAX_RESULT_BYTES",
    "Completed",
    "WorkerResult",
    "launch_worker",
    "read_result",
    "redirect_streams",
    "reserve_result",
    "run_child",
    "write_result",
]
