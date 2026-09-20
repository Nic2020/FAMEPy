# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Code tables adapted from FAME.jl (Types.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2021, Bank of Canada. All rights reserved.
"""Symbolic CHLI code tables: classes, types, frequencies, modes and attributes.

The numeric values reproduce the reference tables. Both protected discovery
reports confirmed the constants that the operational core uses (status codes,
name capacity). Other values remain reference-derived until a header check.
"""

from __future__ import annotations

import enum
import operator
from collections.abc import Mapping


class AccessMode(enum.IntEnum):
    """The seven reference database access modes.

    Only the first five are local modes of the database open the package
    binds. ``WRITE`` and ``DIRECT_WRITE`` are modes of a database opened on
    a named server connection, an API neither the package nor the reference
    binds; they are kept for parity and refused by ``opendb``.
    """

    READONLY = 1
    CREATE = 2
    OVERWRITE = 3
    UPDATE = 4
    SHARED = 5
    WRITE = 6
    DIRECT_WRITE = 7


# Modes accepted by the local database open the package binds.
LOCAL_ACCESS_MODES: tuple[AccessMode, ...] = (
    AccessMode.READONLY,
    AccessMode.CREATE,
    AccessMode.OVERWRITE,
    AccessMode.UPDATE,
    AccessMode.SHARED,
)


class ObjectClass(enum.IntEnum):
    SERIES = 1
    SCALAR = 2
    FORMULA = 3
    GLNAME = 5
    GLFORMULA = 6


class ObjectType(enum.IntEnum):
    """Value types. A date-valued object reports its frequency code instead."""

    UNDEFINED = 0
    NUMERIC = 1
    NAMELIST = 2
    BOOLEAN = 3
    STRING = 4
    PRECISION = 5
    DATE = 6


class Basis(enum.IntEnum):
    UNDEFINED = 0
    DAILY = 1
    BUSINESS = 2


class Observed(enum.IntEnum):
    UNDEFINED = 0
    BEGINNING = 1
    ENDING = 2
    AVERAGED = 3
    SUMMED = 4
    ANNUALIZED = 5
    FORMULA = 6
    HIGH = 7
    LOW = 8


class Relation(enum.IntEnum):
    BEFORE = 1
    AFTER = 2
    CONTAINS = 3


FREQUENCIES: Mapping[str, int] = {
    "undefined": 0,
    "daily": 8,
    "business": 9,
    "weekly_sunday": 16,
    "weekly_monday": 17,
    "weekly_tuesday": 18,
    "weekly_wednesday": 19,
    "weekly_thursday": 20,
    "weekly_friday": 21,
    "weekly_saturday": 22,
    "tenday": 32,
    "biweekly_asunday": 64,
    "biweekly_amonday": 65,
    "biweekly_atuesday": 66,
    "biweekly_awednesday": 67,
    "biweekly_athursday": 68,
    "biweekly_afriday": 69,
    "biweekly_asaturday": 70,
    "biweekly_bsunday": 71,
    "biweekly_bmonday": 72,
    "biweekly_btuesday": 73,
    "biweekly_bwednesday": 74,
    "biweekly_bthursday": 75,
    "biweekly_bfriday": 76,
    "biweekly_bsaturday": 77,
    "twicemonthly": 128,
    "monthly": 129,
    "bimonthly_november": 144,
    "bimonthly_december": 145,
    "quarterly_october": 160,
    "quarterly_november": 161,
    "quarterly_december": 162,
    "annual_january": 192,
    "annual_february": 193,
    "annual_march": 194,
    "annual_april": 195,
    "annual_may": 196,
    "annual_june": 197,
    "annual_july": 198,
    "annual_august": 199,
    "annual_september": 200,
    "annual_october": 201,
    "annual_november": 202,
    "annual_december": 203,
    "semiannual_july": 204,
    "semiannual_august": 205,
    "semiannual_september": 206,
    "semiannual_october": 207,
    "semiannual_november": 208,
    "semiannual_december": 209,
    "ypp": 224,
    "ppy": 225,
    "secondly": 226,
    "minutely": 227,
    "hourly": 228,
    "millisecondly": 229,
    "case": 232,
    "weekly_pattern": 233,
}
FREQUENCY_NAMES: Mapping[int, str] = {code: name for name, code in FREQUENCIES.items()}

