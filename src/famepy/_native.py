# SPDX-License-Identifier: MIT
"""Native call layer: typed methods over the declared prototypes.

``CtypesNative`` marshals Python values, NumPy buffers and byte strings to the
candidate CHLI declarations. Every bulk buffer is validated (dtype, width,
native byte order, contiguity, length) before a native call and is never
converted or mutated on the caller's behalf. ``NativeInterface`` is the
injectable boundary used by the fake backend in tests.
"""

from __future__ import annotations

import ctypes as ct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from ._abi import GLOBAL_TYPES, GLOBALS, PRESENCE_ONLY, SIGNATURES, C, FameRange, S
from ._binding import Binding
from ._constants import NAME_CAPACITY, NAMELIST_ALL
from ._errors import DataValidationError, check_status

# Allocation guards. CHLI ranges use 64-bit indices, but a single read or write
# larger than this is refused before any allocation or native call.
MAX_OBSERVATIONS = 2**31 - 1
MAX_STRING_BYTES = 2**28
MAX_EXTENDED_ERROR_BYTES = 2**16


@dataclass(frozen=True)
class RangeSpec:
    """A validated FAME range: frequency code plus inclusive 64-bit endpoints."""

    frequency: int
    first: int
    last: int

    def __post_init__(self) -> None:
        for value in (self.first, self.last):
            if not isinstance(value, int) or isinstance(value, bool):
                raise DataValidationError("Range endpoints must be integers.")
            if not -(2**63) <= value < 2**63:
                raise DataValidationError("Range endpoints must fit a signed 64-bit index.")
        if not isinstance(self.frequency, int) or not 0 <= self.frequency < 2**31:
            raise DataValidationError("Range frequency must be a non-negative 32-bit code.")
        if self.last < self.first:
            raise DataValidationError("Range end precedes its start.")
        if self.last - self.first + 1 > MAX_OBSERVATIONS:
            raise DataValidationError("Range exceeds the supported number of observations.")

    @property
    def length(self) -> int:
        return self.last - self.first + 1

    def to_ctypes(self) -> FameRange:
        return FameRange(self.frequency, self.first, self.last)


@dataclass(frozen=True)
class Sentinels:
    """Native missing-value globals read after initialization."""

    index_nc: int
    index_na: int
    index_nd: int
    precision_nc: float
    precision_na: float
    precision_nd: float
    # float32 sentinels are kept as numpy.float32 so their exact bits survive.
    numeric_nc: Any
    numeric_na: Any
    numeric_nd: Any
    boolean_nc: int
    boolean_na: int
    boolean_nd: int
    string_nc: bytes
    string_na: bytes
    string_nd: bytes


@dataclass(frozen=True)
class WildcardEntry:
    status: int
    name: bytes
    class_code: int
    type_code: int
    frequency: int
    first_index: int
    last_index: int
    returned_length: int


