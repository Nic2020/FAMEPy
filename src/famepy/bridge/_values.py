# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Conversion semantics adapted from FAME.jl (Bridge.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""Value conversions: ``refame`` and ``unfame`` between Python values and FAME objects.

Representation table (Python value -> FAME object -> Python value):

| Python value                       | FAME object                   | read back as         |
|------------------------------------|-------------------------------|----------------------|
| ``float`` / ``numpy.float64``      | precision scalar              | ``float``            |
| ``numpy.float32``                  | numeric scalar                | ``numpy.float32``    |
| ``int`` (exactly representable)    | precision scalar              | ``float``            |
| ``bool`` / ``numpy.bool_``         | Boolean scalar                | ``bool``             |
| ``MIT`` (calendar frequency)       | date scalar (value frequency) | ``MIT``              |
| ``str`` not shaped ``{...}``       | string scalar                 | ``str``              |
| ``str`` shaped ``{...}``           | namelist (reference detection)| ``NameList``         |
| ``Text``                           | string scalar, always         | ``str``              |
| ``NameList``                       | namelist                      | ``NameList``         |
| ``list``/``tuple`` of ``str``      | case string series from 1     | ``StringSeries``     |
| ``TSeries`` float64 / int (exact)  | precision series              | ``TSeries`` float64  |
| ``TSeries`` float32                | numeric series                | ``TSeries`` float32  |
| ``TSeries`` bool                   | Boolean series                | ``TSeries`` bool     |
| ``DateSeries``                     | date series                   | ``DateSeries``       |
| ``StringSeries``                   | string series                 | ``StringSeries``     |

Deliberate differences from the reference: a Python ``int`` becomes a
precision scalar only when it is exactly representable in float64 (the
reference rounds integers to a float32 numeric scalar); a NaN scalar is
written as NC like NaN observations (the reference's scalar NaN test never
matches, so it stores a plain NaN); a namelist reads back as a ``NameList``
of ordered members instead of the library's list text; string series keep
their first date in a ``StringSeries`` instead of becoming a bare vector; a
missing Boolean observation is never read as ``True``; a missing date is
never a plausible integer; a case moment (``MIT`` of ``Unit``) is refused as
a date *value* (scalar, ``DateSeries`` observation or the value frequency of
an empty or all-missing ``DateSeries``) because the library accepts the case
frequency as a series index but not as an object type, and nothing is
remapped to a calendar or a number in its place (the reference maps it and
lets the library refuse the object). Every difference is covered by the tests.

Missing policies: ``missing="nan"`` (default) reads NC, NA and ND as NaN
for floating kinds and as ``None`` for date and string values, which is
lossy between the three categories; a missing Boolean observation has no
in-band representation and raises ``MissingValueError`` under either
policy; ``missing="strict"`` raises for every missing observation. Writes
encode NaN and ``None`` as NC, as the reference does for NaN.

Empty policies: ``empty="preserve"`` (default) writes an empty series as a
truly empty FAME series and refuses to read a truly empty series without an
explicit ``empty_firstdate``; ``empty="reference"`` writes one NA observation
at the first date and collapses a single missing observation to an empty
series on read (floating, Boolean and date series, as the reference does;
string series are never collapsed). That encoding cannot tell an empty
series from a one-observation missing series, which is why it is opt-in.

Text policies apply to string *values* only (string scalars, ``Text``,
``StringSeries`` observations, plain string vectors); object names,
namelist members, database strings and commands keep their own ASCII
boundary. ``text="ascii"`` (default) encodes ``str`` as ASCII and decodes
strictly as ASCII; ``text="bytes"`` encodes ``str`` as ASCII and returns
read values as the stored bytes; ``text="utf-8"`` encodes ``str`` strictly
as UTF-8 and decodes strictly as UTF-8. ``bytes`` inputs are written as
given under every policy, an embedded NUL, a lone surrogate or a stored
byte sequence that is not the selected encoding raises
``TextEncodingError``, and a missing observation is classified by its
native sentinel bytes before any decoding. The whole stored byte sequence
is decoded: the reference wrapper slices its read buffer by the native
byte length on a character index, which fails when the last character is
multibyte; that behavior is a wrapper limitation and is deliberately not
reproduced. No policy claims that the library itself interprets text.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from tsecon import MIT, TSeries, Unit
from tsecon.frequencies import Frequency

