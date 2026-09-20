# SPDX-License-Identifier: MIT
"""Plan and run a FAME-to-DataEcon migration with explicit loss policies.

``plan_migration`` opens the source read-only, lists its objects and decides
for each one, from metadata alone, whether it can be stored under the
selected options, which DataEcon name it gets, and which losses the
policies may cause. ``migrate`` runs a plan into a *new* file: it refuses
while any entry is refused or the plan no longer matches the source,
refuses an existing destination (claimed exclusively, never opened, never
overwritten), writes into a partial file that is renamed onto the claim
only when the run finishes, marks the destination catalog ``started``
before the first object and ``complete`` or ``incomplete`` after the last,
and contains per-object failures in the report so that a partial archive
is never mistaken for a complete one. There is no append mode: an archive
is produced by one run and never modified by another.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from tsecon import MIT
from tsecon.dataecon import DataEconError, DataEconFile, open_dataecon
from tsecon.frequencies import Frequency

from .._constants import MISSING_NORMAL, ObjectClass, frequency_name
from .._data import RawScalar, RawSeries, classify_by_sentinel, read_named
from .._database import FameDatabase, closedb, opendb
from .._errors import DataValidationError, HLIError, UnsupportedOperationError
from .._objects import FameObject
from .._runtime import Session
from .._text import TextEncodingError
from .._wildcard import listdb
from ..bridge import (
    DateSeries,
    NameList,
    StringSeries,
    UnsupportedFrequencyError,
    fame_frequency,
    is_supported_frequency,
    tsecon_frequency,
)
from ..bridge._values import from_fame
from ._layout import (
    KINDS,
    LAYOUT_VERSION,
    MASK_CATALOG,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_STARTED,
    LayoutError,
    attribute,
    catalog_path,
    frequency_label,
    write_empty_series,
    write_scalar,
    write_series,
)

MISSING_POLICIES = ("mask", "nan")
UNSUPPORTED_POLICIES = ("refuse", "skip")
EMPTY_POLICIES = ("carrier", "refuse")

# What a data-only migration through the verified API does not carry.
OMISSIONS: tuple[str, ...] = (
    "aliases (listed with aliases off; an alias is not migrated as a second object)",
    "BASIS and OBSERVED attributes (not retrievable through the bound calls)",
    "descriptions, documentation and user attributes (not retrievable)",
    "creation and modification timestamps (not retrievable)",
    "formulas, global objects and interpreter state (commands only)",
    "frequencies outside the bridge set (intraday, biweekly, ten-day, ...)",
    "missing-value bit patterns (categories are kept; sentinel bits are not)",
)


class MigrationRefused(ValueError):
    """The plan or the destination does not allow the migration to start."""


class MigrationLossError(ValueError):
    """An object's data cannot be represented under the selected policies."""


def _check_catalog(catalog: Any) -> str:
    """An absolute DataEcon catalog path with valid components; the root is ``/``."""
    if not isinstance(catalog, str) or not catalog.startswith("/") or "\0" in catalog:
        raise ValueError("catalog must be an absolute DataEcon path such as '/'.")
    if catalog == "/":
        return catalog
    components = catalog.strip("/").split("/")
    if catalog.endswith("/") or any(not part.strip() for part in components):
        raise ValueError("catalog must not have empty components or a trailing slash.")
    if MASK_CATALOG in components:
        raise ValueError(f"catalog must not use the reserved name {MASK_CATALOG!r}.")
    return catalog


