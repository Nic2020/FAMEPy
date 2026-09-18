# SPDX-License-Identifier: MIT
"""The case schema every report record must satisfy, shared by the parent and
by group children that adopt cases from nested workers.

A case carries only an identifier, a status, an error class name, a numeric
status, an OS error number, short notes, package frame names and synthetic
expected/actual values. Values are numbers, short strings drawn from a fixed
character set, bit patterns of non-finite floats, or bounded byte records
produced by the recorder's fixture-aware encoder. Anything else makes the
record malformed, and a malformed record fails its group.
"""

from __future__ import annotations

import re
from typing import Any

from ._report import STATUSES

_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.-]{0,159}$")
_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_NOTE = re.compile(r"^[A-Za-z0-9 _,.;:()'+=-]{1,200}$")
_TEXT = re.compile(r"^[A-Za-z0-9 _,.;:+={}\[\]()<>'-]{0,256}$")
_KEY = re.compile(r"^[A-Za-z0-9_:. -]{1,64}$")
_HEX = re.compile(r"^(?:[0-9a-f]{8}|[0-9a-f]{16})$")
_BYTES_HEX = re.compile(r"^(?:[0-9a-f]{2}){0,64}$")
_FRAME = re.compile(r"^[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*\.py:[A-Za-z0-9_<>]{1,80}$")
_MAX_LIST = 512
_MAX_DEPTH = 4
_ATTESTATION = re.compile(r"^[0-9a-f]{64}$")


def _clean_value(value: Any, depth: int = 0) -> tuple[bool, Any]:
    """Accept only synthetic-looking values: numbers, short safe strings, bits."""
    if value is None or isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return -(2**63) <= value < 2**63, value
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf")), value
    if isinstance(value, str):
        return bool(_TEXT.fullmatch(value)), value
    if depth >= _MAX_DEPTH:
        return False, None
    if isinstance(value, list):
        if len(value) > _MAX_LIST:
            return False, None
        cleaned = []
        for item in value:
            ok, clean = _clean_value(item, depth + 1)
            if not ok:
                return False, None
            cleaned.append(clean)
        return True, cleaned
    if isinstance(value, dict):
        if set(value) == {"bits"}:
            return isinstance(value["bits"], str) and bool(_HEX.fullmatch(value["bits"])), value
        if set(value) == {"ascii"}:
            return isinstance(value["ascii"], str) and bool(_TEXT.fullmatch(value["ascii"])), value
        if set(value) == {"hex"}:
            # Bounded synthetic bytes (string fixtures and sentinels), never text.
            return isinstance(value["hex"], str) and bool(_BYTES_HEX.fullmatch(value["hex"])), value
        if set(value) == {"length", "unexpected_bytes"}:
            return (
                value["unexpected_bytes"] is True
                and isinstance(value["length"], int)
                and not isinstance(value["length"], bool)
                and 0 <= value["length"] < 2**31
            ), value
        if set(value) == {"length", "sha256"}:
            return (
                isinstance(value["length"], int)
                and not isinstance(value["length"], bool)
                and 0 <= value["length"] < 2**31
                and isinstance(value["sha256"], str)
                and bool(_ATTESTATION.fullmatch(value["sha256"]))
            ), value
        if len(value) > 32:
            return False, None
        cleaned_dict: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                return False, None
            ok, clean = _clean_value(item, depth + 1)
            if not ok:
                return False, None
            cleaned_dict[key] = clean
        return True, cleaned_dict
    return False, None


def sanitize_case(case: Any) -> dict[str, Any]:
    """Validate one child case against the schema; malformed records fail."""
    malformed = {"id": "malformed", "status": "fail", "note": "malformed case record"}
    if not isinstance(case, dict):
        return malformed
    case_id = case.get("id")
    if not isinstance(case_id, str) or not _ID.fullmatch(case_id):
        return malformed
    status = case.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        return {**malformed, "id": case_id}
    clean: dict[str, Any] = {"id": case_id, "status": status}
    for key in ("error_type", "note"):
        if key in case:
            value = case[key]
            pattern = _ERROR_TYPE if key == "error_type" else _NOTE
            if not isinstance(value, str) or not pattern.fullmatch(value):
                return {**malformed, "id": case_id}
            clean[key] = value
    for key in ("status_code", "errno"):
        if key in case:
            value = case[key]
            if not isinstance(value, int) or isinstance(value, bool) or abs(value) >= 2**32:
                return {**malformed, "id": case_id}
            clean[key] = value
    for key in ("expected", "actual"):
        if key in case:
            ok, value = _clean_value(case[key])
            if not ok:
                return {**malformed, "id": case_id}
            clean[key] = value
    if "frames" in case:
        frames = case["frames"]
        if (
            not isinstance(frames, list)
            or len(frames) > 8
            or not all(isinstance(f, str) and _FRAME.fullmatch(f) for f in frames)
        ):
            return {**malformed, "id": case_id}
        clean["frames"] = frames
    if "observation" in case:
        if case["observation"] is not True:
            return {**malformed, "id": case_id}
        clean["observation"] = True
    return clean


__all__ = ["sanitize_case", "_clean_value"]