from .._constants import MISSING_NA, MISSING_NC, MISSING_NORMAL
from .._data import (
    RawObject,
    RawScalar,
    RawSeries,
    classify_by_sentinel,
    namelist_members,
    object_from_raw,
    raw_of,
    sentinel_value,
)
from .._database import FameDatabase
from .._errors import DataValidationError
from .._native import MAX_OBSERVATIONS
from .._objects import FameObject
from .._runtime import Session
from .._text import VALUE_TEXT_POLICIES, decode_value, encode_value, object_name
from ._frequencies import (
    fame_frequency,
    index_to_mit,
    is_supported_frequency,
    mit_to_index,
    owner_session,
    tsecon_frequency,
)

__all__ = [
    "EMPTY_POLICIES",
    "MISSING_POLICIES",
    "TEXT_POLICIES",
    "DateSeries",
    "EmptySeriesError",
    "MissingValueError",
    "NameList",
    "StringSeries",
    "Text",
    "check_policies",
    "from_fame",
    "refame",
    "to_fame",
    "unfame",
    "validate_value",
]

MISSING_POLICIES = ("nan", "strict")
EMPTY_POLICIES = ("preserve", "reference")
TEXT_POLICIES = VALUE_TEXT_POLICIES


class MissingValueError(ValueError):
    """A missing observation has no representation under the selected policy."""


class EmptySeriesError(ValueError):
    """A truly empty FAME series has no stored first date to rebuild a series."""


def check_policies(
    missing: str | None = None, empty: str | None = None, text: str | None = None
) -> None:
    if missing is not None and missing not in MISSING_POLICIES:
        raise ValueError("missing must be 'nan' or 'strict'.")
    if empty is not None and empty not in EMPTY_POLICIES:
        raise ValueError("empty must be 'preserve' or 'reference'.")
    if text is not None and text not in TEXT_POLICIES:
        raise ValueError("text must be 'ascii', 'bytes' or 'utf-8'.")


# -- carriers ------------------------------------------------------------------

_MEMBER_EXCLUDED = frozenset("{}, \t\r\n")


def _check_member(member: Any) -> str:
    if not isinstance(member, str):
        raise TypeError("Namelist members must be str.")
    if not member or not member.isascii() or not member.isprintable():
        raise ValueError("A namelist member must be printable ASCII text.")
    if any(ch in _MEMBER_EXCLUDED for ch in member):
        raise ValueError("A namelist member cannot contain braces, commas or blanks.")
    return member