@dataclass(frozen=True)
class MigrationOptions:
    """Loss and naming policies; the defaults refuse every representable loss.

    ``missing="mask"`` keeps every missing category (NC, NA, ND) in a sidecar
    mask; ``"nan"`` collapses floating missing observations to NaN (a
    documented loss) and refuses Boolean, date and string objects that have
    missing observations. ``unsupported="refuse"`` stops the migration when
    an object cannot be stored (unsupported class or frequency); ``"skip"``
    records it and continues. ``empty="carrier"`` stores an empty series as
    a typed empty carrier without a first date (``empty_firstdates`` may
    supply explicit ones, keyed by FAME name, and must match the series'
    frequency); ``"refuse"`` stops instead. The metadata a data migration
    cannot carry (``OMISSIONS``) is lost under every policy and is listed
    in every plan and report.
    """

    namecase: Callable[[str], str] = str.lower
    missing: str = "mask"
    unsupported: str = "refuse"
    empty: str = "carrier"
    empty_firstdates: Mapping[str, MIT] = field(default_factory=dict)
    catalog: str = "/"

    def __post_init__(self) -> None:
        if self.missing not in MISSING_POLICIES:
            raise ValueError("missing must be 'mask' or 'nan'.")
        if self.unsupported not in UNSUPPORTED_POLICIES:
            raise ValueError("unsupported must be 'refuse' or 'skip'.")
        if self.empty not in EMPTY_POLICIES:
            raise ValueError("empty must be 'carrier' or 'refuse'.")
        if not callable(self.namecase):
            raise TypeError("namecase must be callable.")
        _check_catalog(self.catalog)
        for key, value in self.empty_firstdates.items():
            if not isinstance(key, str) or not isinstance(value, MIT):
                raise TypeError("empty_firstdates maps FAME names to MIT values.")
            if not is_supported_frequency(_fame_code(value.frequency)):
                raise ValueError(f"empty_firstdates[{key!r}] has an unsupported frequency.")

    def firstdate_for(self, name: str) -> MIT | None:
        for key, value in self.empty_firstdates.items():
            if key.upper() == name.upper():
                return value
        return None


def _fame_code(frequency: Frequency) -> int:
    try:
        return fame_frequency(frequency)
    except (KeyError, UnsupportedFrequencyError):
        return -1


@dataclass(frozen=True)
class PlanEntry:
    """One source object: where it goes, how, and what the policies may lose."""

    name: str
    destination: str | None
    class_name: str
    kind: str | None
    frequency: str | None
    value_frequency: str | None
    empty: bool
    action: str  # store, skip or refuse
    reason: str | None = None
    losses: tuple[str, ...] = ()

    def __str__(self) -> str:
        where = "" if self.destination is None else f" -> {self.destination}"
        detail = "" if self.reason is None else f" ({self.reason})"
        return f"{self.name}{where}: {self.action}{detail}"


@dataclass(frozen=True)
class MigrationPlan:
    """A metadata preview: what a run would do, before any value is read.

    Values are read and converted only during the run, so a conversion
    that fails on the data itself (for example non-ASCII text) is a
    contained per-object failure of the run, not a refusal of the plan.
    """

    entries: tuple[PlanEntry, ...]
    options: MigrationOptions
    patterns: tuple[str | bytes, ...] = ("?",)
    omissions: tuple[str, ...] = OMISSIONS

    @property
    def refused(self) -> tuple[PlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "refuse")

    @property
    def skipped(self) -> tuple[PlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "skip")

    @property
    def stored(self) -> tuple[PlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "store")

    def summary(self) -> str:
        counts = {
            action: sum(1 for entry in self.entries if entry.action == action)
            for action in ("store", "skip", "refuse")
        }
        lines = [
            f"{len(self.entries)} objects: {counts['store']} to store, "
            f"{counts['skip']} skipped, {counts['refuse']} refused"
        ]
        lines.extend(str(entry) for entry in self.entries if entry.action != "store")
        return "\n".join(lines)


@dataclass(frozen=True)
class MigrationEntry:
    """The outcome of one planned object."""

    name: str
    destination: str | None
    action: str  # stored, skipped, refused or failed
    representation: str | None = None
    losses: tuple[str, ...] = ()
    categories: tuple[int, ...] = ()
    error_type: str | None = None
    status: int | None = None
    reason: str | None = None

    def __str__(self) -> str:
        detail = self.reason or self.error_type or ""
        if self.status is not None:
            detail += f" (status {self.status})"
        return f"{self.name}: {self.action}{' ' + detail if detail else ''}"


