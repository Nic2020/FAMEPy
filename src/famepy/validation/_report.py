# SPDX-License-Identifier: MIT
"""Sanitized case records for the validation runner.

A case never stores exception text, paths, native output or command payloads:
only its own identifier, a status, the error class name, a numeric CHLI status
when one exists, an OS error number when one exists, and synthetic expected/
actual values produced by the runner itself. ``check`` records only whether a
callable raised; a return value is never recorded. Assertions go through
``equal`` and observations through ``fact``, so a predicate can never pass by
merely not raising.
"""

from __future__ import annotations

import math
import struct
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

STATUSES = ("pass", "fail", "blocked", "unsupported")


@dataclass
class Case:
    id: str
    status: str
    error_type: str | None = None
    status_code: int | None = None
    errno: int | None = None
    expected: Any = None
    actual: Any = None
    note: str | None = None
    frames: list[str] = field(default_factory=list)
    observation: bool = False

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "status": self.status}
        for key in ("error_type", "status_code", "errno", "expected", "actual", "note"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.frames:
            data["frames"] = self.frames
        if self.observation:
            data["observation"] = True
        return data


def _float_record(value: Any) -> Any:
    """Finite floats stay numbers; anything else keeps its exact bit pattern."""
    typed = np.asarray(value)
    number = float(typed)
    if math.isfinite(number):
        return number
    return {"bits": typed.tobytes().hex()}


def encode_value(value: Any) -> Any:
    """Make synthetic values JSON-safe; non-finite floats keep exact bits."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, (float, np.floating)):
        return _float_record(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, bytes):
        return {"ascii": value.decode("ascii", errors="backslashreplace")}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        if value.dtype.kind == "f":
            return [_float_record(item) for item in value]
        return encode_value(value.tolist())
    return repr(type(value).__name__)


def _package_frames(error: BaseException) -> list[str]:
    """Function names from the package's own frames, without file paths."""
    frames: list[str] = []
    for frame in traceback.extract_tb(error.__traceback__):
        filename = frame.filename.replace("\\", "/")
        if "/famepy/" in filename:
            module = filename.rsplit("/famepy/", 1)[1]
            frames.append(f"{module}:{frame.name}")
    return frames[-6:]


def _status_of(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _errno_of(error: BaseException) -> int | None:
    number = getattr(error, "errno", None)
    return number if isinstance(number, int) and not isinstance(number, bool) else None


class Recorder:
    """Collects cases for one group and knows how to run a checked callable."""

    def __init__(self) -> None:
        self.cases: list[Case] = []

    def add(self, case: Case) -> Case:
        self.cases.append(case)
        return case

    def blocked(self, case_id: str, note: str) -> Case:
        return self.add(Case(case_id, "blocked", note=note))

    def unsupported(self, case_id: str, note: str) -> Case:
        return self.add(Case(case_id, "unsupported", note=note))

    def fact(self, case_id: str, value: Any, *, note: str | None = None) -> Case:
        """Record an observation (never an assertion) with a synthetic value."""
        return self.add(
            Case(case_id, "pass", actual=encode_value(value), note=note, observation=True)
        )

    def check(self, case_id: str, function: Callable[[], Any], *, note: str | None = None) -> Any:
        """Run ``function``; a return records pass, an exception records fail.

        The return value is handed back to the caller and never recorded.
        """
        try:
            result = function()
        except BaseException as error:  # noqa: BLE001 - the runner must survive anything
            self.add(
                Case(
                    case_id,
                    "fail",
                    error_type=type(error).__name__,
                    status_code=_status_of(error),
                    errno=_errno_of(error),
                    note=note,
                    frames=_package_frames(error),
                )
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return None
        self.add(Case(case_id, "pass", note=note))
        return result

    def expect_error(
        self,
        case_id: str,
        function: Callable[[], Any],
        error_types: tuple[type[BaseException], ...],
        *,
        note: str | None = None,
    ) -> BaseException | None:
        """Pass when ``function`` raises one of ``error_types``."""
        try:
            function()
        except error_types as error:
            self.add(
                Case(
                    case_id,
                    "pass",
                    error_type=type(error).__name__,
                    status_code=_status_of(error),
                    note=note,
                )
            )
            return error
        except BaseException as error:  # noqa: BLE001
            self.add(
                Case(
                    case_id,
                    "fail",
                    error_type=type(error).__name__,
                    status_code=_status_of(error),
                    note="unexpected error type",
                    frames=_package_frames(error),
                )
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return None
        self.add(Case(case_id, "fail", note="no error was raised"))
        return None

    def equal(self, case_id: str, actual: Any, expected: Any, *, note: str | None = None) -> bool:
        ok = _equal(actual, expected)
        self.add(
            Case(
                case_id,
                "pass" if ok else "fail",
                expected=encode_value(expected),
                actual=encode_value(actual),
                note=note,
            )
        )
        return ok

    def counts(self) -> dict[str, int]:
        return {
            status: sum(1 for case in self.cases if case.status == status) for status in STATUSES
        }


def _equal(actual: Any, expected: Any) -> bool:
    """Exact comparison: arrays by dtype and bits, floats by bit pattern."""
    if isinstance(actual, np.ndarray) or isinstance(expected, np.ndarray):
        if not (isinstance(actual, np.ndarray) and isinstance(expected, np.ndarray)):
            return False
        if actual.shape != expected.shape or actual.dtype != expected.dtype:
            return False
        if actual.dtype.kind == "f":
            width = f"u{actual.dtype.itemsize}"
            return bool(np.array_equal(actual.view(width), expected.view(width)))
        return bool(np.array_equal(actual, expected))
    if isinstance(actual, (float, np.floating)) and isinstance(expected, (float, np.floating)):
        if isinstance(actual, np.floating) or isinstance(expected, np.floating):
            left, right = np.asarray(actual), np.asarray(expected)
            return bool(left.dtype == right.dtype and left.tobytes() == right.tobytes())
        return struct.pack("<d", actual) == struct.pack("<d", expected)
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            _equal(a, e) for a, e in zip(actual, expected, strict=True)
        )
    if isinstance(actual, dict) and isinstance(expected, dict):
        return set(actual) == set(expected) and all(_equal(actual[k], expected[k]) for k in actual)
    if type(actual) is bool or type(expected) is bool:
        return type(actual) is type(expected) and actual == expected
    return bool(actual == expected)
