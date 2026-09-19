# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Read/write semantics adapted from FAME.jl (Read.jl, Write.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""Raw object I/O with owning typed carriers that preserve native encodings.

Value kinds and their storage:

| kind      | scalar value (read)   | series values           |
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
bytes are kept untouched on the scalar.

Reads return exact-width NumPy scalars so that every bit pattern, including
NaN payloads used as missing encodings, survives a round trip. Writes accept
Python numbers too; a Python float written as ``numeric`` is rounded to
float32 by NumPy (pass ``numpy.float32`` to control the exact encoding).
Missing observations keep their native NC/NA/ND encodings. Reads allocate new
buffers; writes validate the caller's buffer and never modify or convert it.

Every Python-side validation of a write completes before the first native
call, so an invalid input never deletes or creates an object.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
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
from ._database import Database
from ._errors import HNOOBJ, DataValidationError, FameError
from ._native import MAX_OBSERVATIONS, RangeSpec, Sentinels, check_buffer
from ._objects import ObjectInfo, check_supported_class, query_info
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
    try:
        code = frequency_code(value)
    except (TypeError, ValueError):
        raise DataValidationError("A date value needs a supported frequency code.") from None
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

    def range(self) -> RangeSpec | None:
        if self.is_empty:
            return None
        return RangeSpec(self.frequency, self.first_index, self.first_index + len(self.values) - 1)


RawObject = RawScalar | RawSeries


# -- missing-value classification ------------------------------------------


def missing_type(database: Database, kind: str, value: Any) -> int:
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
    native: Any, key: int, name: bytes, kind: str, range_: RangeSpec | None, count: int
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


def read_object(
    database: Database,
    name: str | bytes | ObjectInfo,
    *,
    first_index: int | None = None,
    last_index: int | None = None,
) -> RawObject:
    """Read a scalar or a series (whole range or an explicit subrange).

    Metadata and data are obtained in one locked operation, so the type and
    range used to size the buffer are those of the object actually read. An
    ``ObjectInfo`` argument supplies the name only; metadata is re-queried.
    """
    text = name.name if isinstance(name, ObjectInfo) else to_native(name, what="object name")
    _check_index(first_index, "first_index")
    _check_index(last_index, "last_index")
    with database.operation("read object") as native:
        key = database.key
        info = query_info(native, key, text)
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
        first = info.first_index if first_index is None else int(first_index)
        last = info.last_index if last_index is None else int(last_index)
        if first < info.first_index or last > info.last_index:
            raise DataValidationError("The requested subrange lies outside the stored range.")
        range_ = RangeSpec(info.frequency, first, last)
        values = _read_values(native, key, info.name, kind, range_, range_.length)
        return RawSeries(kind, info.frequency, first, values, info.date_frequency)


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
    native: Any, key: int, name: bytes, obj: RawObject, range_: RangeSpec | None, payload: Any
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


def write_object(
    database: Database,
    name: str | bytes,
    obj: RawObject,
    *,
    replace: bool = False,
    basis: Any = Basis.DAILY,
    observed: Any = None,
) -> None:
    """Create an object and write its data.

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
        raise TypeError("write_object expects a RawScalar or RawSeries.")
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
            except FameError as error:
                if error.status != HNOOBJ:
                    raise
        native.new_object(
            key, text, class_code, frequency, obj.type_code, basis_code, observed_code
        )
        if isinstance(obj, RawSeries) and obj.is_empty:
            return
        _write_values(native, key, text, obj, range_, payload)


def delete_object(database: Database, name: str | bytes, *, missing_ok: bool = False) -> None:
    text = to_native(name, what="object name")
    with database.operation("delete object") as native:
        try:
            native.delete_object(database.key, text)
        except FameError as error:
            if not (missing_ok and error.status == HNOOBJ):
                raise


def series(
    kind: str,
    frequency: Any,
    first_index: int,
    values: Sequence[Any] | np.ndarray,
    *,
    date_frequency: Any = None,
) -> RawSeries:
    """Build a RawSeries, converting sequences (not arrays) to the exact dtype."""
    if kind == "string":
        return RawSeries("string", frequency_code(frequency), first_index, list(values))
    if isinstance(values, np.ndarray):
        data = values
    else:
        data = np.array(list(values), dtype=_DTYPES[kind])
    return RawSeries(
        kind,
        frequency_code(frequency),
        first_index,
        data,
        None if date_frequency is None else frequency_code(date_frequency),
    )


def scalar(kind: str, value: Any, *, date_frequency: Any = None) -> RawScalar:
    return RawScalar(
        kind, value, None if date_frequency is None else frequency_code(date_frequency)
    )


def case_frequency() -> int:
    return FREQUENCY_CASE


def value_type_code(kind_or_frequency: Any) -> int:
    return type_code(kind_or_frequency)