@dataclass(frozen=True)
class MigrationReport:
    """What a migration did; ``complete`` is False whenever anything was not stored."""

    plan: MigrationPlan
    entries: tuple[MigrationEntry, ...]
    status: str
    omissions: tuple[str, ...] = OMISSIONS

    @property
    def complete(self) -> bool:
        return self.status == STATUS_COMPLETE

    @property
    def stored(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries if entry.action == "stored")

    @property
    def failed(self) -> tuple[MigrationEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action == "failed")

    @property
    def lossy(self) -> tuple[MigrationEntry, ...]:
        return tuple(entry for entry in self.entries if entry.losses)

    def summary(self) -> str:
        counts = {
            action: sum(1 for entry in self.entries if entry.action == action)
            for action in ("stored", "skipped", "refused", "failed")
        }
        lines = [
            f"migration {self.status}: {counts['stored']} stored, {counts['skipped']} skipped, "
            f"{counts['refused']} refused, {counts['failed']} failed"
        ]
        lines.extend(str(entry) for entry in self.entries if entry.action != "stored")
        lines.extend(f"{entry.name}: {'; '.join(entry.losses)}" for entry in self.lossy)
        lines.append("not migrated: " + "; ".join(self.omissions))
        return "\n".join(lines)


# -- planning --------------------------------------------------------------------


def _label(code: int) -> str:
    return frequency_name(code)


def _refused(
    name: str,
    destination: str | None,
    class_name: str,
    kind: str | None,
    frequency: str | None,
    value_frequency: str | None,
    empty: bool,
    action: str,
    reason: str,
) -> PlanEntry:
    return PlanEntry(
        name, destination, class_name, kind, frequency, value_frequency, empty, action, reason
    )


def _plan_entry(info: FameObject, options: MigrationOptions, index_nc: int) -> PlanEntry:
    name = info.name_text.upper()
    unsupported = "refuse" if options.unsupported == "refuse" else "skip"
    if info.class_code not in (ObjectClass.SERIES, ObjectClass.SCALAR):
        return _refused(
            name, None, "other", None, None, None, False, unsupported, "unsupported class"
        )
    class_name = "series" if info.is_series else "scalar"
    try:
        kind = info.kind
    except (ValueError, DataValidationError):
        return _refused(
            name, None, class_name, None, None, None, False, unsupported, "unsupported type"
        )
    frequency = _label(info.frequency) if info.is_series else None
    value_frequency = None if info.date_frequency is None else _label(info.date_frequency)
    empty = info.is_empty(index_nc)
    try:
        destination = options.namecase(name)
    except Exception as error:  # noqa: BLE001 - a user callable
        reason = f"namecase raised {type(error).__name__}"
        return _refused(
            name, None, class_name, kind, frequency, value_frequency, empty, "refuse", reason
        )
    invalid = (
        not isinstance(destination, str)
        or not destination.strip()
        or "/" in destination
        or "\0" in destination
        or destination == MASK_CATALOG
    )
    if invalid:
        return _refused(
            name,
            None,
            class_name,
            kind,
            frequency,
            value_frequency,
            empty,
            "refuse",
            "invalid destination name",
        )
    if info.is_series and not is_supported_frequency(info.frequency):
        return _refused(
            name,
            destination,
            class_name,
            kind,
            frequency,
            value_frequency,
            empty,
            unsupported,
            "unsupported index frequency",
        )
    if info.date_frequency is not None and not is_supported_frequency(info.date_frequency):
        return _refused(
            name,
            destination,
            class_name,
            kind,
            frequency,
            value_frequency,
            empty,
            unsupported,
            "unsupported date value frequency",
        )
    supplied = options.firstdate_for(name)
    losses: list[str] = []
    if supplied is not None and not empty:
        reason = "first date supplied for a nonempty object"
        return _refused(
            name, destination, class_name, kind, frequency, value_frequency, empty, "refuse", reason
        )
    if empty:
        if supplied is not None and frequency_label(supplied.frequency) != frequency:
            reason = "explicit first date has another frequency than the series"
            return _refused(
                name,
                destination,
                class_name,
                kind,
                frequency,
                value_frequency,
                True,
                "refuse",
                reason,
            )
        if options.empty == "refuse" and supplied is None:
            reason = "empty series without a first date"
            return _refused(
                name,
                destination,
                class_name,
                kind,
                frequency,
                value_frequency,
                True,
                "refuse",
                reason,
            )
    elif options.missing == "nan":
        if kind in ("precision", "numeric"):
            losses.append("missing categories collapse to NaN")
        else:
            losses.append(f"a {kind} {class_name} with missing observations is refused")
    return PlanEntry(
        name,
        destination,
        class_name,
        kind,
        frequency,
        value_frequency,
        empty,
        "store",
        None,
        tuple(losses),
    )