class NativeInterface(Protocol):
    """Typed CHLI operations. Text is bytes; bulk data is NumPy or byte lists."""

    def has_symbol(self, name: str) -> bool: ...
    def initialize(self) -> None: ...
    def finalize(self) -> None: ...
    def version(self) -> float: ...
    def sentinels(self) -> Sentinels: ...
    def open_work(self) -> int: ...
    def open_database(self, name: bytes, mode: int) -> int: ...
    def close_database(self, key: int) -> None: ...
    def post_database(self, key: int) -> None: ...
    def quick_info(self, key: int, name: bytes) -> tuple[int, int, int, int, int]: ...
    def new_object(
        self,
        key: int,
        name: bytes,
        class_code: int,
        frequency: int,
        type_code: int,
        basis: int,
        observed: int,
    ) -> None: ...
    def delete_object(self, key: int, name: bytes) -> None: ...
    def get_precisions(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None: ...
    def get_numerics(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None: ...
    def get_booleans(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None: ...
    def get_dates(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None: ...
    def get_strings(
        self, key: int, name: bytes, range_: RangeSpec | None, count: int
    ) -> list[bytes]: ...
    def write_precisions(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None: ...
    def write_numerics(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None: ...
    def write_booleans(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None: ...
    def write_dates(
        self,
        key: int,
        name: bytes,
        range_: RangeSpec | None,
        type_code: int,
        values: np.ndarray,
    ) -> None: ...
    def write_strings(
        self, key: int, name: bytes, range_: RangeSpec | None, values: Sequence[bytes]
    ) -> None: ...
    def get_namelist(self, key: int, name: bytes) -> bytes: ...
    def write_namelist(self, key: int, name: bytes, value: bytes) -> None: ...
    def set_option(self, name: bytes, value: bytes) -> None: ...
    def init_wildcard(self, key: int, pattern: bytes) -> int: ...
    def next_wildcard(self, wildcard_key: int, capacity: int) -> WildcardEntry: ...
    def free_wildcard(self, wildcard_key: int) -> None: ...
    def execute(self, command: bytes) -> int: ...
    def missing_type(self, kind: str, value: Any) -> int: ...
    def index_to_year_period(self, frequency: int, index: int) -> tuple[int, int]: ...
    def year_period_to_index(self, frequency: int, year: int, period: int) -> int: ...


def check_buffer(
    values: Any,
    dtype: Any,
    length: int | None,
    *,
    what: str,
    writable: bool = False,
) -> np.ndarray:
    """Validate a NumPy buffer for a native call without copying or converting."""
    if not isinstance(values, np.ndarray):
        raise DataValidationError(f"{what} must be a NumPy array, not {type(values).__name__}.")
    expected = np.dtype(dtype)
    if values.dtype != expected or not values.dtype.isnative:
        raise DataValidationError(
            f"{what} must have native-endian dtype {expected.name}, not {values.dtype.str}."
        )
    if values.ndim != 1:
        raise DataValidationError(f"{what} must be one-dimensional.")
    if not values.flags.c_contiguous:
        raise DataValidationError(f"{what} must be C-contiguous.")
    if writable and not values.flags.writeable:
        raise DataValidationError(f"{what} must be writable.")
    if length is not None and values.shape[0] != length:
        raise DataValidationError(
            f"{what} has {values.shape[0]} elements but the range holds {length}."
        )
    if values.shape[0] > MAX_OBSERVATIONS:
        raise DataValidationError(f"{what} exceeds the supported number of observations.")
    return values


def _range_argument(range_: RangeSpec | None, count: int) -> tuple[Any, Any]:
    """Return (ctypes range or None, owner) and enforce scalar/series lengths."""
    if range_ is None:
        if count != 1:
            raise DataValidationError("A scalar operation needs exactly one element.")
        return None, None
    if range_.length != count:
        raise DataValidationError("Buffer length does not match the range length.")
    native = range_.to_ctypes()
    return ct.byref(native), native


def _int32(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise DataValidationError(f"{what} must be an integer.")
    result = int(value)
    if not -(2**31) <= result < 2**31:
        raise DataValidationError(f"{what} does not fit a signed 32-bit integer.")
    return result


def _int64(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise DataValidationError(f"{what} must be an integer.")
    result = int(value)
    if not -(2**63) <= result < 2**63:
        raise DataValidationError(f"{what} does not fit a signed 64-bit integer.")
    return result


def _name(name: Any) -> bytes:
    if not isinstance(name, bytes) or b"\0" in name:
        raise DataValidationError("Native names must be NUL-free bytes.")
    return name


def read_extended_error(
    query_length: Callable[[], int],
    fetch: Callable[[Any], None],
    *,
    limit: int = MAX_EXTENDED_ERROR_BYTES,
) -> bytes:
    """Dynamic-capacity retrieval mechanics: length query, then a bounded fetch.

    The buffer is initialized as a NUL-terminated non-NUL string of the reported
    length, as the vendor help describes, and the result is cut at the first NUL.
    The package supplies no vendor declaration for these callables.
    """
    length = query_length()
    if not isinstance(length, int) or isinstance(length, bool):
        raise DataValidationError("The extended error length must be an integer.")
    if length < 0 or length > limit:
        raise DataValidationError("The extended error length is outside the accepted bound.")
    buffer = ct.create_string_buffer(b" " * length, length + 1)
    fetch(buffer)
    return bytes(buffer.value)


class CtypesNative:
    """NativeInterface implementation over the candidate ABI table."""

    def __init__(self, library: Any) -> None:
        self._binding = Binding(library)
        self._library = library

    @property
    def binding(self) -> Binding:
        return self._binding

    def has_symbol(self, name: str) -> bool:
        if name not in SIGNATURES and name not in PRESENCE_ONLY and name not in GLOBALS:
            raise ValueError("Only declared or presence-only symbols can be probed.")
        return self._binding.has_symbol(name)

    # -- lifetime --------------------------------------------------------

    def initialize(self) -> None:
        self._binding.call("cfmini")

    def finalize(self) -> None:
        self._binding.call("cfmfin")

    def version(self) -> float:
        value = ct.c_float(0.0)
        self._binding.call("cfmver", ct.byref(value))
        return float(value.value)

    def sentinels(self) -> Sentinels:
        def read(name: str) -> Any:
            return GLOBAL_TYPES[name].in_dll(self._library, name)

        def text(name: str) -> bytes:
            raw = bytes(read(name).raw)
            return raw.split(b"\0", 1)[0]

        def single(name: str) -> Any:
            # Copy the four bytes directly; widening through a Python float
            # would quiet a signaling NaN and change the encoding.
            return np.frombuffer(bytes(read(name)), dtype=np.float32)[0]

        return Sentinels(
            index_nc=int(read("FAME_INDEX_NC").value),
            index_na=int(read("FAME_INDEX_NA").value),
            index_nd=int(read("FAME_INDEX_ND").value),
            precision_nc=float(read("FPRCNC").value),
            precision_na=float(read("FPRCNA").value),
            precision_nd=float(read("FPRCND").value),
            numeric_nc=single("FNUMNC"),
            numeric_na=single("FNUMNA"),
            numeric_nd=single("FNUMND"),
            boolean_nc=int(read("FBOONC").value),
            boolean_na=int(read("FBOONA").value),
            boolean_nd=int(read("FBOOND").value),
            string_nc=text("FSTRNC"),
            string_na=text("FSTRNA"),
            string_nd=text("FSTRND"),
        )

    # -- databases -------------------------------------------------------

    def open_work(self) -> int:
        key = ct.c_int32(-1)
        self._binding.call("cfmopwk", ct.byref(key))
        return int(key.value)

    def open_database(self, name: bytes, mode: int) -> int:
        key = ct.c_int32(-1)
        self._binding.call("cfmopdb", ct.byref(key), _name(name), _int32(mode, "mode"))
        return int(key.value)

    def close_database(self, key: int) -> None:
        self._binding.call("cfmcldb", _int32(key, "database key"))

    def post_database(self, key: int) -> None:
        self._binding.call("cfmpodb", _int32(key, "database key"))

    # -- objects ---------------------------------------------------------

    def quick_info(self, key: int, name: bytes) -> tuple[int, int, int, int, int]:
        class_code, type_code, frequency = ct.c_int32(-1), ct.c_int32(-1), ct.c_int32(-1)
        first, last = ct.c_int64(-1), ct.c_int64(-1)
        self._binding.call(
            "fame_quick_info",
            _int32(key, "database key"),
            _name(name),
            ct.byref(class_code),
            ct.byref(type_code),
            ct.byref(frequency),
            ct.byref(first),
            ct.byref(last),
        )
        return (
            int(class_code.value),
            int(type_code.value),
            int(frequency.value),
            int(first.value),
            int(last.value),
        )

    def new_object(
        self,
        key: int,
        name: bytes,
        class_code: int,
        frequency: int,
        type_code: int,
        basis: int,
        observed: int,
    ) -> None:
        self._binding.call(
            "cfmnwob",
            _int32(key, "database key"),
            _name(name),
            _int32(class_code, "class"),
            _int32(frequency, "frequency"),
            _int32(type_code, "type"),
            _int32(basis, "basis"),
            _int32(observed, "observed"),
        )

    def delete_object(self, key: int, name: bytes) -> None:
        self._binding.call("cfmdlob", _int32(key, "database key"), _name(name))

    # -- bulk numeric/date data -----------------------------------------

    def _bulk(
        self,
        function: str,
        key: int,
        name: bytes,
        range_: RangeSpec | None,
        values: np.ndarray,
        dtype: Any,
        ctype: Any,
        *,
        writable: bool,
        extra: tuple[Any, ...] = (),
    ) -> None:
        length = 1 if range_ is None else range_.length
        checked = check_buffer(values, dtype, length, what="data buffer", writable=writable)
        pointer, owner = _range_argument(range_, checked.shape[0])
        self._binding.call(
            function,
            _int32(key, "database key"),
            _name(name),
            pointer,
            *extra,
            checked.ctypes.data_as(ct.POINTER(ctype)),
        )
        del owner, checked

    def get_precisions(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None:
        self._bulk(
            "fame_get_precisions", key, name, range_, out, np.float64, ct.c_double, writable=True
        )

    def get_numerics(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None:
        self._bulk(
            "fame_get_numerics", key, name, range_, out, np.float32, ct.c_float, writable=True
        )

    def get_booleans(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None:
        self._bulk("fame_get_booleans", key, name, range_, out, np.int32, ct.c_int32, writable=True)

    def get_dates(self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray) -> None:
        self._bulk("fame_get_dates", key, name, range_, out, np.int64, ct.c_int64, writable=True)

    def write_precisions(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None:
        self._bulk(
            "fame_write_precisions",
            key,
            name,
            range_,
            values,
            np.float64,
            ct.c_double,
            writable=False,
        )

    def write_numerics(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None:
        self._bulk(
            "fame_write_numerics", key, name, range_, values, np.float32, ct.c_float, writable=False
        )

    def write_booleans(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None:
        self._bulk(
            "fame_write_booleans", key, name, range_, values, np.int32, ct.c_int32, writable=False
        )

    def write_dates(
        self,
        key: int,
        name: bytes,
        range_: RangeSpec | None,
        type_code: int,
        values: np.ndarray,
    ) -> None:
        self._bulk(
            "fame_write_dates",
            key,
            name,
            range_,
            values,
            np.int64,
            ct.c_int64,
            writable=False,
            extra=(_int32(type_code, "date type"),),
        )

    # -- strings ---------------------------------------------------------

    def get_strings(
        self, key: int, name: bytes, range_: RangeSpec | None, count: int
    ) -> list[bytes]:
        count = _int32(count, "string count")
        if count < 1 or count > MAX_OBSERVATIONS:
            raise DataValidationError("String count is outside the supported bound.")
        pointer, owner = _range_argument(range_, count)
        lengths = np.zeros(count, dtype=np.int32)
        self._binding.call(
            "fame_len_strings",
            _int32(key, "database key"),
            _name(name),
            pointer,
            lengths.ctypes.data_as(ct.POINTER(ct.c_int32)),
        )
        if np.any(lengths < 0) or int(lengths.sum(dtype=np.int64)) > MAX_STRING_BYTES:
            raise DataValidationError("Reported string lengths are outside the accepted bound.")
        buffers = [ct.create_string_buffer(int(length) + 1) for length in lengths]
        pointers = (C * count)(*(ct.cast(buffer, C) for buffer in buffers))
        capacities = lengths.copy()
        self._binding.call(
            "fame_get_strings",
            _int32(key, "database key"),
            _name(name),
            pointer,
            pointers,
            capacities.ctypes.data_as(ct.POINTER(ct.c_int32)),
            None,
        )
        result = []
        for buffer, capacity, returned in zip(buffers, lengths, capacities, strict=True):
            used = min(int(returned), int(capacity))
            if used < 0:
                raise DataValidationError("A returned string length is negative.")
            result.append(bytes(buffer.raw[:used]))
        del owner, pointers
        return result

    def write_strings(
        self, key: int, name: bytes, range_: RangeSpec | None, values: Sequence[bytes]
    ) -> None:
        items = list(values)
        if not items:
            raise DataValidationError("At least one string is required.")
        for item in items:
            if not isinstance(item, bytes):
                raise DataValidationError("String data must be bytes.")
            if b"\0" in item:
                raise DataValidationError("String data cannot contain NUL bytes.")
        pointer, owner = _range_argument(range_, len(items))
        array = (S * len(items))(*items)
        self._binding.call(
            "fame_write_strings", _int32(key, "database key"), _name(name), pointer, array
        )
        del owner, array, items

    # -- namelists -------------------------------------------------------

    def get_namelist(self, key: int, name: bytes) -> bytes:
        length = ct.c_int32(-1)
        self._binding.call(
            "cfmnlen", _int32(key, "database key"), _name(name), NAMELIST_ALL, ct.byref(length)
        )
        size = int(length.value)
        if size < 0 or size > MAX_STRING_BYTES:
            raise DataValidationError("Reported namelist length is outside the accepted bound.")
        buffer = ct.create_string_buffer(size + 1)
        returned = ct.c_int32(-1)
        self._binding.call(
            "cfmgtnl",
            _int32(key, "database key"),
            _name(name),
            NAMELIST_ALL,
            ct.cast(buffer, C),
            size,
            ct.byref(returned),
        )
        used = min(max(int(returned.value), 0), size)
        return bytes(buffer.raw[:used])

    def write_namelist(self, key: int, name: bytes, value: bytes) -> None:
        if not isinstance(value, bytes) or b"\0" in value:
            raise DataValidationError("Namelist text must be NUL-free bytes.")
        self._binding.call("cfmwtnl", _int32(key, "database key"), _name(name), NAMELIST_ALL, value)

    # -- options, wildcards, commands ------------------------------------

    def set_option(self, name: bytes, value: bytes) -> None:
        self._binding.call("cfmsopt", _name(name), _name(value))

    def init_wildcard(self, key: int, pattern: bytes) -> int:
        wildcard_key = ct.c_int32(-1)
        self._binding.call(
            "fame_init_wildcard",
            _int32(key, "database key"),
            ct.byref(wildcard_key),
            _name(pattern),
            0,
            None,
        )
        return int(wildcard_key.value)

    def next_wildcard(self, wildcard_key: int, capacity: int) -> WildcardEntry:
        capacity = _int32(capacity, "name capacity")
        if capacity < 1 or capacity > 2**20:
            raise DataValidationError("Name capacity is outside the accepted bound.")
        buffer = ct.create_string_buffer(capacity + 1)
        class_code, type_code, frequency = ct.c_int32(-1), ct.c_int32(-1), ct.c_int32(-1)
        first, last = ct.c_int64(-1), ct.c_int64(-1)
        returned = ct.c_int32(-1)
        status = self._binding.call_status(
            "fame_get_next_wildcard",
            _int32(wildcard_key, "wildcard key"),
            ct.cast(buffer, C),
            ct.byref(class_code),
            ct.byref(type_code),
            ct.byref(frequency),
            ct.byref(first),
            ct.byref(last),
            capacity,
            ct.byref(returned),
        )
        used = min(max(int(returned.value), 0), capacity)
        raw = bytes(buffer.raw[:used]).split(b"\0", 1)[0]
        return WildcardEntry(
            status=status,
            name=raw,
            class_code=int(class_code.value),
            type_code=int(type_code.value),
            frequency=int(frequency.value),
            first_index=int(first.value),
            last_index=int(last.value),
            returned_length=int(returned.value),
        )

    def free_wildcard(self, wildcard_key: int) -> None:
        self._binding.call("fame_free_wildcard", _int32(wildcard_key, "wildcard key"))

    def execute(self, command: bytes) -> int:
        if not isinstance(command, bytes) or b"\0" in command:
            raise DataValidationError("Commands must be NUL-free bytes.")
        return self._binding.call_status("cfmfame", command)

    # -- classification and calendar ------------------------------------

    def missing_type(self, kind: str, value: Any) -> int:
        result = ct.c_int32(-1)
        if kind == "precision":
            self._binding.call("cfmispm", ct.c_double(float(value)), ct.byref(result))
        elif kind == "numeric":
            # Build the float argument from the float32 bits, never via a double.
            single = ct.c_float.from_buffer_copy(np.float32(value).tobytes())
            self._binding.call("cfmisnm", single, ct.byref(result))
        elif kind == "boolean":
            self._binding.call("cfmisbm", _int32(value, "boolean code"), ct.byref(result))
        elif kind == "string":
            if not isinstance(value, bytes) or b"\0" in value:
                raise DataValidationError("String values must be NUL-free bytes.")
            self._binding.call("cfmissm", value, ct.byref(result))
        elif kind == "date":
            self._binding.call(
                "fame_date_missing_type", _int64(value, "date index"), ct.byref(result)
            )
        else:
            raise ValueError("Unknown missing-value kind.")
        return int(result.value)

    def index_to_year_period(self, frequency: int, index: int) -> tuple[int, int]:
        year, period = ct.c_int32(-1), ct.c_int32(-1)
        self._binding.call(
            "fame_index_to_year_period",
            _int32(frequency, "frequency"),
            _int64(index, "index"),
            ct.byref(year),
            ct.byref(period),
        )
        return int(year.value), int(period.value)

    def year_period_to_index(self, frequency: int, year: int, period: int) -> int:
        index = ct.c_int64(-1)
        self._binding.call(
            "fame_year_period_to_index",
            _int32(frequency, "frequency"),
            ct.byref(index),
            _int32(year, "year"),
            _int32(period, "period"),
        )
        return int(index.value)


def wildcard_capacity() -> int:
    """The reported maximum object-name length used for listing buffers."""
    return NAME_CAPACITY


__all__ = [
    "MAX_OBSERVATIONS",
    "MAX_STRING_BYTES",
    "CtypesNative",
    "NativeInterface",
    "RangeSpec",
    "Sentinels",
    "WildcardEntry",
    "check_buffer",
    "check_status",
    "read_extended_error",
    "wildcard_capacity",
]
