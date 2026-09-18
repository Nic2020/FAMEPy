# SPDX-License-Identifier: MIT
"""Child process execution with a timeout that terminates the whole tree."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str


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
    command: list[str], input_text: str, timeout: float, *, nested: bool = False
) -> Completed:
    """Run ``command`` with ASCII stdin; on timeout kill it and its descendants.

    The top-level worker owns the process group. Nested verification workers
    (which do not spawn further workers) stay in that group so the outer timeout
    also reaches them. A nested timeout kills only that verification worker on
    POSIX; the outer group remains responsible for any library-created children.
    """
    # A new process group (Windows) or session (POSIX) lets the timeout
    # path terminate every descendant, not just the immediate child.
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        start_new_session=sys.platform != "win32" and not nested,
    )
    try:
        stdout, _stderr = process.communicate(input_text.encode("ascii"), timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process, owns_group=not nested)
        try:
            process.communicate(timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise
    return Completed(process.returncode, stdout.decode("ascii", errors="replace"))


__all__ = ["Completed", "run_child"]
