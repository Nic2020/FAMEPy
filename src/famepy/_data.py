# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Read/write semantics adapted from FAME.jl (Read.jl, Write.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""Object data I/O: ``do_read`` and ``do_write`` over validated owning carriers.

Value kinds and their storage on a ``FameObject``:

| kind      | scalar data (read)    | series data             |
|-----------|-----------------------|-------------------------|
| precision | numpy float64 scalar  | float64 array           |
| numeric   | numpy float32 scalar  | float32 array           |
| boolean   | numpy int32 scalar    | int32 array (codes)     |
| date      | numpy int64 scalar    | int64 array (indices)   |
| string    | bytes                 | list of bytes           |
| namelist  | bytes                 | (scalar only)           |

A namelist value is the library's own text of the list (braces, comma
separated members). The library documents that it may lay that text out
differently from what was written, so ``namelist_members`` parses the
returned bytes into the ordered members under a strict grammar; the raw
bytes are kept untouched on the object.

Reads return exact-width NumPy scalars so that every bit pattern, including
NaN payloads used as missing encodings, survives a round trip. Writes accept
Python numbers too; a Python float written as ``numeric`` is rounded to
float32 by NumPy (pass ``numpy.float32`` to control the exact encoding).
Missing observations keep their native NC/NA/ND encodings. Reads allocate new
buffers; writes validate the caller's buffer and never modify or convert it.

``RawScalar`` and ``RawSeries`` are the internal validated carriers behind a
``FameObject``'s data: every Python-side validation of a write completes
while building them, before the first native call, so an invalid input never
deletes or creates an object.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

import numpy as np

from ._constants import (
    FREQUENCY_CASE,
    FREQUENCY_UNDEFINED,
    MISSING_NA,
    MISSING_NC,
    MISSING_ND,
    MISSING_NORMAL,
    Basis,
    ObjectClass,
    ObjectType,
    Observed,
    frequency_code,
    is_date_type,
    type_code,
)
from ._database import FameDatabase
from ._errors import HNOOBJ, DataValidationError, HLIError
from ._native import MAX_OBSERVATIONS, FameRange, Sentinels, check_buffer
from ._objects import FameObject, check_supported_class, query_info
from ._text import object_name, to_native

KINDS = ("precision", "numeric", "boolean", "date", "string", "namelist")
# Characters allowed in a namelist member as returned by the library: any
# printable ASCII except the structural characters and whitespace.
_MEMBER_EXCLUDED = frozenset(b"{}, \t\r\n")
_DTYPES: dict[str, np.dtype[Any]] = {
    "precision": np.dtype(np.float64),
    "numeric": np.dtype(np.float32),
    "boolean": np.dtype(np.int32),
    "date": np.dtype(np.int64),
}
_TYPE_CODES = {
    "precision": int(ObjectType.PRECISION),
    "numeric": int(ObjectType.NUMERIC),
    "boolean": int(ObjectType.BOOLEAN),
    "string": int(ObjectType.STRING),
    "namelist": int(ObjectType.NAMELIST),
}


def namelist_members(value: bytes) -> tuple[bytes, ...]:
    """The ordered members of a namelist's text, or ``DataValidationError``.

    The grammar is deliberately narrow: an opening brace, members separated
    by commas, a closing brace, with optional ASCII blanks around members and
    inside an empty list. A member is one or more printable ASCII bytes other
    than braces, commas and blanks; blanks inside a member, an empty member,
    a missing brace or anything after the closing brace is refused. Members
    are returned exactly as spelled (no case change, no de-duplication).
    """
    if not isinstance(value, bytes):
        raise DataValidationError("A namelist value must be bytes.")
    if len(value) < 2 or value[:1] != b"{" or value[-1:] != b"}":
        raise DataValidationError("A namelist must be enclosed in braces.")
    body = value[1:-1]
    if not body.strip(b" \t"):
        return ()
    members: list[bytes] = []
    for part in body.split(b","):
        member = part.strip(b" \t")
        if not member:
            raise DataValidationError("A namelist member is empty.")
        if any(byte in _MEMBER_EXCLUDED or not 0x20 < byte < 0x7F for byte in member):
            raise DataValidationError("A namelist member contains an unsupported byte.")
        members.append(member)
    return tuple(members)