def _resolve(target: Any, session: Session | None) -> tuple[FameDatabase, bool]:
    if isinstance(target, FameDatabase):
        return target, False
    if isinstance(target, (str, bytes, os.PathLike)):
        return opendb(target, "readonly", session=session), True
    raise TypeError("Expected a FameDatabase or a database path.")


def plan_migration(
    source: FameDatabase | str | bytes | os.PathLike[str],
    *,
    patterns: Sequence[str | bytes] = ("?",),
    options: MigrationOptions | None = None,
    session: Session | None = None,
) -> MigrationPlan:
    """Inspect the source read-only and decide what a migration would do.

    Objects are listed without aliases; formulas and globals, unsupported
    frequencies, invalid destination names, explicit first dates that do
    not fit their series and two objects landing on one destination name
    are refused (or, for unsupported objects, skipped). The decisions use
    object metadata only; values are read during the run.
    """
    selected = MigrationOptions() if options is None else options
    database, owned = _resolve(source, session)
    try:
        index_nc = database.session.sentinels.index_nc
        infos: dict[str, FameObject] = {}
        for pattern in patterns:
            for info in listdb(database, pattern, alias=False):
                infos.setdefault(info.name_text.upper(), info)
        entries = [_plan_entry(infos[name], selected, index_nc) for name in sorted(infos)]
    finally:
        if owned:
            closedb(database)
    seen: dict[str, str] = {}
    collided: set[str] = set()
    for entry in entries:
        if entry.destination is None or entry.action == "refuse":
            continue
        other = seen.get(entry.destination)
        if other is not None:
            collided.update((other, entry.name))
        seen[entry.destination] = entry.name
    if collided:
        entries = [
            PlanEntry(
                e.name,
                e.destination,
                e.class_name,
                e.kind,
                e.frequency,
                e.value_frequency,
                e.empty,
                "refuse",
                "destination name collision",
                e.losses,
            )
            if e.name in collided
            else e
            for e in entries
        ]
    return MigrationPlan(tuple(entries), selected, tuple(patterns))


# -- conversion of one object ----------------------------------------------------


@dataclass(frozen=True)
class _Converted:
    kind: str
    scalar: bool
    value: Any
    categories: np.ndarray
    firstdate: MIT | None
    value_frequency: Frequency | None
    losses: tuple[str, ...]


def _convert(raw: RawScalar | RawSeries, database: FameDatabase, options: MigrationOptions) -> Any:
    """Bridge value plus the raw categories; refuses unrepresentable data."""
    kind = raw.kind
    sentinels = database.session.sentinels
    if isinstance(raw, RawScalar):
        if kind == "namelist":
            value = from_fame(raw, database=database)
            return _Converted(kind, True, value, np.zeros(1, dtype=np.int32), None, None, ())
        values: Any = (
            [raw.value] if kind == "string" else np.array([raw.value], dtype=_scalar_dtype(kind))
        )
        codes = classify_by_sentinel(values, kind, sentinels)
        category = int(codes[0])
        value_frequency = (
            None if raw.date_frequency is None else tsecon_frequency(raw.date_frequency)
        )
        if category != MISSING_NORMAL:
            if options.missing == "nan" and kind not in ("precision", "numeric"):
                raise MigrationLossError(f"The {kind} scalar is missing.")
            if options.missing == "nan":
                # The lossy policy stores a plain NaN and no category.
                nan: Any = np.float32(np.nan) if kind == "numeric" else float("nan")
                return _Converted(
                    kind,
                    True,
                    nan,
                    np.zeros(1, dtype=np.int32),
                    None,
                    value_frequency,
                    ("missing category collapses to NaN",),
                )
            return _Converted(kind, True, None, codes, None, value_frequency, ())
        value = from_fame(raw, database=database, missing="strict")
        return _Converted(kind, True, value, codes, None, value_frequency, ())
    if raw.is_empty:
        value_frequency = (
            None if raw.date_frequency is None else tsecon_frequency(raw.date_frequency)
        )
        return _Converted(kind, False, None, np.empty(0, dtype=np.int32), None, value_frequency, ())
    codes = classify_by_sentinel(raw.values, kind, sentinels)
    any_missing = bool(np.any(codes != MISSING_NORMAL))
    losses: list[str] = []
    if any_missing and options.missing == "nan":
        if kind in ("precision", "numeric"):
            losses.append("missing categories collapse to NaN")
        else:
            raise MigrationLossError(f"The {kind} series has missing observations.")
    if kind == "boolean":
        # The bridge refuses missing Booleans; the mask carries them here.
        replaced = np.array(raw.values, copy=True)
        replaced[codes != MISSING_NORMAL] = 0
        raw = RawSeries("boolean", raw.frequency, raw.first_index, replaced)
    value = from_fame(raw, database=database)
    if isinstance(value, DateSeries):
        return _Converted(
            kind, False, value.values, codes, value.firstdate, value.value_frequency, tuple(losses)
        )
    if isinstance(value, StringSeries):
        return _Converted(kind, False, value.values, codes, value.firstdate, None, tuple(losses))
    return _Converted(kind, False, value.values, codes, value.firstdate, None, tuple(losses))


