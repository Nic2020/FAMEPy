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

import hashlib
import math
import re
import struct
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

STATUSES = ("pass", "fail", "blocked", "unsupported")

# Bytes are rendered losslessly only when they are *permitted*: values the
# runner itself constructed (every expected value of an assertion) or
# sentinels a group registered. Permitted bytes appear as text when printable
# in the report's own character set, as bounded hex otherwise, and as a
# length plus digest beyond the hex bound. Any other bytes, which is what a
# library returns when it disagrees with the fixture, are reduced to their
# length: fitting a regex never makes unknown bytes safe to export.
_PRINTABLE = re.compile(rb"^[A-Za-z0-9 _,.;:+={}\[\]()<>'-]{0,256}$")
MAX_HEX_BYTES = 64
_STAGE = re.compile(r"^[a-z_]{1,32}$")


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


Permitted = frozenset[bytes] | set[bytes]


def encode_value(value: Any, permitted: Permitted = frozenset()) -> Any:
    """Make synthetic values JSON-safe; non-finite floats keep exact bits.

    ``permitted`` names the byte values that may be rendered losslessly.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, (float, np.floating)):
        return _float_record(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (bytes, bytearray)):
        return encode_bytes(bytes(value), permitted)
    if isinstance(value, (list, tuple)):
        return [encode_value(item, permitted) for item in value]
    if isinstance(value, dict):
        return {str(key): encode_value(item, permitted) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        if value.dtype.kind == "f":
            return [_float_record(item) for item in value]
        return encode_value(value.tolist(), permitted)
    return repr(type(value).__name__)


def encode_bytes(value: bytes, permitted: Permitted = frozenset()) -> dict[str, Any]:
    """Permitted bytes losslessly (text, bounded hex or digest); others by length."""
    if value not in permitted:
        return {"length": len(value), "unexpected_bytes": True}
    if _PRINTABLE.fullmatch(value):
        return {"ascii": value.decode("ascii")}
    if len(value) <= MAX_HEX_BYTES:
        return {"hex": value.hex()}
    return {"length": len(value), "sha256": hashlib.sha256(value).hexdigest()}


def collect_bytes(value: Any, into: set[bytes]) -> None:
    """Gather every bytes value nested in a synthetic expected value."""
    if isinstance(value, (bytes, bytearray)):
        into.add(bytes(value))
    elif isinstance(value, (list, tuple)):
        for item in value:
            collect_bytes(item, into)
    elif isinstance(value, dict):
        for item in value.values():
            collect_bytes(item, into)


def _stage_note(error: BaseException) -> str | None:
    """A short identifier of the failing stage when the error names one."""
    stage = getattr(error, "stage", None)
    if isinstance(stage, str) and _STAGE.fullmatch(stage):
        return f"stage {stage}"
    return None


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
        # Byte values that may appear losslessly in this recorder's records:
        # expected values of assertions (synthetic by construction) and the
        # sentinels a group registers with ``permit``.
        self.permitted: set[bytes] = set()

    def permit(self, *values: Any) -> None:
        """Register runner-owned byte values (for example sentinels) as exportable."""
        for value in values:
            collect_bytes(value, self.permitted)

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
            Case(
                case_id,
                "pass",
                actual=encode_value(value, self.permitted),
                note=note,
                observation=True,
            )
        )

    def check(self, case_id: str, function: Callable[[], Any], *, note: str | None = None) -> Any:
        """Run ``function``; a return records pass, an exception records fail.

        The return value is handed back to the caller and never recorded.
        Use ``ok`` when the function returns None and only success matters.
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
                    note=note or _stage_note(error),
                    frames=_package_frames(error),
                )
            )
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return None
        self.add(Case(case_id, "pass", note=note))
        return result

    def ok(self, case_id: str, function: Callable[[], Any], *, note: str | None = None) -> bool:
        """Like ``check`` but report whether the case passed (for None-returning steps)."""
        self.check(case_id, function, note=note)
        return self.cases[-1].id == case_id and self.cases[-1].status == "pass"

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
                    note=note or _stage_note(error),
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
        # The expected side is the runner's own fixture, so its bytes are
        # exportable; the actual side is rendered only where it matches them.
        collect_bytes(expected, self.permitted)
        self.add(
            Case(
                case_id,
                "pass" if ok else "fail",
                expected=encode_value(expected, self.permitted),
                actual=encode_value(actual, self.permitted),
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