def _kind_from_type(code: int) -> str:
    if is_date_type(code):
        return "date"
    try:
        return ObjectType(code).name.lower()
    except ValueError:
        raise DataValidationError(f"Unsupported object type code {code}.") from None


def _check_bytes(value: Any, what: str) -> bytes:
    if not isinstance(value, bytes):
        raise DataValidationError(f"{what} must be bytes.")
    if b"\0" in value:
        raise DataValidationError(f"{what} cannot contain NUL bytes.")
    return value


def _encode_scalar(kind: str, value: Any) -> np.ndarray:
    """Encode one numeric scalar as a one-element exact-dtype array or refuse."""
    dtype = _DTYPES[kind]
    try:
        if dtype.kind == "i":
            number = int(value)
            info = np.iinfo(dtype)
            if not info.min <= number <= info.max:
                raise DataValidationError(f"A {kind} scalar must fit {dtype.name}.")
            return np.array([number], dtype=dtype)
        with np.errstate(over="raise", invalid="ignore"):
            if isinstance(value, np.floating):
                return np.array([value], dtype=dtype)
            return np.array([float(value)], dtype=dtype)
    except (OverflowError, ValueError, TypeError, FloatingPointError) as error:
        if isinstance(error, DataValidationError):
            raise
        raise DataValidationError(f"The value cannot be encoded as {dtype.name}.") from None


def _date_frequency(value: Any) -> int:
    """The value frequency of a date object: a defined calendar frequency.

    The case frequency indexes series but cannot type a date value (the
    library refuses it when creating the object), so it is refused here,
    before any native call, rather than remapped to a calendar or a number.
    """
    try:
        code = frequency_code(value)
    except (TypeError, ValueError):
        raise DataValidationError("A date value needs a supported frequency code.") from None
    if code == FREQUENCY_CASE:
        raise DataValidationError(
            "The case frequency is an index frequency, not a date value type."
        )
    if not is_date_type(code):
        raise DataValidationError("A date value needs a defined frequency.")
    return code


@dataclass(frozen=True)
class RawScalar:
    """One scalar value with its native encoding preserved."""

    kind: str
    value: Any
    date_frequency: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise DataValidationError("Unknown scalar kind.")
        if self.kind == "date":
            if self.date_frequency is None:
                raise DataValidationError("A date scalar needs its value frequency.")
            object.__setattr__(self, "date_frequency", _date_frequency(self.date_frequency))
            if isinstance(self.value, bool) or not isinstance(self.value, (int, np.integer)):
                raise DataValidationError("A date scalar holds a 64-bit integer index.")
        elif self.date_frequency is not None:
            raise DataValidationError("Only date scalars carry a value frequency.")
        if self.kind in ("string", "namelist"):
            _check_bytes(self.value, f"A {self.kind} scalar value")
            return
        if self.kind == "boolean" and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, np.integer))
        ):
            raise DataValidationError("A boolean scalar holds a 32-bit integer code.")
        if self.kind in ("precision", "numeric") and (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float, np.floating, np.integer))
        ):
            raise DataValidationError("Numeric scalars hold floats.")
        # Refuse values that cannot be encoded at construction, not at write time.
        _encode_scalar(self.kind, self.value)

    @property
    def type_code(self) -> int:
        if self.kind == "date":
            assert self.date_frequency is not None
            return self.date_frequency
        return _TYPE_CODES[self.kind]


