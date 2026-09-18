# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Object model adapted from FAME.jl (Objects.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2022, Bank of Canada. All rights reserved.
"""Object metadata, periods and quick information."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._constants import (
    FREQUENCY_CASE,
    FREQUENCY_UNDEFINED,
    ObjectClass,
    ObjectType,
    frequency_name,
    is_date_type,
    type_name,
)
from ._database import Database
from ._errors import UnsupportedOperationError
from ._native import RangeSpec
from ._text import from_native, to_native


@dataclass(frozen=True)
class Period:
    """A FAME year:period pair; case frequencies use year 0."""

    year: int
    period: int

    def __str__(self) -> str:
        return f"{self.year}:{self.period}"


@dataclass(frozen=True)
class ObjectInfo:
    """Class, value type, frequency and index range of a database object."""

    name: bytes
    class_code: int
    type_code: int
    frequency: int
    first_index: int
    last_index: int

    @property
    def name_text(self) -> str:
        return from_native(self.name, what="object name")

    @property
    def object_class(self) -> ObjectClass:
        return ObjectClass(self.class_code)

    @property
    def is_series(self) -> bool:
        return self.class_code == ObjectClass.SERIES

    @property
    def is_scalar(self) -> bool:
        return self.class_code == ObjectClass.SCALAR

    @property
    def kind(self) -> str:
        """``precision``, ``numeric``, ``boolean``, ``string``, ``namelist`` or ``date``."""
        if is_date_type(self.type_code):
            return "date"
        return ObjectType(self.type_code).name.lower()

    @property
    def date_frequency(self) -> int | None:
        """Frequency code of the values when the object holds dates."""
        return self.type_code if is_date_type(self.type_code) else None

    @property
    def type_label(self) -> str:
        return type_name(self.type_code)

    @property
    def frequency_label(self) -> str:
        return frequency_name(self.frequency)

    def is_empty(self, index_nc: int) -> bool:
        """A series whose endpoints are the NC index has no observations."""
        return self.is_series and (index_nc in (self.first_index, self.last_index))

    def length(self, index_nc: int) -> int:
        if not self.is_series or self.is_empty(index_nc):
            return 0
        return self.last_index - self.first_index + 1

    def range(self, index_nc: int) -> RangeSpec | None:
        if not self.is_series or self.is_empty(index_nc):
            return None
        return RangeSpec(self.frequency, self.first_index, self.last_index)

    def __str__(self) -> str:
        return (
            f"{self.name_text}: {self.object_class.name.lower()},{self.type_label},"
            f"{self.frequency_label},{self.first_index}:{self.last_index}"
        )


def check_supported_class(info: ObjectInfo) -> None:
    """Only scalars and series carry structured data; other classes use commands."""
    if info.class_code not in (ObjectClass.SERIES, ObjectClass.SCALAR):
        raise UnsupportedOperationError(
            "Only scalar and series objects support structured reads and writes; "
            "use FAME commands for formulas and global objects."
        )


def query_info(native: Any, key: int, name: bytes) -> ObjectInfo:
    """Query metadata through an already locked native handle."""
    class_code, type_code, frequency, first, last = native.quick_info(key, name)
    return ObjectInfo(name, class_code, type_code, frequency, first, last)


def quick_info(database: Database, name: str | bytes) -> ObjectInfo:
    """Return metadata for one object without reading its data."""
    text = to_native(name, what="object name")
    with database.operation("query an object") as native:
        return query_info(native, database.key, text)


def index_to_period(frequency: int, index: int, *, database: Database) -> Period:
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


def period_to_index(frequency: int, period: Period, *, database: Database) -> int:
    if frequency == FREQUENCY_CASE:
        return period.period
    with database.session.operation("year_period_to_index") as native:
        return native.year_period_to_index(frequency, period.year, period.period)
