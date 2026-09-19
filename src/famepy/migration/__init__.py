# SPDX-License-Identifier: MIT
"""FAME-to-DataEcon retirement workflow: plan, migrate, read back, verify.

The workflow copies the scalar and series objects of a FAME database into a
DataEcon file through FAMEPy's own readers and the public
``tsecon.dataecon`` writers, under a documented, versioned layout
(``famepy.migration._layout``). It is a *data* migration: values, missing
categories, index and value frequencies, ranges and names travel; the
report and the plan name what does not (``OMISSIONS``).

Typical use::

    from famepy import migration

    plan = migration.plan_migration("source.db")      # read-only
    print(plan.summary())                             # refusals, skips, losses
    report = migration.migrate("source.db", "archive.daec", plan=plan)
    assert report.complete, report.summary()

    with open_dataecon("archive.daec") as db:
        for name in migration.list_migrated(db):
            back = migration.read_migrated(db, name)
            assert migration.describe(back) == migration.describe(expected)

``expected_object`` builds the object an exact migration produces from a
bridge value (or a FAME raw object), so that a caller holding the source
values can verify the archive independently of what was read from FAME.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from tsecon import MIT, TSeries
from tsecon.frequencies import Frequency

from .._constants import MISSING_NORMAL
from ..bridge import DateSeries, NameList, StringSeries
from ._layout import (
    ATTRIBUTE_PREFIX,
    CATEGORY_CODES,
    CATEGORY_NAMES,
    KINDS,
    LAYOUT_VERSION,
    MASK_CATALOG,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_STARTED,
    LayoutError,
    MigratedObject,
    check_layout,
    describe,
    frequency_label,
    label_frequency,
    list_migrated,
    migration_status,
    read_migrated,
)
from ._migrate import (
    EMPTY_POLICIES,
    MISSING_POLICIES,
    OMISSIONS,
    UNSUPPORTED_POLICIES,
    MigrationEntry,
    MigrationLossError,
    MigrationOptions,
    MigrationPlan,
    MigrationRefused,
    MigrationReport,
    PlanEntry,
    migrate,
    plan_migration,
)


def dataecon_available() -> tuple[bool, str | None]:
    """Whether the DataEcon native extension loads here (never raises).

    Returns ``(True, None)`` or ``(False, <error class name>)``. Used by
    tests and the validation runner to record the environment block
    distinctly instead of substituting fake writes.
    """
    try:
        from tsecon.dataecon import open_dataecon_memory

        with open_dataecon_memory():
            pass
    except Exception as error:  # noqa: BLE001 - any failure is the block
        return False, type(error).__name__
    return True, None


def expected_object(
    name: str,
    value: Any,
    *,
    categories: Any = None,
    category: int = MISSING_NORMAL,
    missing: str = "mask",
    empty_frequency: Frequency | None = None,
    value_frequency: Frequency | None = None,
    kind: str | None = None,
) -> MigratedObject:
    """The ``MigratedObject`` an exact migration of ``value`` produces.

    ``value`` is a bridge value; ``categories`` the per-observation missing
    codes of a series (default: NaN or ``None`` observations are NC) and
    ``category`` the code of a missing scalar (``value`` is then ignored).
    An empty FAME series with an unknown first date is described by
    ``value=None`` with ``empty_frequency`` (and ``value_frequency`` for
    dates) and ``kind``. With ``missing="nan"`` floating series carry no
    categories, as the lossy policy stores them.
    """
    if value is None and empty_frequency is not None:
        if kind not in KINDS:
            raise ValueError("An empty series needs its kind.")
        return MigratedObject(
            name,
            kind,
            "series",
            None,
            np.empty(0, dtype=np.int32),
            frequency_label(empty_frequency),
            None if value_frequency is None else frequency_label(value_frequency),
            True,
            "empty",
        )
    if isinstance(value, TSeries):
        found = _kind_of_dtype(value.values.dtype)
        codes = _series_codes(value.values, categories, found)
        if missing == "nan" and found in ("precision", "numeric"):
            codes = np.zeros(len(value), dtype=np.int32)
        stored = value
        if found == "boolean":
            data = np.array(value.values, dtype=np.bool_)
            data[codes != MISSING_NORMAL] = False
            stored = TSeries(value.firstdate, data)
        return MigratedObject(
            name,
            found,
            "series",
            stored,
            codes,
            frequency_label(value.frequency),
            None,
            len(value) == 0,
            "empty" if len(value) == 0 else "native",
        )
    if isinstance(value, DateSeries):
        codes = _series_codes(value.values, categories, "date")
        any_missing = bool(np.any(codes != MISSING_NORMAL))
        return MigratedObject(
            name,
            "date",
            "series",
            value,
            codes,
            frequency_label(value.frequency),
            frequency_label(value.value_frequency),
            len(value) == 0,
            "empty" if len(value) == 0 else "codes" if any_missing else "native",
        )
    if isinstance(value, StringSeries):
        codes = _series_codes(value.values, categories, "string")
        strings = StringSeries(
            value.firstdate,
            [None if codes[i] else _as_text(v) for i, v in enumerate(value.values)],
        )
        return MigratedObject(
            name,
            "string",
            "series",
            strings,
            codes,
            frequency_label(value.frequency),
            None,
            len(value) == 0,
            "empty" if len(value) == 0 else "text",
        )
    if kind is None:
        kind = _scalar_kind(value)
    if category != MISSING_NORMAL:
        label = None if value_frequency is None else frequency_label(value_frequency)
        if kind == "date" and label is None and isinstance(value, MIT):
            label = frequency_label(value.frequency)
        return MigratedObject(name, kind, "scalar", None, category, None, label)
    label = frequency_label(value.frequency) if kind == "date" else None
    stored_value: Any = value
    if kind == "string":
        stored_value = _as_text(value)
    elif kind == "boolean":
        stored_value = bool(value)
    return MigratedObject(
        name,
        kind,
        "scalar",
        stored_value,
        MISSING_NORMAL,
        None,
        label,
        representation="members" if kind == "namelist" else None,
    )


def _as_text(value: Any) -> str:
    return value.decode("ascii") if isinstance(value, bytes) else str(value)


def _kind_of_dtype(dtype: Any) -> str:
    if dtype == np.float64:
        return "precision"
    if dtype == np.float32:
        return "numeric"
    if dtype == np.bool_:
        return "boolean"
    raise ValueError(f"No FAME series kind for dtype {dtype}.")


def _scalar_kind(value: Any) -> str:
    if isinstance(value, NameList):
        return "namelist"
    if isinstance(value, (bool, np.bool_)):
        return "boolean"
    if isinstance(value, np.float32):
        return "numeric"
    if isinstance(value, (int, float, np.floating, np.integer)):
        return "precision"
    if isinstance(value, MIT):
        return "date"
    if isinstance(value, (str, bytes)):
        return "string"
    raise ValueError(f"No FAME scalar kind for {type(value).__name__}.")


def _series_codes(values: Any, categories: Any, kind: str) -> np.ndarray:
    count = len(values)
    if categories is not None:
        codes = np.asarray(categories, dtype=np.int32)
        if codes.shape != (count,):
            raise ValueError("categories must have one code per observation.")
        return codes
    codes = np.zeros(count, dtype=np.int32)
    if kind in ("precision", "numeric"):
        codes[np.isnan(np.asarray(values))] = 1
    elif kind in ("date", "string"):
        for position, item in enumerate(values):
            if item is None:
                codes[position] = 1
    return codes


__all__ = [
    "ATTRIBUTE_PREFIX",
    "CATEGORY_CODES",
    "CATEGORY_NAMES",
    "EMPTY_POLICIES",
    "KINDS",
    "LAYOUT_VERSION",
    "MASK_CATALOG",
    "MISSING_POLICIES",
    "OMISSIONS",
    "STATUS_COMPLETE",
    "STATUS_INCOMPLETE",
    "STATUS_STARTED",
    "UNSUPPORTED_POLICIES",
    "LayoutError",
    "MigratedObject",
    "MigrationEntry",
    "MigrationLossError",
    "MigrationOptions",
    "MigrationPlan",
    "MigrationRefused",
    "MigrationReport",
    "PlanEntry",
    "check_layout",
    "dataecon_available",
    "describe",
    "expected_object",
    "frequency_label",
    "label_frequency",
    "list_migrated",
    "migrate",
    "migration_status",
    "plan_migration",
    "read_migrated",
]
