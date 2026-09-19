# SPDX-License-Identifier: MIT
"""The DataEcon layout of migrated FAME objects (layout version 1).

Every migrated object is stored through the public ``tsecon.dataecon``
writers under its own name and described by string attributes whose names
start with ``famepy.migration.``; the destination catalog carries the
layout version and the migration status; a catalog named
``famepy_migration_masks`` inside it holds one ``int8`` series per object
that had missing observations (0 normal, 1 NC, 2 NA, 3 ND). Nothing about
the DataEcon file format is extended: catalogs, series, scalars, arrays
and attributes are the ordinary public operations.

Representation of each FAME kind (``famepy.migration.kind``):

| FAME object            | DataEcon object                       | attributes / sidecars          |
|------------------------|---------------------------------------|--------------------------------|
| precision scalar       | Float64 scalar (NaN when missing)     | missing = NC, NA or ND         |
| numeric scalar         | Float32 scalar (NaN when missing)     | missing                        |
| Boolean scalar         | Int8 scalar (0 when missing)          | missing                        |
| date scalar            | MIT scalar; Int64 zero when missing   | value_frequency, missing       |
| string scalar          | string scalar (empty when missing)    | missing                        |
| namelist scalar        | text vector of the ordered members    | representation = members       |
| precision/numeric ser. | float64/float32 TSeries (NaN missing) | frequency; mask if any missing |
| Boolean series         | bool TSeries (False when missing)     | frequency; mask if any missing |
| date series, complete  | StoredSeries of MIT elements          | frequency, value_frequency     |
| date series, missing   | int64 TSeries of moment codes (0)     | representation = codes, mask   |
| string series          | text vector ("" when missing)         | frequency, firstdate, mask     |
| empty series, any kind | zero-length array of the kind's dtype | frequency, empty               |

Moment codes are TimeSeriesEconPy's own ``MIT`` integers, never FAME
indices; a first date stored as an attribute is that integer together with
the ``frequency`` label (a FAME frequency name such as ``monthly`` or
``weekly_sunday``). An empty FAME series has no first date, and the layout
keeps it unknown (``empty`` = ``unknown_firstdate``) unless the caller
supplied one explicitly (``empty`` = ``explicit_firstdate``, stored as a
typed empty series); no date is ever synthesized.

Reading is structural verification, not trust in the attributes: the
catalog must carry this layout version, every attribute must be one the
layout defines with a defined value, and the stored object's DataEcon
class, dtype, length, index frequency, element frequency, missing
placeholders and mask (frequency, first date, length, codes) must agree
with what the attributes declare. Any disagreement is a ``LayoutError``;
nothing contradictory is read back as ordinary data.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from tsecon import MIT, TSeries
from tsecon.dataecon import DataEconFile, StoredElement, StoredSeries, StoredText
from tsecon.frequencies import Frequency

from .._constants import FREQUENCIES, MISSING_NORMAL, frequency_name
from ..bridge import DateSeries, NameList, StringSeries, fame_frequency, tsecon_frequency

LAYOUT_VERSION = "1"
ATTRIBUTE_PREFIX = "famepy.migration."
MASK_CATALOG = "famepy_migration_masks"
STATUS_STARTED = "started"
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUSES = (STATUS_STARTED, STATUS_COMPLETE, STATUS_INCOMPLETE)
CATALOG_ATTRIBUTES = ("layout", "status", "planned", "written")
OBJECT_ATTRIBUTES = (
    "kind",
    "missing",
    "value_frequency",
    "representation",
    "frequency",
    "mask",
    "firstdate",
    "empty",
)
EMPTY_MODES = ("unknown_firstdate", "explicit_firstdate")
CATEGORY_NAMES = {1: "NC", 2: "NA", 3: "ND"}
CATEGORY_CODES = {name: code for code, name in CATEGORY_NAMES.items()}
KINDS = ("precision", "numeric", "boolean", "date", "string", "namelist")
_EMPTY_DTYPES: dict[str, Any] = {
    "precision": np.float64,
    "numeric": np.float32,
    "boolean": np.bool_,
    "date": np.int64,
}


class LayoutError(ValueError):
    """A migrated object does not follow the documented layout."""


def attribute(name: str) -> str:
    return ATTRIBUTE_PREFIX + name


def catalog_path(catalog: str, name: str) -> str:
    """The DataEcon path of ``name`` inside ``catalog`` (``/`` is the root)."""
    base = catalog.rstrip("/")
    return f"{base}/{name}"


def mask_path(catalog: str, name: str) -> str:
    return catalog_path(catalog_path(catalog, MASK_CATALOG), name)


def frequency_label(frequency: Frequency) -> str:
    """The FAME frequency name of a TimeSeriesEconPy frequency."""
    return frequency_name(fame_frequency(frequency))


def label_frequency(label: str) -> Frequency:
    if label not in FREQUENCIES:
        raise LayoutError(f"Unknown frequency label {label!r}.")
    return tsecon_frequency(FREQUENCIES[label])


@dataclass(frozen=True)
class MigratedObject:
    """One migrated object as read back from DataEcon, in bridge terms.

    ``value`` is the bridge value (``float``, ``numpy.float32``, ``bool``,
    ``MIT``, ``str``, ``NameList``, ``TSeries``, ``DateSeries`` or
    ``StringSeries``); a missing scalar reads as ``None`` and a missing
    observation as NaN (floating series) or ``None``. ``categories`` gives
    the missing category of a scalar (``0`` normal) or one code per
    observation. An empty series has ``empty=True``, no value unless a
    first date was stored, and its ``frequency`` label.

    The labels are checked against the value: a series whose carrier has
    another index or element frequency than its label, or whose categories
    do not match its length, cannot be constructed.
    """

    name: str
    kind: str
    class_name: str
    value: Any
    categories: Any
    frequency: str | None = None
    value_frequency: str | None = None
    empty: bool = False
    representation: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise LayoutError(f"Unknown kind {self.kind!r}.")
        if self.class_name not in ("scalar", "series"):
            raise LayoutError("class_name must be 'scalar' or 'series'.")
        value = self.value
        if self.class_name == "scalar":
            if self.kind == "date" and isinstance(value, MIT):
                _same_label(self.name, "value frequency", value.frequency, self.value_frequency)
            return
        if self.empty != (self.representation == "empty"):
            raise LayoutError(f"{self.name!r}: empty flag and representation disagree.")
        if value is None:
            if len(self.categories) != 0 or not self.empty:
                raise LayoutError(f"{self.name!r}: a series without a carrier must be empty.")
            return
        if len(self.categories) != len(value):
            raise LayoutError(f"{self.name!r}: one category per observation is required.")
        if self.empty and len(value) != 0:
            raise LayoutError(f"{self.name!r}: an empty series carries observations.")
        _same_label(self.name, "index frequency", value.firstdate.frequency, self.frequency)
        if isinstance(value, DateSeries):
            _same_label(self.name, "value frequency", value.value_frequency, self.value_frequency)


def _same_label(name: str, what: str, frequency: Frequency, label: str | None) -> None:
    actual = frequency_label(frequency)
    if actual != label:
        raise LayoutError(f"{name!r}: the {what} label {label!r} contradicts the value ({actual}).")


# -- descriptions (JSON-safe, used by the verification) ----------------------


def _bits(value: Any, dtype: Any) -> str:
    return np.array(value, dtype=dtype).tobytes().hex()


def _describe_values(kind: str, value: Any) -> Any:
    if kind in ("precision", "numeric"):
        dtype = np.float64 if kind == "precision" else np.float32
        return [_bits(v, dtype) for v in np.asarray(value, dtype=dtype)]
    if kind == "boolean":
        return [int(bool(v)) for v in value]
    if kind == "date":
        return [None if v is None else int(v) for v in value]
    if kind == "string":
        return [None if v is None else str(v) for v in value]
    return [str(member) for member in value.members]


def describe(migrated: MigratedObject) -> dict[str, Any]:
    """A canonical, JSON-safe description: exact bits, codes and categories.

    Applied to an expected object built from a fixture and to the object
    read back from DataEcon, so that equality of the two descriptions is
    the verification. The frequency labels come from the value's own
    carrier (``MigratedObject`` refuses contradictory labels), so a
    description never repeats metadata the stored object does not have.
    """
    record: dict[str, Any] = {
        "name": migrated.name,
        "kind": migrated.kind,
        "class": migrated.class_name,
        "frequency": migrated.frequency,
        "value_frequency": migrated.value_frequency,
        "empty": migrated.empty,
        "representation": migrated.representation,
    }
    value = migrated.value
    if migrated.class_name == "scalar":
        record["category"] = int(migrated.categories)
        if value is None:
            record["value"] = None
        elif migrated.kind == "date":
            record["value"] = int(value)
        elif migrated.kind == "namelist":
            record["value"] = list(value.members)
        elif migrated.kind == "string":
            record["value"] = str(value)
        else:
            record["value"] = _describe_values(migrated.kind, [value])[0]
        return record
    record["categories"] = [int(c) for c in migrated.categories]
    if value is None:
        record["firstdate"] = None
        record["values"] = []
        return record
    record["firstdate"] = int(value.firstdate)
    if isinstance(value, TSeries) and migrated.kind == "date":
        record["values"] = [int(v) for v in value.values]
    else:
        record["values"] = _describe_values(migrated.kind, value.values)
    return record


# -- writing --------------------------------------------------------------------


def _set(db: DataEconFile, path: str, **attributes: str | None) -> None:
    for key, value in attributes.items():
        if value is not None:
            db.set_attribute(path, attribute(key), value)


def _write_mask(
    db: DataEconFile, catalog: str, name: str, firstdate: MIT, categories: np.ndarray
) -> None:
    # A migration only ever writes into a file it created, so a mask that
    # already exists is a layout error, never something to replace.
    mask = TSeries(firstdate, np.asarray(categories, dtype=np.int8))
    db.write_series(mask_path(catalog, name), mask, overwrite=False)


def write_scalar(
    db: DataEconFile,
    catalog: str,
    name: str,
    kind: str,
    value: Any,
    category: int,
    *,
    value_frequency: Frequency | None = None,
) -> None:
    """Store one scalar of a FAME kind with its missing category."""
    path = catalog_path(catalog, name)
    missing = None if category == MISSING_NORMAL else CATEGORY_NAMES[category]
    attributes: dict[str, str | None] = {"kind": kind, "missing": missing}
    stored: Any
    if kind == "namelist":
        stored = list(value.members)
        attributes["representation"] = "members"
        db.write_array(path, stored, overwrite=False)
    else:
        if kind == "precision":
            stored = math.nan if missing else float(value)
        elif kind == "numeric":
            stored = np.float32(np.nan) if missing else np.float32(value)
        elif kind == "boolean":
            stored = False if missing else bool(value)
        elif kind == "string":
            stored = "" if missing else str(value)
        else:
            if value_frequency is None:
                raise LayoutError("A date scalar needs its value frequency.")
            attributes["value_frequency"] = frequency_label(value_frequency)
            stored = 0 if missing else value
        db.write_scalar(path, stored, overwrite=False)
    _set(db, path, **attributes)


def write_series(
    db: DataEconFile,
    catalog: str,
    name: str,
    kind: str,
    firstdate: MIT,
    values: Sequence[Any] | np.ndarray,
    categories: np.ndarray,
    *,
    value_frequency: Frequency | None = None,
    mask: bool = True,
) -> str:
    """Store one nonempty series; returns the representation label.

    ``values`` holds normal observations in bridge form (floats, bools,
    ``MIT``/``None``, ``str``/``None``); ``categories`` the per-observation
    missing codes. With ``mask=True`` the categories are stored as a sidecar
    when any observation is missing; with ``mask=False`` floating kinds
    collapse missing observations to NaN and the other kinds refuse them.
    """
    path = catalog_path(catalog, name)
    codes = np.asarray(categories, dtype=np.int32)
    any_missing = bool(np.any(codes != MISSING_NORMAL))
    if any_missing and not mask and kind not in ("precision", "numeric"):
        raise LayoutError(
            f"A {kind} series with missing observations has no representation without a mask."
        )
    attributes: dict[str, str | None] = {
        "kind": kind,
        "frequency": frequency_label(firstdate.frequency),
    }
    representation = "native"
    if kind in ("precision", "numeric"):
        dtype = np.float64 if kind == "precision" else np.float32
        data = np.array(values, dtype=dtype, copy=True)
        data[codes != MISSING_NORMAL] = np.nan
        db.write_series(path, TSeries(firstdate, data), overwrite=False)
    elif kind == "boolean":
        data = np.array([bool(v) for v in values], dtype=np.bool_)
        data[codes != MISSING_NORMAL] = False
        db.write_series(path, TSeries(firstdate, data), overwrite=False)
    elif kind == "date":
        if value_frequency is None:
            raise LayoutError("A date series needs its value frequency.")
        attributes["value_frequency"] = frequency_label(value_frequency)
        if any_missing:
            representation = "codes"
            codes64 = np.array([0 if v is None else int(v) for v in values], dtype=np.int64)
            db.write_series(path, TSeries(firstdate, codes64), overwrite=False)
        else:
            element = StoredElement.date(value_frequency)
            stored = StoredSeries.from_list(firstdate, element, list(values))
            db.write_series(path, stored, overwrite=False)
    elif kind == "string":
        representation = "text"
        attributes["firstdate"] = str(int(firstdate))
        text = ["" if v is None else str(v) for v in values]
        db.write_array(path, text, overwrite=False)
    else:
        raise LayoutError(f"{kind} is not a series kind.")
    attributes["representation"] = representation
    if any_missing and mask:
        _write_mask(db, catalog, name, firstdate, codes)
        attributes["mask"] = "1"
    _set(db, path, **attributes)
    return representation


def write_empty_series(
    db: DataEconFile,
    catalog: str,
    name: str,
    kind: str,
    frequency: Frequency,
    *,
    value_frequency: Frequency | None = None,
    firstdate: MIT | None = None,
) -> str:
    """Store an empty series without inventing a first date.

    Without ``firstdate`` a zero-length array of the kind's dtype (a text
    vector for strings) carries the kind and frequency labels; with an
    explicit ``firstdate`` a typed empty series is stored instead.
    """
    path = catalog_path(catalog, name)
    attributes: dict[str, str | None] = {"kind": kind, "frequency": frequency_label(frequency)}
    if kind == "date":
        if value_frequency is None:
            raise LayoutError("A date series needs its value frequency.")
        attributes["value_frequency"] = frequency_label(value_frequency)
    if firstdate is not None:
        if firstdate.frequency != frequency:
            raise LayoutError("The explicit first date has the wrong frequency.")
        attributes["empty"] = "explicit_firstdate"
        if kind == "string":
            attributes["firstdate"] = str(int(firstdate))
            db.write_array(path, [], overwrite=False)
        elif kind == "date":
            assert value_frequency is not None
            stored = StoredSeries.from_list(firstdate, StoredElement.date(value_frequency), [])
            db.write_series(path, stored, overwrite=False)
        else:
            empty = TSeries(firstdate, np.empty(0, dtype=_EMPTY_DTYPES[kind]))
            db.write_series(path, empty, overwrite=False)
    else:
        attributes["empty"] = "unknown_firstdate"
        if kind == "string":
            db.write_array(path, [], overwrite=False)
        else:
            db.write_array(path, np.empty(0, dtype=_EMPTY_DTYPES[kind]), overwrite=False)
    _set(db, path, **attributes)
    return "empty"


# -- reading ---------------------------------------------------------------------


def _attributes(db: DataEconFile, path: str) -> dict[str, str]:
    found = db.get_attributes(path)
    return {
        key[len(ATTRIBUTE_PREFIX) :]: value
        for key, value in found.items()
        if key.startswith(ATTRIBUTE_PREFIX)
    }


def _require(
    attributes: dict[str, str], name: str, required: Iterable[str], optional: Iterable[str] = ()
) -> None:
    """Exactly the declared attributes: unknown or missing ones are a layout error."""
    needed, allowed = set(required), set(required) | set(optional)
    unknown = sorted(set(attributes) - allowed)
    if unknown:
        raise LayoutError(f"{name!r} carries undefined layout attributes: {', '.join(unknown)}.")
    absent = sorted(needed - set(attributes))
    if absent:
        raise LayoutError(f"{name!r} lacks layout attributes: {', '.join(absent)}.")


def check_layout(db: DataEconFile, catalog: str) -> dict[str, str]:
    """The catalog's layout attributes, verified to be this layout version."""
    found = _attributes(db, catalog)
    _require(found, catalog, ("layout", "status"), ("planned", "written"))
    if found["layout"] != LAYOUT_VERSION:
        raise LayoutError(
            f"{catalog!r} carries layout {found['layout']!r}; this release reads "
            f"layout {LAYOUT_VERSION}."
        )
    if found["status"] not in STATUSES:
        raise LayoutError(f"{catalog!r} carries an undefined status {found['status']!r}.")
    for key in ("planned", "written"):
        if key in found and not (found[key].isascii() and found[key].isdigit()):
            raise LayoutError(f"{catalog!r} carries an invalid {key} count.")
    if found["status"] != STATUS_STARTED and not {"planned", "written"} <= set(found):
        raise LayoutError(f"{catalog!r} lacks final migration counts.")
    if {"planned", "written"} <= set(found):
        planned, written = int(found["planned"]), int(found["written"])
        if written > planned or (found["status"] == STATUS_COMPLETE and written != planned):
            raise LayoutError(f"{catalog!r} carries inconsistent migration counts.")
    return found