@dataclass(frozen=True)
class RawSeries:
    """A series with frequency, first index and owning typed values.

    ``values`` is an exact-dtype one-dimensional array, or a list of bytes for
    string series. A zero-length series is truly empty (NC endpoints). The
    buffer is validated again immediately before a write, so a caller that
    resizes it after construction gets an error instead of a native call.
    """

    kind: str
    frequency: int
    first_index: int
    values: Any
    date_frequency: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS or self.kind == "namelist":
            raise DataValidationError("Series kinds are precision, numeric, boolean, date, string.")
        if isinstance(self.first_index, bool) or not isinstance(self.first_index, int):
            raise DataValidationError("The first index must be an integer.")
        if not -(2**63) <= self.first_index < 2**63:
            raise DataValidationError("The first index must fit a signed 64-bit integer.")
        if self.kind == "date":
            if self.date_frequency is None:
                raise DataValidationError("A date series needs its value frequency.")
            object.__setattr__(self, "date_frequency", _date_frequency(self.date_frequency))
        elif self.date_frequency is not None:
            raise DataValidationError("Only date series carry a value frequency.")
        if self.kind == "string":
            if not isinstance(self.values, (list, tuple)):
                raise DataValidationError("String series values must be a list of bytes.")
            for item in self.values:
                _check_bytes(item, "A string series value")
            object.__setattr__(self, "values", list(self.values))
        else:
            check_buffer(self.values, _DTYPES[self.kind], None, what=f"{self.kind} series values")
        if len(self.values) > MAX_OBSERVATIONS:
            raise DataValidationError("Series length exceeds the supported bound.")
        if len(self.values) and self.first_index + len(self.values) - 1 >= 2**63:
            raise DataValidationError("The series range overflows a signed 64-bit index.")
        object.__setattr__(self, "_declared_length", len(self.values))

    def __len__(self) -> int:
        return len(self.values)

    @property
    def is_empty(self) -> bool:
        return len(self.values) == 0

    @property
    def last_index(self) -> int | None:
        return None if self.is_empty else self.first_index + len(self.values) - 1

    @property
    def type_code(self) -> int:
        if self.kind == "date":
            assert self.date_frequency is not None
            return self.date_frequency
        return _TYPE_CODES[self.kind]

    def range(self) -> FameRange | None:
        if self.is_empty:
            return None
        return FameRange(self.frequency, self.first_index, self.first_index + len(self.values) - 1)


RawObject = RawScalar | RawSeries


# -- missing-value classification ------------------------------------------


def missing_type(database: FameDatabase, kind: str, value: Any) -> int:
    """Classify one value with the library's own classifier (0 normal, 1 NC, 2 NA, 3 ND)."""
    if kind not in KINDS or kind == "namelist":
        raise ValueError("Classification supports precision, numeric, boolean, date and string.")
    with database.session.operation("missing classification") as native:
        return native.missing_type(kind, value)


def _sentinel_triplet(kind: str, sentinels: Sentinels) -> tuple[Any, Any, Any]:
    if kind == "precision":
        return sentinels.precision_nc, sentinels.precision_na, sentinels.precision_nd
    if kind == "numeric":
        return sentinels.numeric_nc, sentinels.numeric_na, sentinels.numeric_nd
    if kind == "boolean":
        return sentinels.boolean_nc, sentinels.boolean_na, sentinels.boolean_nd
    if kind == "date":
        return sentinels.index_nc, sentinels.index_na, sentinels.index_nd
    if kind == "string":
        return sentinels.string_nc, sentinels.string_na, sentinels.string_nd
    raise ValueError("Unknown kind.")