def _scalar_dtype(kind: str) -> Any:
    return {
        "precision": np.float64,
        "numeric": np.float32,
        "boolean": np.int32,
        "date": np.int64,
    }[kind]


def _store(
    db: DataEconFile,
    entry: PlanEntry,
    converted: _Converted,
    options: MigrationOptions,
    raw: RawScalar | RawSeries,
) -> str:
    assert entry.destination is not None
    catalog = options.catalog
    name = entry.destination
    if converted.scalar:
        write_scalar(
            db,
            catalog,
            name,
            converted.kind,
            converted.value,
            int(converted.categories[0]),
            value_frequency=converted.value_frequency,
        )
        return "members" if converted.kind == "namelist" else "native"
    if converted.firstdate is None:
        assert isinstance(raw, RawSeries)
        return write_empty_series(
            db,
            catalog,
            name,
            converted.kind,
            tsecon_frequency(raw.frequency),
            value_frequency=converted.value_frequency,
            firstdate=options.firstdate_for(entry.name),
        )
    return write_series(
        db,
        catalog,
        name,
        converted.kind,
        converted.firstdate,
        converted.value,
        converted.categories,
        value_frequency=converted.value_frequency,
        mask=options.missing == "mask",
    )


_OBJECT_ERRORS: tuple[type[Exception], ...] = (
    HLIError,
    DataValidationError,
    UnsupportedOperationError,
    UnsupportedFrequencyError,
    TextEncodingError,
    MigrationLossError,
    LayoutError,
    DataEconError,
    ValueError,
    TypeError,
)


# -- running ---------------------------------------------------------------------


def _check_destination(destination: Any, source: Any) -> str:
    if isinstance(destination, os.PathLike):
        destination = os.fspath(destination)
    if not isinstance(destination, str) or not destination:
        raise TypeError("destination must be a DataEcon file path.")
    if isinstance(source, (str, bytes, os.PathLike)):
        source_text = os.fsdecode(source)
        if os.path.normcase(os.path.abspath(source_text)) == os.path.normcase(
            os.path.abspath(destination)
        ):
            raise MigrationRefused("The destination is the source database.")
    if os.path.lexists(destination):
        raise MigrationRefused(
            "The destination already exists; a migration only ever creates a new file."
        )
    return destination


def _claim(target: str) -> None:
    """Create the destination exclusively; an existing file is refused, never opened."""
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise MigrationRefused(
            "The destination already exists; a migration only ever creates a new file."
        ) from None
    os.close(descriptor)


def _release(target: str) -> None:
    """Remove the zero-byte claim after a failed run (never a file with content)."""
    try:
        if os.path.getsize(target) == 0:
            os.unlink(target)
    except OSError:
        pass


def _check_plan(plan: MigrationPlan, source: Any, session: Session | None) -> None:
    """A supplied plan must still describe the source exactly as it is now."""
    current = plan_migration(source, patterns=plan.patterns, options=plan.options, session=session)
    if current.entries != plan.entries:
        raise MigrationRefused(
            "The plan no longer matches the source (objects were added, removed or changed "
            "since it was built); plan again."
        )