FREQUENCY_UNDEFINED = 0
FREQUENCY_MONTHLY = 129
FREQUENCY_CASE = 232

# Listing selectors: each date-indexed frequency belongs to one family that
# the ``ITEM FREQUENCY <family>`` option selects. Case series are selected by
# the index option (``ITEM INDEX CASE``) and scalars have no frequency, so
# neither has a family here.
FREQUENCY_FAMILIES: Mapping[int, str] = {
    code: ("USERDEFINED" if name == "weekly_pattern" else name.split("_", 1)[0].upper())
    for name, code in FREQUENCIES.items()
    if name not in ("undefined", "case")
}

# Namelist "all items" selector used by the reference for cfmnlen/cfmgtnl/cfmwtnl.
NAMELIST_ALL = -1

# Reported v4 object-name capacity (bytes, excluding the terminator) on both
# inspected installations. The reference's fixed 101-byte buffer is not used.
NAME_CAPACITY = 242

# Missing-value classification codes returned by the cfmis*m functions.
MISSING_NORMAL = 0
MISSING_NC = 1
MISSING_NA = 2
MISSING_ND = 3
MISSING_MAGIC = 4


def _lookup(value: object, table: Mapping[str, int], kind: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"A {kind} cannot be a Boolean.")
    if isinstance(value, str):
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        if key not in table:
            raise ValueError(f"Unknown {kind} name.")
        return table[key]
    if not isinstance(value, int):
        raise TypeError(f"A {kind} must be an integer, name or enumeration member.")
    code = int(value)
    if code not in table.values():
        raise ValueError(f"Unknown {kind} code {code}.")
    return code


def access_mode(value: object) -> AccessMode:
    """Accept an integer 1-7, a case-insensitive name or an AccessMode member."""
    table = {member.name.lower(): int(member) for member in AccessMode}
    return AccessMode(_lookup(value, table, "access mode"))


def object_class(value: object) -> ObjectClass:
    table = {member.name.lower(): int(member) for member in ObjectClass}
    return ObjectClass(_lookup(value, table, "object class"))


def frequency_code(value: object) -> int:
    """Return a FAME frequency code from a code, name or MIT-like value."""
    return _lookup(value, FREQUENCIES, "frequency")


def frequency_name(code: int) -> str:
    try:
        return FREQUENCY_NAMES[code]
    except KeyError:
        raise ValueError(f"Unknown frequency code {code}.") from None


def type_code(value: object) -> int:
    """Return a value-type code. Date-valued objects use a calendar frequency code.

    The case frequency indexes series but is not a value type: the library
    refuses it as an object type, so it is refused here before any call.
    """
    if isinstance(value, ObjectType):
        return int(value)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in {member.name.lower() for member in ObjectType}:
            return int(ObjectType[key.upper()])
        code = frequency_code(value)
    else:
        code = _lookup(
            value, {**{m.name.lower(): int(m) for m in ObjectType}, **FREQUENCIES}, "type"
        )
    if code == FREQUENCY_CASE:
        raise ValueError("The case frequency is an index frequency, not a date value type.")
    return code


def is_date_type(code: int) -> bool:
    """True when a type code denotes a calendar frequency (date-valued data).

    The case frequency is an index frequency only and never a value type.
    """
    code = operator.index(code)
    return code >= 8 and code != FREQUENCY_CASE and code in FREQUENCY_NAMES


def type_name(code: int) -> str:
    code = operator.index(code)
    if is_date_type(code):
        return f"date:{FREQUENCY_NAMES[code]}"
    try:
        return ObjectType(code).name.lower()
    except ValueError:
        raise ValueError(f"Unknown type code {code}.") from None