def classify_by_sentinel(values: Any, kind: str, sentinels: Sentinels) -> np.ndarray:
    """Bitwise classification against the native sentinel globals.

    Returns int32 codes (0 normal, 1 NC, 2 NA, 3 ND). Floating sentinels are
    compared by bit pattern so NaN-encoded sentinels compare exactly. The native
    validation campaign checks this classifier against the library's own
    per-value classifier before it is relied on for vendor data.
    """
    nc, na, nd = _sentinel_triplet(kind, sentinels)
    if kind == "string":
        return np.array(
            [
                MISSING_NC
                if v == nc
                else MISSING_NA
                if v == na
                else MISSING_ND
                if v == nd
                else MISSING_NORMAL
                for v in values
            ],
            dtype=np.int32,
        )
    array = check_buffer(values, _DTYPES[kind], None, what="values")
    if kind in ("precision", "numeric"):
        width = np.uint64 if kind == "precision" else np.uint32
        bits = array.view(width)
        codes = np.zeros(array.shape[0], dtype=np.int32)
        for code, sentinel in ((MISSING_NC, nc), (MISSING_NA, na), (MISSING_ND, nd)):
            pattern = np.array(sentinel, dtype=array.dtype).view(width)
            codes[bits == pattern] = code
        return codes
    codes = np.zeros(array.shape[0], dtype=np.int32)
    for code, sentinel in ((MISSING_NC, nc), (MISSING_NA, na), (MISSING_ND, nd)):
        codes[array == sentinel] = code
    return codes


def sentinel_value(kind: str, category: int, sentinels: Sentinels) -> Any:
    nc, na, nd = _sentinel_triplet(kind, sentinels)
    if category == MISSING_NC:
        return nc
    if category == MISSING_NA:
        return na
    if category == MISSING_ND:
        return nd
    raise ValueError("Category must be 1 (NC), 2 (NA) or 3 (ND).")


# -- reading -----------------------------------------------------------------


def _read_values(
    native: Any, key: int, name: bytes, kind: str, range_: FameRange | None, count: int
) -> Any:
    if kind == "string":
        return native.get_strings(key, name, range_, count)
    out = np.empty(count, dtype=_DTYPES[kind])
    if kind == "precision":
        native.get_precisions(key, name, range_, out)
    elif kind == "numeric":
        native.get_numerics(key, name, range_, out)
    elif kind == "boolean":
        native.get_booleans(key, name, range_, out)
    else:
        native.get_dates(key, name, range_, out)
    return out