def migrate(
    source: FameDatabase | str | bytes | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    patterns: Sequence[str | bytes] = ("?",),
    options: MigrationOptions | None = None,
    plan: MigrationPlan | None = None,
    session: Session | None = None,
) -> MigrationReport:
    """Copy the planned objects into a new DataEcon file and report every outcome.

    Before anything is created: the destination must not exist and must not
    be the source; the plan (built here when not given, otherwise checked
    against the source's current metadata) must have no refused entry.
    Then the destination path is claimed exclusively, the archive is
    written to a partial file next to it (``<destination>.<id>.partial``)
    with the layout version, the ``started`` mark and the planned count on
    the catalog, each object is read, converted and stored in its own
    contained step (a failure is recorded with its error class and status
    and the others continue), the catalog is marked ``complete`` or
    ``incomplete``, and the finished file is moved onto the claim. The
    source is only ever opened read-only. A run that stops before the move
    (an interrupted process, an error outside the per-object containment)
    leaves the partial file with its ``started`` or ``incomplete`` mark and
    releases the claim.
    """
    if plan is not None and options is not None and plan.options is not options:
        raise ValueError("plan and options disagree; pass the plan's options or neither.")
    if plan is not None and tuple(patterns) != ("?",) and tuple(patterns) != plan.patterns:
        raise ValueError("plan and patterns disagree; pass the plan's patterns or neither.")
    selected = (
        plan.options if plan is not None else MigrationOptions() if options is None else options
    )
    target = _check_destination(destination, source)
    if plan is None:
        plan = plan_migration(source, patterns=patterns, options=selected, session=session)
    else:
        _check_plan(plan, source, session)
    if plan.refused:
        names = ", ".join(entry.name for entry in plan.refused[:5])
        raise MigrationRefused(
            f"{len(plan.refused)} object(s) refused by the plan (for example {names}); "
            "adjust the options or the selection."
        )
    entries: list[MigrationEntry] = [
        MigrationEntry(e.name, e.destination, "skipped", reason=e.reason) for e in plan.skipped
    ]
    _claim(target)
    partial = f"{target}.{uuid.uuid4().hex[:12]}.partial"
    if os.path.lexists(partial):
        _release(target)
        raise MigrationRefused("The partial file name is already taken; run again.")
    status = STATUS_INCOMPLETE
    try:
        with open_dataecon(partial, "w") as db:
            if selected.catalog != "/":
                db.new_catalog(selected.catalog, parents=True, exist_ok=False)
            db.new_catalog(catalog_path(selected.catalog, MASK_CATALOG), exist_ok=False)
            db.set_attribute(selected.catalog, attribute("layout"), LAYOUT_VERSION)
            db.set_attribute(selected.catalog, attribute("status"), STATUS_STARTED)
            db.set_attribute(selected.catalog, attribute("planned"), str(len(plan.stored)))
            stored = 0
            try:
                database, owned = _resolve(source, session)
                try:
                    for entry in plan.stored:
                        entries.append(_migrate_one(db, database, entry, selected))
                        if entries[-1].action == "stored":
                            stored += 1
                finally:
                    if owned:
                        closedb(database)
                if stored == len(plan.stored) and not plan.skipped:
                    status = STATUS_COMPLETE
            finally:
                db.set_attribute(selected.catalog, attribute("written"), str(stored))
                db.set_attribute(selected.catalog, attribute("status"), status)
        os.replace(partial, target)
    except BaseException:
        _release(target)
        raise
    return MigrationReport(plan, tuple(entries), status)


def _migrate_one(
    db: DataEconFile, database: FameDatabase, entry: PlanEntry, options: MigrationOptions
) -> MigrationEntry:
    try:
        raw = read_named(database, entry.name)
        converted = _convert(raw, database, options)
        representation = _store(db, entry, converted, options, raw)
    except _OBJECT_ERRORS as error:
        status = getattr(error, "status", None)
        return MigrationEntry(
            entry.name,
            entry.destination,
            "failed",
            error_type=type(error).__name__,
            status=status if isinstance(status, int) and not isinstance(status, bool) else None,
            reason=str(error) if isinstance(error, (MigrationLossError, LayoutError)) else None,
        )
    return MigrationEntry(
        entry.name,
        entry.destination,
        "stored",
        representation,
        converted.losses,
        tuple(int(c) for c in converted.categories),
    )


__all__ = [
    "EMPTY_POLICIES",
    "KINDS",
    "MISSING_POLICIES",
    "OMISSIONS",
    "UNSUPPORTED_POLICIES",
    "MigrationEntry",
    "MigrationLossError",
    "MigrationOptions",
    "MigrationPlan",
    "MigrationRefused",
    "MigrationReport",
    "NameList",
    "PlanEntry",
    "migrate",
    "plan_migration",
]