def _read_mask(
    db: DataEconFile, catalog: str, name: str, firstdate: MIT, length: int
) -> np.ndarray:
    mask = db.read_series(mask_path(catalog, name))
    if not isinstance(mask, TSeries) or mask.values.dtype != np.int8:
        raise LayoutError(f"The mask of {name!r} is not an int8 series.")
    if mask.firstdate.frequency != firstdate.frequency or int(mask.firstdate) != int(firstdate):
        raise LayoutError(f"The mask of {name!r} does not start with the object.")
    codes = np.asarray(mask.values, dtype=np.int32)
    if len(codes) != length or bool(np.any((codes < 0) | (codes > 3))):
        raise LayoutError(f"The mask of {name!r} does not match the object.")
    if not bool(np.any(codes != MISSING_NORMAL)):
        raise LayoutError(f"The mask of {name!r} marks no missing observation.")
    return codes


def _codes(
    db: DataEconFile,
    catalog: str,
    name: str,
    attributes: dict[str, str],
    firstdate: MIT,
    length: int,
) -> np.ndarray:
    """The per-observation categories: the declared mask, or all normal without one."""
    declared = attributes.get("mask")
    if declared is None:
        if db.exists(mask_path(catalog, name)):
            raise LayoutError(f"{name!r} has a mask it does not declare.")
        return np.zeros(length, dtype=np.int32)
    if declared != "1":
        raise LayoutError(f"{name!r} declares an undefined mask value {declared!r}.")
    return _read_mask(db, catalog, name, firstdate, length)