def _check_index(value: int | None, what: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise DataValidationError(f"{what} must be an integer index.")
    if not -(2**63) <= int(value) < 2**63:
        raise DataValidationError(f"{what} must fit a signed 64-bit index.")


def read_named(
    database: FameDatabase,
    name: str | bytes,
    *,
    first_index: int | None = None,
    last_index: int | None = None,
) -> RawObject:
    """Read a scalar or a series by name (whole range or an explicit subrange).

    Internal carrier read: metadata and data are obtained in one locked
    operation, so the type and range used to size the buffer are those of
    the object actually read. ``do_read`` is the public form.
    """
    text = to_native(name, what="object name")
    _check_index(first_index, "first_index")
    _check_index(last_index, "last_index")
    with database.operation("read object") as native:
        info = query_info(native, database.key, text)
        return _read_locked(native, database, info, first_index, last_index)


def _read_locked(
    native: Any,
    database: FameDatabase,
    info: FameObject,
    first_index: int | None,
    last_index: int | None,
) -> RawObject:
    key = database.key
    check_supported_class(info)
    kind = _kind_from_type(info.type_code)
    if info.is_scalar:
        if first_index is not None or last_index is not None:
            raise DataValidationError("A scalar has no range to select.")
        if kind == "namelist":
            return RawScalar("namelist", native.get_namelist(key, info.name))
        values = _read_values(native, key, info.name, kind, None, 1)
        return RawScalar(kind, values[0], info.date_frequency)
    index_nc = database.session.sentinels.index_nc
    if kind == "namelist":
        raise DataValidationError("A namelist is always a scalar object.")
    if info.is_empty(index_nc):
        if first_index is not None or last_index is not None:
            raise DataValidationError("An empty series has no observations to select.")
        empty: Any = [] if kind == "string" else np.empty(0, dtype=_DTYPES[kind])
        return RawSeries(kind, info.frequency, index_nc, empty, info.date_frequency)
    assert info.first_index is not None and info.last_index is not None
    first = info.first_index if first_index is None else int(first_index)
    last = info.last_index if last_index is None else int(last_index)
    if first < info.first_index or last > info.last_index:
        raise DataValidationError("The requested subrange lies outside the stored range.")
    range_ = FameRange(info.frequency, first, last)
    values = _read_values(native, key, info.name, kind, range_, range_.length)
    return RawSeries(kind, info.frequency, first, values, info.date_frequency)


def do_read(obj: FameObject, db: FameDatabase) -> FameObject:
    """Read the object's data from the database into ``obj`` and return it.

    The object's class, type and frequency must agree with what the database
    holds now (metadata is re-queried inside the locked read, so an object
    replaced since ``quick_info`` is refused rather than read as something
    else). For a series, ``first_index`` and ``last_index`` select the range:
    ``None`` (or the NC index) means the stored endpoint, and an explicit
    endpoint must lie inside the stored range, which is how a subrange is
    read. ``obj`` is modified only after the read succeeded: its range is
    set to what was read and ``data`` holds a new owning buffer.
    """
    if not isinstance(obj, FameObject):
        raise TypeError("do_read expects a FameObject; get one from quick_info or listdb.")
    if not isinstance(db, FameDatabase):
        raise TypeError("do_read expects a FameDatabase as its second argument.")
    with db.operation("read object") as native:
        info = query_info(native, db.key, obj.name)
        check_supported_class(info)
        if (obj.class_code, obj.type_code, obj.frequency) != (
            info.class_code,
            info.type_code,
            info.frequency,
        ):
            raise DataValidationError(
                "The stored object's class, type or frequency differ from this FameObject; "
                "query it again with quick_info."
            )
        index_nc = db.session.sentinels.index_nc
        first: int | None = None
        last: int | None = None
        if info.is_series and not info.is_empty(index_nc):
            if obj.first_index is not None and obj.first_index != index_nc:
                first = obj.first_index
            if obj.last_index is not None and obj.last_index != index_nc:
                last = obj.last_index
        elif info.is_series:
            for endpoint in (obj.first_index, obj.last_index):
                if endpoint is not None and endpoint != index_nc:
                    raise DataValidationError("An empty series has no observations to select.")
        raw = _read_locked(native, db, info, first, last)
    fill_object(obj, raw, info)
    return obj


def fill_object(obj: FameObject, raw: RawObject, info: FameObject | None = None) -> None:
    """Store a carrier's range and data on ``obj`` (after a successful read)."""
    if isinstance(raw, RawSeries):
        obj.first_index = raw.first_index
        obj.last_index = raw.first_index if raw.is_empty else raw.last_index
        obj.data = raw.values
    else:
        if info is not None:
            obj.first_index, obj.last_index = info.first_index, info.last_index
        obj.data = raw.value


def object_from_raw(name: str | bytes, raw: RawObject) -> FameObject:
    """A ``FameObject`` carrying a converted carrier (the result of ``refame``)."""
    if isinstance(raw, RawSeries):
        return FameObject(
            name,
            ObjectClass.SERIES,
            raw.type_code,
            raw.frequency,
            raw.first_index,
            raw.first_index if raw.is_empty else raw.last_index,
            raw.values,
        )
    return FameObject(name, ObjectClass.SCALAR, raw.type_code, FREQUENCY_UNDEFINED, 0, 0, raw.value)


def raw_of(obj: FameObject, index_nc: int) -> RawObject:
    """The validated carrier of a ``FameObject``'s data, or ``DataValidationError``.

    Every check runs here, before any native call: class, kind, frequency,
    the presence and shape of the data, the range against the data length.
    """
    if not isinstance(obj, FameObject):
        raise TypeError("Expected a FameObject.")
    check_supported_class(obj)
    if obj.data is None:
        raise DataValidationError("The FameObject has no data; read it with do_read first.")
    if obj.type_code == int(ObjectType.DATE):
        raise DataValidationError(
            "A date object is typed by the frequency of its values; pass that frequency as "
            "the type."
        )
    kind = _kind_from_type(obj.type_code)
    date_frequency = obj.date_frequency
    if obj.is_scalar:
        if obj.frequency != FREQUENCY_UNDEFINED:
            raise DataValidationError("A scalar has the undefined frequency.")
        return RawScalar(kind, obj.data, date_frequency)
    if kind == "namelist":
        raise DataValidationError("A namelist is always a scalar object.")
    values = obj.data
    if isinstance(values, (str, bytes)):
        raise DataValidationError("Series data must be an array or a list of bytes.")
    try:
        count = len(values)
    except TypeError:
        raise DataValidationError("Series data must be an array or a list of bytes.") from None
    if count == 0:
        # A truly empty series stores no first date; the endpoints are ignored.
        return RawSeries(kind, obj.frequency, index_nc, values, date_frequency)
    if obj.first_index is None or obj.first_index == index_nc:
        raise DataValidationError("A series with data needs its first index.")
    if obj.last_index is not None and obj.last_index != obj.first_index + count - 1:
        raise DataValidationError("The last index disagrees with the first index and the data.")
    return RawSeries(kind, obj.frequency, obj.first_index, values, date_frequency)


# -- writing -----------------------------------------------------------------


def _prepare(obj: RawObject) -> Any:
    """Produce the exact native payload, refusing anything invalid before any call."""
    kind = obj.kind
    if isinstance(obj, RawScalar):
        if kind in ("string", "namelist"):
            return _check_bytes(obj.value, f"A {kind} scalar value")
        return _encode_scalar(kind, obj.value)
    declared = getattr(obj, "_declared_length", None)
    if declared is not None and len(obj.values) != declared:
        raise DataValidationError("The series buffer changed length after construction.")
    if kind == "string":
        return [_check_bytes(item, "A string series value") for item in obj.values]
    return check_buffer(obj.values, _DTYPES[kind], len(obj.values), what=f"{kind} series values")


def _write_values(
    native: Any, key: int, name: bytes, obj: RawObject, range_: FameRange | None, payload: Any
) -> None:
    kind = obj.kind
    if isinstance(obj, RawScalar):
        if kind == "namelist":
            native.write_namelist(key, name, payload)
            return
        if kind == "string":
            native.write_strings(key, name, None, [payload])
            return
    if kind == "precision":
        native.write_precisions(key, name, range_, payload)
    elif kind == "numeric":
        native.write_numerics(key, name, range_, payload)
    elif kind == "boolean":
        native.write_booleans(key, name, range_, payload)
    elif kind == "date":
        native.write_dates(key, name, range_, obj.type_code, payload)
    else:
        native.write_strings(key, name, range_, payload)


def _default_observed(obj: RawObject) -> Observed:
    # The reference marks floating data as summed and everything else undefined.
    return Observed.SUMMED if obj.kind in ("precision", "numeric") else Observed.UNDEFINED


def attribute_codes(basis: Any = None, observed: Any = None) -> tuple[int | None, int | None]:
    """Validate the optional ``basis``/``observed`` attributes without any native call.

    ``None`` keeps the default (daily basis; observed summed for floating
    data, undefined otherwise). Members, their integer values or their names
    are accepted; anything else raises ``ValueError``. Every writer validates
    attributes through this function before it opens or mutates anything.
    """

    def code(value: Any, table: type[enum.IntEnum], what: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"A {what} attribute cannot be a Boolean.")
        if isinstance(value, str):
            key = value.strip().upper().replace("-", "_").replace(" ", "_")
            if key not in table.__members__:
                raise ValueError(f"Unknown {what} attribute name.")
            return int(table[key])
        if isinstance(value, int):
            try:
                return int(table(value))
            except ValueError:
                raise ValueError(f"Unknown {what} attribute code.") from None
        raise ValueError(f"A {what} attribute must be a member, code or name.")

    return code(basis, Basis, "basis"), code(observed, Observed, "observed")


def write_raw(
    database: FameDatabase,
    name: str | bytes,
    obj: RawObject,
    *,
    replace: bool = False,
    basis: Any = None,
    observed: Any = None,
) -> None:
    """Create an object from a carrier and write its data (internal form of ``do_write``).

    Name, kind, frequency, attributes, value encodings and buffers are all
    validated before the first native call: an invalid Python input makes no
    mutating call. With ``replace=True`` an existing object of the same name
    is deleted first, as the reference does. That deletion is destructive and
    is not undone if the subsequent native creation or data write fails: the
    old object is gone and the new one may be absent or partially written.
    Without ``replace`` an existing name surfaces the library's own status.
    Nothing is posted by this function.
    """
    text = object_name(name)
    if not isinstance(obj, (RawScalar, RawSeries)):
        raise TypeError("write_raw expects a RawScalar or RawSeries.")
    if not database.is_writable:
        raise DataValidationError("The database was opened read-only.")
    if isinstance(obj, RawSeries):
        frequency = frequency_code(obj.frequency)
        if frequency == FREQUENCY_UNDEFINED:
            raise DataValidationError("A series needs a defined frequency.")
        class_code = int(ObjectClass.SERIES)
    else:
        frequency = FREQUENCY_UNDEFINED
        class_code = int(ObjectClass.SCALAR)
    basis_code, observed_code = attribute_codes(basis, observed)
    if basis_code is None:
        basis_code = int(Basis.DAILY)
    if observed_code is None:
        observed_code = int(_default_observed(obj))
    payload = _prepare(obj)
    range_ = obj.range() if isinstance(obj, RawSeries) else None
    with database.operation("write object") as native:
        key = database.key
        if replace:
            try:
                native.delete_object(key, text)
            except HLIError as error:
                if error.status != HNOOBJ:
                    raise
        native.new_object(
            key, text, class_code, frequency, obj.type_code, basis_code, observed_code
        )
        if isinstance(obj, RawSeries) and obj.is_empty:
            return
        _write_values(native, key, text, obj, range_, payload)


def do_write(
    obj: FameObject,
    db: FameDatabase,
    *,
    replace: bool = True,
    basis: Any = None,
    observed: Any = None,
) -> None:
    """Create the object in the database and write its data.

    Every check (name, class, kind, frequency, range, attributes, value
    encodings, buffers) runs before the first native call, so an invalid
    object never deletes or creates anything. Like the reference, the default
    deletes an existing object of the same name first. Pass ``replace=False``
    to refuse an existing name with the library's own status.
    A replacement is not transactional: a native failure after the deletion
    leaves the old object gone. ``basis`` and ``observed`` accept attribute
    members, codes or names (daily basis; observed summed for floating data
    and undefined otherwise, as the reference writes). Nothing is posted;
    call ``postdb`` before closing to keep the changes.
    """
    if not isinstance(obj, FameObject):
        raise TypeError("do_write expects a FameObject; use refame(name, value).")
    if not isinstance(db, FameDatabase):
        raise TypeError("do_write expects a FameDatabase as its second argument.")
    raw = raw_of(obj, db.session.sentinels.index_nc)
    write_raw(db, obj.name, raw, replace=replace, basis=basis, observed=observed)


def delete_object(db: FameDatabase, name: str | bytes, *, missing_ok: bool = False) -> None:
    """Delete an object; ``missing_ok`` ignores the absent-object status. Nothing is posted."""
    text = to_native(name, what="object name")
    with db.operation("delete object") as native:
        try:
            native.delete_object(db.key, text)
        except HLIError as error:
            if not (missing_ok and error.status == HNOOBJ):
                raise


def case_frequency() -> int:
    return FREQUENCY_CASE


def value_type_code(kind_or_frequency: Any) -> int:
    return type_code(kind_or_frequency)
