# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Object model adapted from FAME.jl (Objects.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2022, Bank of Canada. All rights reserved.
"""The named object model: ``FameObject``, ``Period`` and ``quick_info``.

A ``FameObject`` carries an object's name, class, type, frequency and index
range, plus its data once ``do_read`` filled it or ``refame`` converted a
value into it. ``quick_info`` and ``listdb`` return objects without data;
``do_write`` writes an object's data; ``unfame`` converts it back to a
Python value. Class, type and frequency are stored as the library's codes
and can be given as names (``"series"``, ``"precision"``, ``"monthly"``),
codes or enumeration members; a date-valued object's type is the frequency
of its values, as the reference spells it.

The data field holds the value itself: an exact-width NumPy scalar or
``bytes`` for a scalar, a one-dimensional exact-dtype array or a list of
``bytes`` for a series. Validation of the data against the class, type and
range happens at use (``do_write``, ``unfame``), before any native call, so
an object can be built and adjusted freely and never reaches the library
in an inconsistent state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._constants import (
    FREQUENCY_CASE,
    FREQUENCY_UNDEFINED,
    ObjectClass,
    ObjectType,
    frequency_code,
    frequency_name,
    is_date_type,
    type_name,
)
from ._constants import object_class as _object_class
from ._constants import type_code as _type_code
from ._database import FameDatabase
from ._errors import DataValidationError, UnsupportedOperationError
from ._native import FameRange
from ._text import from_native, object_name


@dataclass(frozen=True)
class Period:
    """A FAME year:period pair; case frequencies use year 0."""

    year: int
    period: int

    def __str__(self) -> str:
        return f"{self.year}:{self.period}"


def _index_or_none(value: Any, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not hasattr(value, "__index__"):
        raise DataValidationError(f"{what} must be an integer index or None.")
    number = int(value)
    if not -(2**63) <= number < 2**63:
        raise DataValidationError(f"{what} must fit a signed 64-bit index.")
    return number


class FameObject:
    """A named FAME object: class, type, frequency, index range and data.

    ``FameObject(name, class_, type, freq)`` builds an object without data
    and without a range; ``FameObject(name, class_, type, freq, first_index,
    last_index, data)`` a complete one. ``class_`` is ``"series"`` or
    ``"scalar"`` (other classes can be listed but not read or written),
    ``type`` is a value kind (``"precision"``, ``"numeric"``, ``"boolean"``,
    ``"string"``, ``"namelist"``) or, for date values, the frequency of the
    dates, and ``freq`` is the index frequency (``"undefined"`` for scalars).
    Every field is a plain, assignable attribute; codes are validated when
    assigned and the data when the object is written or converted.
    """

    __slots__ = ("_class", "_first", "_frequency", "_last", "_name", "_type", "data")

    def __init__(
        self,
        name: str | bytes,
        class_: Any,
        type: Any,
        freq: Any,
        first_index: int | None = None,
        last_index: int | None = None,
        data: Any = None,
    ) -> None:
        self.name = name
        self.class_code = class_
        self.type_code = type
        self.frequency = freq
        self.first_index = first_index
        self.last_index = last_index
        self.data = data

    # -- validated fields ---------------------------------------------------

    @property
    def name(self) -> bytes:
        """The object name as bytes (ASCII ``str`` input is encoded)."""
        return self._name

    @name.setter
    def name(self, value: str | bytes) -> None:
        self._name = object_name(value)

    @property
    def class_code(self) -> int:
        return self._class

    @class_code.setter
    def class_code(self, value: Any) -> None:
        self._class = int(_object_class(value))

    @property
    def type_code(self) -> int:
        """The value type code; a date-valued object reports its value frequency."""
        return self._type

    @type_code.setter
    def type_code(self, value: Any) -> None:
        try:
            self._type = _type_code(value)
        except ValueError as error:
            # The case frequency is never a value type; keep the raw layer's error class.
            raise DataValidationError(str(error)) from None

    @property
    def frequency(self) -> int:
        """The index frequency code (``undefined`` for scalars)."""
        return self._frequency

    @frequency.setter
    def frequency(self, value: Any) -> None:
        self._frequency = frequency_code(value)

    @property
    def first_index(self) -> int | None:
        return self._first

    @first_index.setter
    def first_index(self, value: int | None) -> None:
        self._first = _index_or_none(value, "first_index")

    @property
    def last_index(self) -> int | None:
        return self._last

    @last_index.setter
    def last_index(self, value: int | None) -> None:
        self._last = _index_or_none(value, "last_index")

    @classmethod
    def _from_codes(
        cls, name: bytes, class_code: int, type_code: int, frequency: int, first: int, last: int
    ) -> FameObject:
        """An object from codes the library returned, kept as reported.

        Codes outside the package tables are stored unchanged so that a
        listing never fails on them; the derived views raise when asked.
        """
        obj = cls.__new__(cls)
        obj._name = name
        obj._class = int(class_code)
        obj._type = int(type_code)
        obj._frequency = int(frequency)
        obj._first = int(first)
        obj._last = int(last)
        obj.data = None
        return obj

    # -- derived views -------------------------------------------------------

    @property
    def name_text(self) -> str:
        return from_native(self.name, what="object name")

    @property
    def object_class(self) -> ObjectClass:
        return ObjectClass(self._class)

    @property
    def is_series(self) -> bool:
        return self._class == ObjectClass.SERIES

    @property
    def is_scalar(self) -> bool:
        return self._class == ObjectClass.SCALAR

    @property
    def kind(self) -> str:
        """``precision``, ``numeric``, ``boolean``, ``string``, ``namelist`` or ``date``."""
        if is_date_type(self._type):
            return "date"
        return ObjectType(self._type).name.lower()

    @property
    def date_frequency(self) -> int | None:
        """Frequency code of the values when the object holds dates."""
        return self._type if is_date_type(self._type) else None

    @property
    def type_label(self) -> str:
        return type_name(self._type)

    @property
    def frequency_label(self) -> str:
        return frequency_name(self._frequency)

    @property
    def has_data(self) -> bool:
        return self.data is not None

    def is_empty(self, index_nc: int) -> bool:
        """A series whose endpoints are the NC index has no observations."""
        return self.is_series and (index_nc in (self._first, self._last))

    def length(self, index_nc: int) -> int:
        if not self.is_series or self.is_empty(index_nc) or self._first is None:
            return 0
        if self._last is None:
            return 0
        return self._last - self._first + 1

    def range(self, index_nc: int) -> FameRange | None:
        if not self.is_series or self.is_empty(index_nc):
            return None
        if self._first is None or self._last is None:
            return None
        return FameRange(self._frequency, self._first, self._last)

    def _range_text(self) -> str:
        first = "" if self._first is None else str(self._first)
        last = "" if self._last is None else str(self._last)
        return f"{first}:{last}"

    def _label(self, table: Any, code: int) -> str:
        try:
            return str(table(code)).lower()
        except ValueError:
            return str(code)

    def __str__(self) -> str:
        try:
            class_label = self.object_class.name.lower()
        except ValueError:
            class_label = str(self._class)
        return (
            f"{self.name_text}: {class_label},{self._label(type_name, self._type)},"
            f"{self._label(frequency_name, self._frequency)},{self._range_text()}"
        )

    def __repr__(self) -> str:
        if self.data is None:
            payload = "no data"
        elif self.is_scalar:
            payload = "scalar data"
        else:
            try:
                payload = f"{len(self.data)} values"
            except TypeError:
                payload = "data"
        return f"FameObject({self}, {payload})"


def check_supported_class(obj: FameObject) -> None:
    """Only scalars and series carry structured data; other classes use commands."""
    if obj.class_code not in (ObjectClass.SERIES, ObjectClass.SCALAR):
        raise UnsupportedOperationError(
            "Only scalar and series objects support structured reads and writes; "
            "use FAME commands for formulas and global objects."
        )


def query_info(native: Any, key: int, name: bytes) -> FameObject:
    """Query metadata through an already locked native handle."""
    class_code, type_code, frequency, first, last = native.quick_info(key, name)
    return FameObject._from_codes(name, class_code, type_code, frequency, first, last)


def quick_info(db: FameDatabase, name: str | bytes) -> FameObject:
    """Return the object's class, type, frequency and range, without its data.

    The returned ``FameObject`` is what ``do_read`` fills in place.
    """
    if not isinstance(db, FameDatabase):
        raise TypeError("quick_info expects a FameDatabase.")
    text = object_name(name)
    with db.operation("query an object") as native:
        return query_info(native, db.key, text)


def index_to_period(frequency: int, index: int, *, database: FameDatabase) -> Period:
    """Convert an index to year:period through the library.

    Case frequencies map directly; undefined frequencies yield 0:0. Missing
    indices are reported by the caller through missing classification, not here.
    """
    if frequency == FREQUENCY_CASE:
        return Period(0, index)
    if frequency == FREQUENCY_UNDEFINED:
        return Period(0, 0)
    with database.session.operation("index_to_year_period") as native:
        year, period = native.index_to_year_period(frequency, index)
    return Period(year, period)


def period_to_index(frequency: int, period: Period, *, database: FameDatabase) -> int:
    if frequency == FREQUENCY_CASE:
        return period.period
    with database.session.operation("year_period_to_index") as native:
        return native.year_period_to_index(frequency, period.year, period.period)