def _read_firstdate(attributes: dict[str, str], frequency: Frequency, name: str) -> MIT:
    text = attributes["firstdate"]
    if not (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
        raise LayoutError(f"{name!r} stores an invalid first date.")
    return MIT(frequency, int(text))


def _text_list(value: Any, name: str) -> list[str]:
    if isinstance(value, StoredText):
        value = value.tolist()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LayoutError(f"{name!r} is not a text vector.")
    return list(value)


def _expect_class(info: Any, name: str, kind: str) -> None:
    if info.kind != kind:
        raise LayoutError(f"{name!r} is stored as a DataEcon {info.kind}, not a {kind}.")


def _read_scalar(
    db: DataEconFile, name: str, path: str, kind: str, attributes: dict[str, str]
) -> MigratedObject:
    required = ("kind", "value_frequency") if kind == "date" else ("kind",)
    _require(attributes, name, required, ("missing",))
    tag = attributes.get("missing")
    if tag is not None and tag not in CATEGORY_CODES:
        raise LayoutError(f"{name!r} carries an undefined missing tag {tag!r}.")
    category = MISSING_NORMAL if tag is None else CATEGORY_CODES[tag]
    stored = db.read_scalar(path)
    value_label = attributes.get("value_frequency")
    value: Any
    if kind == "precision":
        if not isinstance(stored, float) or isinstance(stored, np.float32):
            raise LayoutError(f"{name!r} is not a stored Float64.")
        value = float(stored)
    elif kind == "numeric":
        if not isinstance(stored, np.float32):
            raise LayoutError(f"{name!r} is not a stored Float32.")
        value = stored
    elif kind == "boolean":
        if not isinstance(stored, np.int8) or int(stored) not in (0, 1):
            raise LayoutError(f"{name!r} is not a stored Boolean.")
        value = bool(int(stored))
    elif kind == "string":
        if not isinstance(stored, str):
            raise LayoutError(f"{name!r} is not a stored string.")
        value = stored
    elif category == MISSING_NORMAL:
        if not isinstance(stored, MIT):
            raise LayoutError(f"{name!r} is not a stored date.")
        label_frequency(value_label or "")
        value = stored
    else:
        if isinstance(stored, (bool, MIT)) or not isinstance(stored, (int, np.integer)):
            raise LayoutError(f"{name!r} is not a stored missing date.")
        label_frequency(value_label or "")
        value = int(stored)
    if category != MISSING_NORMAL:
        placeholder = (
            (isinstance(value, float | np.floating) and math.isnan(float(value)))
            if kind in ("precision", "numeric")
            else value == ""
            if kind == "string"
            else value in (0, False)
        )
        if not placeholder:
            raise LayoutError(f"{name!r} is tagged missing but stores a value.")
        value = None
    return MigratedObject(name, kind, "scalar", value, category, None, value_label)


def _empty_carrier(
    db: DataEconFile,
    name: str,
    path: str,
    kind: str,
    attributes: dict[str, str],
    frequency: Frequency,
    info: Any,
) -> MIT | None:
    """Verify an empty carrier and return its explicit first date, if any."""
    mode = attributes["empty"]
    if mode not in EMPTY_MODES:
        raise LayoutError(f"{name!r} declares an undefined empty mode {mode!r}.")
    explicit = mode == "explicit_firstdate"
    if kind == "string" or not explicit:
        _expect_class(info, name, "array")
        payload = db.read_array(path)
        if kind == "string":
            length = len(_text_list(payload, name))
        else:
            if (
                not isinstance(payload, np.ndarray)
                or payload.ndim != 1
                or payload.dtype != _EMPTY_DTYPES[kind]
            ):
                raise LayoutError(f"{name!r} is not a zero-length {kind} array.")
            length = len(payload)
        if length != 0:
            raise LayoutError(f"{name!r} is declared empty but stores {length} elements.")
        return _read_firstdate(attributes, frequency, name) if explicit else None
    _expect_class(info, name, "series")
    stored = db.read_series(path)
    if kind == "date":
        if not isinstance(stored, StoredSeries) or stored.element.kind != "date":
            raise LayoutError(f"{name!r} is not an empty date carrier.")
        element_frequency = stored.element.frequency
        if element_frequency is None:
            raise LayoutError(f"{name!r} is not an empty date carrier.")
        _same_label(name, "value frequency", element_frequency, attributes["value_frequency"])
    elif not isinstance(stored, TSeries) or stored.values.dtype != _EMPTY_DTYPES[kind]:
        raise LayoutError(f"{name!r} is not an empty {kind} series.")
    if len(stored) != 0:
        raise LayoutError(f"{name!r} is declared empty but stores {len(stored)} observations.")
    if stored.firstdate.frequency != frequency:
        raise LayoutError(f"{name!r} is indexed by another frequency than its label.")
    return stored.firstdate


def _read_empty(
    db: DataEconFile,
    name: str,
    path: str,
    kind: str,
    attributes: dict[str, str],
    frequency: Frequency,
    info: Any,
) -> MigratedObject:
    required = ["kind", "frequency", "empty"]
    if kind == "date":
        required.append("value_frequency")
    if kind == "string" and attributes.get("empty") == "explicit_firstdate":
        required.append("firstdate")
    _require(attributes, name, required)
    label = attributes["frequency"]
    value_label = attributes.get("value_frequency")
    firstdate = _empty_carrier(db, name, path, kind, attributes, frequency, info)
    value: Any = None
    if firstdate is not None:
        if kind == "date":
            value = DateSeries(firstdate, (), label_frequency(value_label or ""))
        elif kind == "string":
            value = StringSeries(firstdate, ())
        else:
            value = TSeries(firstdate, np.empty(0, dtype=_EMPTY_DTYPES[kind]))
    codes = np.empty(0, dtype=np.int32)
    return MigratedObject(name, kind, "series", value, codes, label, value_label, True, "empty")


def _read_strings(
    db: DataEconFile,
    catalog: str,
    name: str,
    path: str,
    attributes: dict[str, str],
    frequency: Frequency,
    info: Any,
) -> MigratedObject:
    _require(attributes, name, ("kind", "frequency", "representation", "firstdate"), ("mask",))
    if attributes["representation"] != "text":
        raise LayoutError(f"{name!r} declares an undefined string representation.")
    _expect_class(info, name, "array")
    text = _text_list(db.read_array(path), name)
    firstdate = _read_firstdate(attributes, frequency, name)
    codes = _codes(db, catalog, name, attributes, firstdate, len(text))
    if any(text[i] != "" for i in range(len(text)) if codes[i]):
        raise LayoutError(f"{name!r} stores text at a missing observation.")
    strings = [None if codes[i] else text[i] for i in range(len(text))]
    value = StringSeries(firstdate, strings)
    label = attributes["frequency"]
    return MigratedObject(name, "string", "series", value, codes, label, None, False, "text")


def _read_dates(
    db: DataEconFile,
    catalog: str,
    name: str,
    stored: Any,
    attributes: dict[str, str],
) -> MigratedObject:
    _require(
        attributes, name, ("kind", "frequency", "representation", "value_frequency"), ("mask",)
    )
    label = attributes["frequency"]
    value_label = attributes["value_frequency"]
    value_frequency = label_frequency(value_label)
    representation = attributes["representation"]
    codes = _codes(db, catalog, name, attributes, stored.firstdate, len(stored))
    moments: list[MIT | None]
    if representation == "native":
        if not isinstance(stored, StoredSeries) or stored.element.kind != "date":
            raise LayoutError(f"{name!r} is not a date carrier.")
        if "mask" in attributes:
            raise LayoutError(f"{name!r} declares a mask on a complete date series.")
        moments = list(stored.tolist())
        if any(not isinstance(m, MIT) or m.frequency != value_frequency for m in moments):
            raise LayoutError(f"{name!r} holds elements of another frequency.")
    elif representation == "codes":
        if not isinstance(stored, TSeries) or stored.values.dtype != np.int64:
            raise LayoutError(f"{name!r} is not a series of moment codes.")
        if "mask" not in attributes:
            raise LayoutError(f"{name!r} stores moment codes without a mask.")
        if bool(np.any(stored.values[codes != MISSING_NORMAL] != 0)):
            raise LayoutError(f"{name!r} stores a moment at a missing observation.")
        moments = [
            None if codes[i] else MIT(value_frequency, int(v)) for i, v in enumerate(stored.values)
        ]
    else:
        raise LayoutError(f"{name!r} declares an undefined date representation.")
    dates = DateSeries(stored.firstdate, moments, value_frequency)
    return MigratedObject(
        name, "date", "series", dates, codes, label, value_label, False, representation
    )


def _read_plain(
    db: DataEconFile,
    catalog: str,
    name: str,
    kind: str,
    stored: Any,
    attributes: dict[str, str],
) -> MigratedObject:
    _require(attributes, name, ("kind", "frequency", "representation"), ("mask",))
    if attributes["representation"] != "native":
        raise LayoutError(f"{name!r} declares an undefined {kind} representation.")
    expected_dtype = {"precision": np.float64, "numeric": np.float32, "boolean": np.bool_}[kind]
    if not isinstance(stored, TSeries) or stored.values.dtype != expected_dtype:
        raise LayoutError(f"{name!r} is not a {kind} series.")
    codes = _codes(db, catalog, name, attributes, stored.firstdate, len(stored))
    at_missing = stored.values[codes != MISSING_NORMAL]
    placeholders = np.isnan(at_missing) if kind != "boolean" else ~at_missing
    if not bool(np.all(placeholders)):
        raise LayoutError(f"{name!r} stores a value at a missing observation.")
    label = attributes["frequency"]
    return MigratedObject(name, kind, "series", stored, codes, label, None, False, "native")


def read_migrated(db: DataEconFile, name: str, *, catalog: str = "/") -> MigratedObject:
    """Read one migrated object back into bridge terms, verifying the layout.

    The catalog must carry this layout version, the object's attributes
    must be exactly those the layout defines for its representation, and
    the stored object must agree with them (see the module note). A
    ``LayoutError`` names the first disagreement.
    """
    check_layout(db, catalog)
    path = catalog_path(catalog, name)
    attributes = _attributes(db, path)
    kind = attributes.get("kind")
    if kind not in KINDS:
        raise LayoutError(f"{name!r} carries no migration kind attribute.")
    info = db.object_info(path)
    if kind == "namelist":
        _require(attributes, name, ("kind", "representation"))
        if attributes["representation"] != "members":
            raise LayoutError(f"{name!r} declares an undefined namelist representation.")
        _expect_class(info, name, "array")
        members = _text_list(db.read_array(path), name)
        return MigratedObject(
            name, kind, "scalar", NameList(members), MISSING_NORMAL, representation="members"
        )
    if "frequency" not in attributes:
        _expect_class(info, name, "scalar")
        return _read_scalar(db, name, path, kind, attributes)
    frequency = label_frequency(attributes["frequency"])
    if "empty" in attributes:
        return _read_empty(db, name, path, kind, attributes, frequency, info)
    if kind == "string":
        return _read_strings(db, catalog, name, path, attributes, frequency, info)
    _expect_class(info, name, "series")
    stored = db.read_series(path)
    if not isinstance(stored, (TSeries, StoredSeries)):
        raise LayoutError(f"{name!r} is not a series.")
    if stored.firstdate.frequency != frequency:
        raise LayoutError(f"{name!r} is indexed by another frequency than its label.")
    if kind == "date":
        return _read_dates(db, catalog, name, stored, attributes)
    return _read_plain(db, catalog, name, kind, stored, attributes)


def list_migrated(db: DataEconFile, *, catalog: str = "/") -> list[str]:
    """Names of the migrated objects directly inside ``catalog`` (masks excluded)."""
    check_layout(db, catalog)
    names: list[str] = []
    for info in db.list_objects(catalog):
        if info.kind == "catalog":
            continue
        if attribute("kind") in db.get_attributes(info.path):
            names.append(info.name)
    return names


def migration_status(db: DataEconFile, *, catalog: str = "/") -> dict[str, str]:
    """The verified layout attributes of a destination catalog (version, status, counts)."""
    return check_layout(db, catalog)


__all__ = [
    "ATTRIBUTE_PREFIX",
    "CATALOG_ATTRIBUTES",
    "CATEGORY_CODES",
    "CATEGORY_NAMES",
    "EMPTY_MODES",
    "KINDS",
    "LAYOUT_VERSION",
    "MASK_CATALOG",
    "OBJECT_ATTRIBUTES",
    "STATUSES",
    "STATUS_COMPLETE",
    "STATUS_INCOMPLETE",
    "STATUS_STARTED",
    "LayoutError",
    "MigratedObject",
    "attribute",
    "catalog_path",
    "check_layout",
    "describe",
    "frequency_label",
    "label_frequency",
    "list_migrated",
    "mask_path",
    "migration_status",
    "read_migrated",
    "write_empty_series",
    "write_scalar",
    "write_series",
]