@dataclass(frozen=True)
class NameList:
    """An ordered FAME name-list. ``str(namelist)`` is the ``{A,B}`` text.

    Members are kept as given; the library upper-cases names and may lay the
    list text out differently, so a round trip compares members, not text.
    ``NameList("{a,b}")`` parses list text; ``NameList(["a", "b"])`` takes
    members.
    """

    members: tuple[str, ...]

    def __init__(self, members: Iterable[str] | str = ()) -> None:
        if isinstance(members, str):
            text = members.strip()
            if len(text) < 2 or text[0] != "{" or text[-1] != "}":
                raise ValueError("Namelist text must be enclosed in braces.")
            body = text[1:-1].strip()
            items = [part.strip() for part in body.split(",")] if body else []
        else:
            items = list(members)
        object.__setattr__(self, "members", tuple(_check_member(item) for item in items))

    @property
    def text(self) -> str:
        return "{" + ",".join(self.members) + "}"

    def __str__(self) -> str:
        return self.text

    def __len__(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class Text:
    """A string written as a string scalar even when it looks like a namelist."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("Text wraps a str.")


def _check_firstdate(firstdate: Any) -> MIT:
    if not isinstance(firstdate, MIT):
        raise TypeError("firstdate must be an MIT.")
    fame_frequency(firstdate.frequency)
    return firstdate


def _check_value_frequency(frequency: Frequency) -> int:
    """The library code of a date *value* frequency: a supported calendar one.

    The case frequency indexes series but cannot type a date value; refusing
    it here keeps the refusal deterministic and ahead of any native call.
    """
    code = fame_frequency(frequency)
    if isinstance(frequency, Unit):
        raise DataValidationError(
            "A date value cannot carry the case frequency; it indexes series only."
        )
    return code


@dataclass(frozen=True)
class DateSeries:
    """A series of date-valued observations: ``MIT`` values or ``None`` (missing).

    ``firstdate`` gives the index frequency (any supported frequency,
    including case); ``value_frequency`` the frequency of the observations,
    inferred from the values when any is present and required otherwise.
    Every value must carry that frequency, which must be a calendar
    frequency: the case frequency is refused as a value frequency.
    """

    firstdate: MIT
    values: tuple[MIT | None, ...]
    value_frequency: Frequency

    def __init__(
        self,
        firstdate: MIT,
        values: Iterable[MIT | None] = (),
        value_frequency: Frequency | None = None,
    ) -> None:
        items = tuple(values)
        found = value_frequency
        for item in items:
            if item is None:
                continue
            if not isinstance(item, MIT):
                raise TypeError("DateSeries values are MIT or None.")
            if found is None:
                found = item.frequency
            elif item.frequency != found:
                raise DataValidationError("All dates in a DateSeries share one frequency.")
        if found is None:
            raise DataValidationError("An empty DateSeries needs value_frequency.")
        if not isinstance(found, Frequency):
            raise TypeError("value_frequency must be a tsecon Frequency instance.")
        _check_value_frequency(found)
        object.__setattr__(self, "firstdate", _check_firstdate(firstdate))
        object.__setattr__(self, "values", items)
        object.__setattr__(self, "value_frequency", found)

    @property
    def frequency(self) -> Frequency:
        return self.firstdate.frequency

    def __len__(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class StringSeries:
    """A series of string observations: ``str``, ``bytes`` or ``None`` (missing).

    The reference reads string series as a bare vector; this carrier keeps
    the first date (an extension). ``list(series.values)`` gives the vector.
    """

    firstdate: MIT
    values: tuple[str | bytes | None, ...]

    def __init__(self, firstdate: MIT, values: Iterable[str | bytes | None] = ()) -> None:
        items = tuple(values)
        for item in items:
            if item is not None and not isinstance(item, (str, bytes)):
                raise TypeError("StringSeries values are str, bytes or None.")
        object.__setattr__(self, "firstdate", _check_firstdate(firstdate))
        object.__setattr__(self, "values", items)

    @property
    def frequency(self) -> Frequency:
        return self.firstdate.frequency

    def __len__(self) -> int:
        return len(self.values)


# -- database-independent validation --------------------------------------------


def _exact_float64(values: np.ndarray) -> np.ndarray:
    """Convert an integer array to float64, refusing any inexact element."""
    converted = values.astype(np.float64)
    limit = 2.0**64 if values.dtype.kind == "u" else 2.0**63
    with np.errstate(all="ignore"):
        lower = 0.0 if values.dtype.kind == "u" else -limit
        representable = (converted >= lower) & (converted < limit)
        back = np.zeros(values.shape, dtype=values.dtype)
        back[representable] = converted[representable].astype(values.dtype)
    exact = representable & (back == values)
    if not bool(np.all(exact)):
        raise DataValidationError("Some integers are not exactly representable in float64.")
    return converted


def _series_kind_and_values(values: np.ndarray, what: str) -> tuple[str, np.ndarray]:
    """Kind and an owned exact-dtype copy of a TSeries buffer."""
    if values.ndim != 1:
        raise DataValidationError(f"{what} must be one-dimensional.")
    if values.dtype == np.float64:
        return "precision", np.array(values, dtype=np.float64, copy=True)
    if values.dtype == np.float32:
        return "numeric", np.array(values, dtype=np.float32, copy=True)
    if values.dtype == np.bool_:
        return "boolean", values.astype(np.int32)
    if np.issubdtype(values.dtype, np.integer):
        return "precision", _exact_float64(values)
    raise DataValidationError(
        f"{what} must be float64, float32, bool or exactly representable integers; "
        f"got {values.dtype}."
    )


def _scalar_number(value: Any) -> float:
    """A precision scalar from a real number; integers must be exact."""
    if isinstance(value, (int, np.integer)):
        integer = int(value)
        try:
            number = float(integer)
        except OverflowError:
            raise DataValidationError(
                "The integer cannot be represented exactly as float64."
            ) from None
        if int(number) != integer:
            raise DataValidationError("The integer cannot be represented exactly as float64.")
        return number
    return float(value)


def _check_length(count: int) -> None:
    if count > MAX_OBSERVATIONS:
        raise DataValidationError("Series length exceeds the supported bound.")


@dataclass(frozen=True)
class _Prepared:
    """Everything about a value that is known before any native call."""

    kind: str
    scalar: bool
    firstdate: MIT | None
    values: Any
    value_frequency: Frequency | None
    empty_marker_kind: str | None = None


def _looks_like_namelist(value: str) -> bool:
    return len(value) > 1 and value[0] == "{" and value[-1] == "}"


def _prepare(value: Any, empty: str, text: str = "ascii") -> _Prepared:
    """Classify and validate a value without a session or database."""
    check_policies(empty=empty, text=text)
    if isinstance(value, (bool, np.bool_)):
        return _Prepared("boolean", True, None, int(bool(value)), None)
    if isinstance(value, np.float32):
        return _Prepared("numeric", True, None, value, None)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return _Prepared("precision", True, None, _scalar_number(value), None)
    if isinstance(value, MIT):
        _check_value_frequency(value.frequency)
        return _Prepared("date", True, None, value, value.frequency)
    if isinstance(value, Text):
        return _Prepared("string", True, None, encode_value(value.value, text), None)
    if isinstance(value, NameList):
        return _Prepared("namelist", True, None, value, None)
    if isinstance(value, str):
        if _looks_like_namelist(value):
            return _Prepared("namelist", True, None, NameList(value), None)
        return _Prepared("string", True, None, encode_value(value, text), None)
    if isinstance(value, bytes):
        return _Prepared("string", True, None, encode_value(value, text), None)
    if isinstance(value, TSeries):
        fame_frequency(value.frequency)
        kind, values = _series_kind_and_values(value.values, "TSeries values")
        _check_length(len(values))
        return _Prepared(kind, False, value.firstdate, values, None, kind)
    if isinstance(value, DateSeries):
        _check_length(len(value))
        return _Prepared(
            "date", False, value.firstdate, value.values, value.value_frequency, "date"
        )
    if isinstance(value, StringSeries):
        _check_length(len(value))
        items = [
            None if item is None else encode_value(item, text, what="string series value")
            for item in value.values
        ]
        return _Prepared("string", False, value.firstdate, items, None)
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, (str, bytes)) for item in value):
            raise TypeError("A list or tuple is written as a case string series of str values.")
        _check_length(len(value))
        items = [encode_value(item, text, what="string series value") for item in value]
        return _Prepared("string", False, MIT(Unit(), 1), items, None)
    raise TypeError(f"Cannot write a {type(value).__name__} to a FAME database.")


# -- refame ---------------------------------------------------------------------


def refame(
    name: str | bytes,
    value: Any,
    *,
    database: FameDatabase | None = None,
    session: Session | None = None,
    empty: str = "preserve",
    text: str = "ascii",
) -> FameObject:
    """Convert ``value`` into a ``FameObject`` named ``name``, ready for ``do_write``.

    See the module note for the representation table. The name is validated
    first; type, dtype, frequency, exact integer conversion and text checks
    happen before any native call, and the only native calls are read-only
    calendar conversions (through ``database``'s session, ``session``, or
    the current one). The caller's arrays, strings and bytes are never
    modified. ``empty`` and ``text`` are the empty-series and string value
    policies.
    """
    validated = object_name(name)
    return object_from_raw(
        validated, to_fame(value, database=database, session=session, empty=empty, text=text)
    )


def to_fame(
    value: Any,
    *,
    database: FameDatabase | None = None,
    session: Session | None = None,
    empty: str = "preserve",
    text: str = "ascii",
) -> RawObject:
    """Convert a Python value into an internal carrier (``refame`` without the name).

    Type, dtype, frequency, exact integer conversion and text checks happen
    before any native call; the only native calls are read-only calendar
    conversions. The caller's arrays, strings and bytes are never modified.
    """
    prepared = _prepare(value, empty, text)
    owner = owner_session(database, session)
    sentinels = owner.sentinels
    kind = prepared.kind
    if prepared.scalar:
        if kind == "boolean":
            return RawScalar("boolean", prepared.values)
        if kind == "numeric":
            single = prepared.values
            if np.isnan(single):
                single = sentinel_value("numeric", MISSING_NC, sentinels)
            return RawScalar("numeric", single)
        if kind == "precision":
            number = prepared.values
            if math.isnan(number):
                number = sentinel_value("precision", MISSING_NC, sentinels)
            return RawScalar("precision", number)
        if kind == "date":
            moment = prepared.values
            index = mit_to_index(moment, session=owner)
            return RawScalar("date", index, fame_frequency(moment.frequency))
        if kind == "namelist":
            # The reference upper-cases namelist text before writing.
            return RawScalar("namelist", prepared.values.text.upper().encode("ascii"))
        return RawScalar("string", prepared.values)
    assert prepared.firstdate is not None
    code = fame_frequency(prepared.firstdate.frequency)
    first = mit_to_index(prepared.firstdate, session=owner)
    if isinstance(prepared.firstdate.frequency, Unit):
        _check_case_range(first, len(prepared.values))
    count = len(prepared.values)
    if count == 0:
        if empty == "reference" and prepared.empty_marker_kind is not None:
            marker_kind = prepared.empty_marker_kind
            marker = sentinel_value(marker_kind, MISSING_NA, sentinels)
            if marker_kind == "date":
                assert prepared.value_frequency is not None
                return RawSeries(
                    "date",
                    code,
                    first,
                    np.array([marker], dtype=np.int64),
                    fame_frequency(prepared.value_frequency),
                )
            dtype = {"precision": np.float64, "numeric": np.float32, "boolean": np.int32}[
                marker_kind
            ]
            return RawSeries(marker_kind, code, first, np.array([marker], dtype=dtype))
        if kind == "date":
            assert prepared.value_frequency is not None
            return RawSeries(
                "date",
                code,
                sentinels.index_nc,
                np.empty(0, dtype=np.int64),
                fame_frequency(prepared.value_frequency),
            )
        if kind == "string":
            return RawSeries("string", code, sentinels.index_nc, [])
        dtype = {"precision": np.float64, "numeric": np.float32, "boolean": np.int32}[kind]
        return RawSeries(kind, code, sentinels.index_nc, np.empty(0, dtype=dtype))
    if kind in ("precision", "numeric"):
        values = prepared.values
        nan = np.isnan(values)
        if nan.any():
            values[nan] = sentinel_value(kind, MISSING_NC, sentinels)
        return RawSeries(kind, code, first, values)
    if kind == "boolean":
        return RawSeries("boolean", code, first, prepared.values)
    if kind == "date":
        assert prepared.value_frequency is not None
        value_code = fame_frequency(prepared.value_frequency)
        indices = np.empty(count, dtype=np.int64)
        for position, item in enumerate(prepared.values):
            indices[position] = (
                sentinels.index_nc if item is None else mit_to_index(item, session=owner)
            )
        return RawSeries("date", code, first, indices, value_code)
    strings = [
        sentinel_value("string", MISSING_NC, sentinels) if item is None else item
        for item in prepared.values
    ]
    return RawSeries("string", code, first, strings)


def _check_case_range(first: int, count: int) -> None:
    if count and first + count - 1 >= 2**63:
        raise DataValidationError("The case range overflows a signed 64-bit index.")


# -- unfame ---------------------------------------------------------------------


def unfame(
    obj: FameObject,
    *,
    database: FameDatabase | None = None,
    session: Session | None = None,
    missing: str = "nan",
    empty: str = "preserve",
    empty_firstdate: MIT | None = None,
    text: str = "ascii",
) -> Any:
    """Convert a ``FameObject`` that holds data into a Python value.

    The object comes from ``do_read`` (or ``refame``); one without data is
    refused. See the module note for the representation table and the
    ``missing``, ``empty`` and ``text`` policies; ``empty_firstdate`` gives a
    truly empty series the first date it does not store.
    """
    if not isinstance(obj, FameObject):
        raise TypeError("unfame expects a FameObject; read it with do_read first.")
    owner = owner_session(database, session)
    raw = raw_of(obj, owner.sentinels.index_nc)
    return from_fame(
        raw,
        session=owner,
        missing=missing,
        empty=empty,
        empty_firstdate=empty_firstdate,
        text=text,
    )


def _text(value: bytes, text: str) -> str | bytes:
    return decode_value(value, text)


def _check_raw_frequency(raw: RawObject) -> None:
    if isinstance(raw, RawSeries) and not is_supported_frequency(raw.frequency):
        tsecon_frequency(raw.frequency)  # raises with the library's frequency name
    if raw.kind == "date":
        assert raw.date_frequency is not None
        if not is_supported_frequency(raw.date_frequency):
            tsecon_frequency(raw.date_frequency)


def from_fame(
    raw: RawObject,
    *,
    database: FameDatabase | None = None,
    session: Session | None = None,
    missing: str = "nan",
    empty: str = "preserve",
    empty_firstdate: MIT | None = None,
    text: str = "ascii",
) -> Any:
    """Convert an internal carrier into a Python value (``unfame`` on a carrier).

    See the module note for the representation table and the policies.
    """
    if not isinstance(raw, (RawScalar, RawSeries)):
        raise TypeError("from_fame expects a RawScalar or RawSeries.")
    check_policies(missing, empty, text)
    _check_raw_frequency(raw)
    owner = owner_session(database, session)
    if isinstance(raw, RawScalar):
        return _scalar_from_fame(raw, owner, missing, text)
    return _series_from_fame(raw, owner, missing, empty, empty_firstdate, text)


def _scalar_from_fame(raw: RawScalar, owner: Session, missing: str, text: str) -> Any:
    sentinels = owner.sentinels
    kind = raw.kind
    if kind == "namelist":
        members = namelist_members(raw.value)
        return NameList(member.decode("ascii") for member in members)
    code = int(classify_by_sentinel(_as_array(raw), kind, sentinels)[0])
    if code != MISSING_NORMAL:
        if missing == "strict" or kind == "boolean":
            raise MissingValueError(f"The {kind} scalar is missing.")
        if kind == "precision":
            return math.nan
        if kind == "numeric":
            return np.float32(np.nan)
        return None
    if kind == "precision":
        return float(raw.value)
    if kind == "numeric":
        return np.float32(raw.value)
    if kind == "boolean":
        return int(raw.value) != 0
    if kind == "date":
        assert raw.date_frequency is not None
        return index_to_mit(int(raw.value), raw.date_frequency, session=owner)
    return _text(raw.value, text)


def _as_array(raw: RawScalar) -> Any:
    if raw.kind == "string":
        return [raw.value]
    dtype = {
        "precision": np.float64,
        "numeric": np.float32,
        "boolean": np.int32,
        "date": np.int64,
    }[raw.kind]
    return np.array([raw.value], dtype=dtype)


def _series_from_fame(
    raw: RawSeries,
    owner: Session,
    missing: str,
    empty: str,
    empty_firstdate: MIT | None,
    text: str,
) -> Any:
    sentinels = owner.sentinels
    kind = raw.kind
    frequency = tsecon_frequency(raw.frequency)
    value_frequency = None if raw.date_frequency is None else tsecon_frequency(raw.date_frequency)
    if raw.is_empty:
        if empty_firstdate is None:
            raise EmptySeriesError(
                "The FAME series is empty and stores no first date; pass empty_firstdate."
            )
        if not isinstance(empty_firstdate, MIT) or empty_firstdate.frequency != frequency:
            raise DataValidationError("empty_firstdate has the wrong frequency.")
        return _empty_series(kind, empty_firstdate, value_frequency)
    codes = classify_by_sentinel(raw.values, kind, sentinels)
    firstdate = index_to_mit(raw.first_index, raw.frequency, session=owner)
    if empty == "reference" and kind != "string" and len(raw) == 1 and codes[0] != MISSING_NORMAL:
        return _empty_series(kind, firstdate, value_frequency)
    any_missing = bool(np.any(codes != MISSING_NORMAL))
    if any_missing and (missing == "strict" or kind == "boolean"):
        raise MissingValueError(f"The {kind} series contains missing observations.")
    if kind in ("precision", "numeric"):
        values = np.array(raw.values, copy=True)
        values[codes != MISSING_NORMAL] = np.nan
        return TSeries(firstdate, values)
    if kind == "boolean":
        return TSeries(firstdate, np.asarray(raw.values) != 0)
    if kind == "date":
        assert value_frequency is not None
        assert raw.date_frequency is not None
        moments: list[MIT | None] = []
        for position, index in enumerate(raw.values):
            if codes[position] != MISSING_NORMAL:
                moments.append(None)
            else:
                moments.append(index_to_mit(int(index), raw.date_frequency, session=owner))
        return DateSeries(firstdate, moments, value_frequency)
    strings: list[str | bytes | None] = []
    for position, item in enumerate(raw.values):
        strings.append(None if codes[position] != MISSING_NORMAL else _text(item, text))
    return StringSeries(firstdate, strings)


def validate_value(value: Any, empty: str = "preserve", text: str = "ascii") -> None:
    """Run every database-independent check of ``to_fame`` without a session."""
    _prepare(value, empty, text)


def _empty_series(kind: str, firstdate: MIT, value_frequency: Frequency | None) -> Any:
    if kind == "date":
        assert value_frequency is not None
        return DateSeries(firstdate, (), value_frequency)
    if kind == "string":
        return StringSeries(firstdate, ())
    dtype = {"precision": np.float64, "numeric": np.float32, "boolean": np.bool_}[kind]
    return TSeries(firstdate, np.empty(0, dtype=dtype))
