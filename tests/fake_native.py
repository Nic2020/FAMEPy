# SPDX-License-Identifier: MIT
"""In-memory NativeInterface emulation for tests. Not FAME behavior evidence.

The fake models the contracts FAMEPy relies on: one initialization per
process, handles keyed by integers, unposted writes discarded on close,
uppercase object names, wildcard cursors, process-global ITEM options and
output redirection. Databases persist as pickles at their path so that
separate processes can verify posted data. Synthetic status codes are
documented below. Float32 values are stored as ``numpy.float32`` so that
bit patterns survive.

Behaviors observed on both protected hosts and modeled here on purpose:
string missing sentinels are two bytes that are not ASCII text; the local
database open rejects the write and direct-write modes with the bad-mode
status (5) and creates nothing; an ``ITEM FREQUENCY`` word that is not a
documented family (``CASE``, an anchored name) is a bad option (67); a
command displayed while no redirection is active goes to the C-level
standard output of the process. The documented selector semantics are
modeled as documented, not as observed: ``ITEM FREQUENCY <family>`` narrows
date-indexed series by family, ``ITEM INDEX CASE``/``DATE`` narrows series
by index kind, and neither touches scalars. Endpoint handling of missing
observations is measured per campaign, so the fake stores exactly what is
written. A namelist reads back exactly as written by default; the library
documents no fixed layout, so variants exist for a different layout and for
corrupted members.

Intentionally faulty variants (``make_*_backend``) exist so that the
validation runner can be shown to report FAIL/BLOCKED for each defect.
"""

from __future__ import annotations

import copy
import ctypes as ct
import os
import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from tsecon import MIT, BDaily, Daily, Weekly, bdaily, daily, mit_to_date, weekly

from famepy._constants import (
    FREQUENCY_CASE,
    FREQUENCY_FAMILIES,
    FREQUENCY_MONTHLY,
    FREQUENCY_NAMES,
)
from famepy._native import FameRange, Sentinels, WildcardEntry

HSUCC, HFIN, HBMODE, HNOOBJ, HBOBJT, HTRUNC, HNRESW, HBOPT, HFAMER = (
    0,
    3,
    5,
    13,
    16,
    18,
    25,
    67,
    513,
)
# A small sample of names the library refuses as object names (basic data
# type names and missing-value codes): the model of the documented refusal,
# not the vendor's reserved-word list and not an exhaustive validator.
RESERVED_NAME_SAMPLE = frozenset(
    {"NAMELIST", "NUMERIC", "PRECISION", "BOOLEAN", "STRING", "DATE", "CASE", "NC", "NA", "ND"}
)
# Documented ITEM option words the fake accepts; anything else is a bad option.
OPTION_LABELS: dict[bytes, frozenset[bytes]] = {
    b"ALIAS": frozenset(),
    b"CLASS": frozenset({b"FORMULA", b"GLFORMULA", b"GLNAME", b"SCALAR", b"SERIES"}),
    b"TYPE": frozenset({b"BOOLEAN", b"DATE", b"NAMELIST", b"NUMERIC", b"PRECISION", b"STRING"}),
    b"FREQUENCY": frozenset(f.encode("ascii") for f in FREQUENCY_FAMILIES.values()),
    b"INDEX": frozenset({b"CASE", b"DATE"}),
}
# Synthetic statuses used only by this fake.
S_NOT_INITIALIZED = 901
S_ALREADY_INITIALIZED = 902
S_BAD_KEY = 903
S_READONLY = 904
S_EXISTS = 905
S_MISSING_FILE = 906
S_TYPE_MISMATCH = 907
S_RANGE = 908
S_BAD_WILDCARD = 909
S_BAD_MODE = 910
S_NAME_TOO_LONG = 911
S_REFUSED_OBJECT = 912  # a synthetic per-object creation refusal (self-test only)
S_CLASSIFIER = 913  # a synthetic classifier failure (self-test only)
S_BAD_YEAR = 914  # year outside the calendar the fake models (100..9999)
S_BAD_DATE = 915  # period outside the year for the frequency
S_BAD_FREQUENCY = 916  # frequency without a calendar in the fake

# The fake's calendar. Monthly indices keep the ``12 * year + period - 1``
# layout the earlier tests rely on; every other calendar frequency maps a
# moment to its TimeSeriesEconPy integer value plus an offset, so that code
# which assumed a library index equals a tsecon value would be caught.
# Case (232) indices are never converted by the bridge; the fake refuses them.
CALENDAR_OFFSET = 5_000_000

PRIVATE_MARKER = "SYNTHETIC_PRIVATE_PATH_TOKEN"


def _f64(bits: int) -> float:
    return float(np.array([bits], dtype=np.uint64).view(np.float64)[0])


def _f32(bits: int) -> Any:
    return np.array([bits], dtype=np.uint32).view(np.float32)[0]


SENTINELS = Sentinels(
    index_nc=-(2**62) - 1,
    index_na=-(2**62) - 2,
    index_nd=-(2**62) - 3,
    precision_nc=_f64(0x7FF8000000000101),
    precision_na=_f64(0x7FF8000000000102),
    precision_nd=_f64(0x7FF8000000000103),
    numeric_nc=_f32(0x7FC00101),
    numeric_na=_f32(0x7FC00102),
    numeric_nd=_f32(0x7FC00103),
    boolean_nc=-2147483647,
    boolean_na=-2147483646,
    boolean_nd=-2147483645,
    # Two bytes each, not ASCII text: the library's string sentinels are not
    # decodable text either (both hosts). Synthetic values, not vendor ones.
    string_nc=b"\xfe\x01",
    string_na=b"\xfe\x02",
    string_nd=b"\xfe\x03",
)

_TYPE_OF_KIND = {5: "precision", 1: "numeric", 3: "boolean", 4: "string", 2: "namelist"}
_DTYPES = {"precision": np.float64, "numeric": np.float32, "boolean": np.int32, "date": np.int64}


class FakeStatus(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


@dataclass
class FakeObject:
    class_code: int
    type_code: int
    frequency: int
    first: int
    last: int
    basis: int
    observed: int
    values: Any = None  # list for scalars/series

    def kind(self) -> str:
        return "date" if self.type_code >= 8 else _TYPE_OF_KIND[self.type_code]


@dataclass
class FakeHandle:
    name: str
    mode: int
    objects: dict[str, FakeObject]
    path: Path | None
    is_work: bool = False


@dataclass
class FakeNative:
    """See module docstring."""

    persist: bool = True
    initialized: bool = False
    init_count: int = 0
    fin_count: int = 0
    handles: dict[int, FakeHandle] = field(default_factory=dict)
    next_key: int = 1
    work: dict[str, FakeObject] = field(default_factory=dict)
    options: dict[bytes, bytes] = field(default_factory=dict)
    cursors: dict[int, list[tuple[str, FakeObject]]] = field(default_factory=dict)
    next_cursor: int = 100
    commands: list[bytes] = field(default_factory=list)
    output_path: Path | None = None
    fail_next: dict[str, int] = field(default_factory=dict)
    error_text: bytes = b""
    calls: list[str] = field(default_factory=list)
    missing_symbols: set[str] = field(default_factory=set)
    version_value: float = 11.8
    memory: dict[str, dict[str, FakeObject]] = field(default_factory=dict)
    profile: Sentinels = SENTINELS
    # Fault switches used by the intentionally faulty backends.
    canonicalize_nan: bool = False
    discard_posts: bool = False
    leak_marker: bool = False
    init_delay: float = 0.0
    stream_noise: bool = False
    refuse_redirect: int | None = None
    refuse_restore: int | None = None
    refuse_modes: dict[int, int] = field(default_factory=lambda: {6: HBMODE, 7: HBMODE})
    create_on_refusal: bool = False
    refuse_options: set[bytes] = field(default_factory=set)
    namelist_layout: str | None = None
    namelist_corruption: str | None = None
    refuse_objects: dict[str, int] = field(default_factory=dict)
    trim_nd: bool = False
    drop_neighbour: bool = False
    truncate_multibyte: bool = False
    corrupt_reads: dict[str, Any] = field(default_factory=dict)
    shift_ranges: dict[str, int] = field(default_factory=dict)
    fail_after: dict[str, list[int]] = field(default_factory=dict)
    shift_periods: bool = False
    ignore_leap_days: bool = False
    boolean_missing_as_one: bool = False
    omit_from_listing: set[str] = field(default_factory=set)
    nc_to_na_nonmonthly: bool = False
    extended_length_override: int | None = None

    # -- helpers -------------------------------------------------------

    def _enter(self, name: str, *, needs_init: bool = True) -> None:
        self.calls.append(name)
        status = self.fail_next.pop(name, None)
        if status is not None:
            raise FakeStatus(status)
        countdown = self.fail_after.get(name)
        if countdown is not None:
            # [remaining successful calls, status]: fails once the count is spent.
            if countdown[0] == 0:
                del self.fail_after[name]
                raise FakeStatus(countdown[1])
            countdown[0] -= 1
        if needs_init and not self.initialized:
            raise FakeStatus(S_NOT_INITIALIZED)

    def _handle(self, key: int) -> FakeHandle:
        try:
            return self.handles[key]
        except KeyError:
            raise FakeStatus(S_BAD_KEY) from None

    def _object(self, key: int, name: bytes) -> FakeObject:
        handle = self._handle(key)
        try:
            return handle.objects[name.decode("ascii").upper()]
        except (KeyError, UnicodeDecodeError):
            raise FakeStatus(HNOOBJ) from None

    def _writable(self, key: int) -> FakeHandle:
        handle = self._handle(key)
        if handle.mode == 1:
            raise FakeStatus(S_READONLY)
        return handle

    def _store_path(self, name: bytes) -> Path:
        return Path(name.decode("ascii"))

    def has_symbol(self, name: str) -> bool:
        return name not in self.missing_symbols

    # -- lifetime -------------------------------------------------------

    def initialize(self) -> None:
        """One-shot: after a finalization the fake reports HFIN like the library."""
        self._enter("cfmini", needs_init=False)
        if self.init_delay:
            time.sleep(self.init_delay)
        if self.stream_noise:
            # C-level writes that bypass sys.stdout/sys.stderr, as a native
            # library would produce; they must never reach a parent's report.
            _write_descriptor(1, b'{"group": "forged", "cases": []}\n' + b"noise " * 8)
            _write_descriptor(2, b"native diagnostic text\n")
        if self.leak_marker:
            _write_descriptor(1, PRIVATE_MARKER.encode() + b"\n")
        if self.fin_count:
            raise FakeStatus(HFIN)
        if self.initialized:
            raise FakeStatus(S_ALREADY_INITIALIZED)
        self.initialized = True
        self.init_count += 1

    def finalize(self) -> None:
        """One-shot: a second finalization reports HFIN."""
        self._enter("cfmfin", needs_init=False)
        if self.fin_count:
            raise FakeStatus(HFIN)
        if not self.initialized:
            raise FakeStatus(S_NOT_INITIALIZED)
        self.initialized = False
        self.fin_count += 1
        self.handles.clear()
        self.work.clear()
        self.cursors.clear()
        self.output_path = None

    def version(self) -> float:
        self._enter("cfmver")
        return self.version_value

    def sentinels(self) -> Sentinels:
        self.calls.append("globals")
        return self.profile

    # -- databases ------------------------------------------------------

    def open_work(self) -> int:
        self._enter("cfmopwk")
        for handle in self.handles.values():
            if handle.is_work:
                raise FakeStatus(S_EXISTS)
        key = self.next_key
        self.next_key += 1
        self.handles[key] = FakeHandle("WORK", 4, self.work, None, is_work=True)
        return key

    def open_database(self, name: bytes, mode: int) -> int:
        self._enter("cfmopdb")
        if not 1 <= mode <= 7:
            raise FakeStatus(S_BAD_MODE)
        if mode in self.refuse_modes:
            if self.create_on_refusal and self.persist:
                # A defective library: refuses the mode but leaves a file behind.
                self._store_path(name).write_bytes(pickle.dumps({}))
            raise FakeStatus(self.refuse_modes[mode])
        text = name.decode("ascii")
        if self.persist:
            path = self._store_path(name)
            exists = path.is_file()
            if mode == 2 and exists:
                raise FakeStatus(S_EXISTS)
            if mode in (1, 4, 5, 6, 7) and not exists:
                raise FakeStatus(S_MISSING_FILE)
            objects: dict[str, FakeObject]
            if mode == 3 or not exists:
                objects = {}
                path.write_bytes(pickle.dumps(objects))
            else:
                objects = pickle.loads(path.read_bytes())
        else:
            path = None
            exists = text in self.memory
            if mode == 2 and exists:
                raise FakeStatus(S_EXISTS)
            if mode in (1, 4, 5, 6, 7) and not exists:
                raise FakeStatus(S_MISSING_FILE)
            if mode == 3 or not exists:
                self.memory[text] = {}
            objects = copy.deepcopy(self.memory[text])
        key = self.next_key
        self.next_key += 1
        self.handles[key] = FakeHandle(text, mode, objects, path)
        return key

    def close_database(self, key: int) -> None:
        self._enter("cfmcldb")
        self._handle(key)
        del self.handles[key]

    def post_database(self, key: int) -> None:
        self._enter("cfmpodb")
        handle = self._handle(key)
        if handle.is_work:
            return
        if handle.mode == 1:
            raise FakeStatus(S_READONLY)
        if self.discard_posts:
            return
        if handle.path is not None:
            handle.path.write_bytes(pickle.dumps(handle.objects))
        else:
            self.memory[handle.name] = copy.deepcopy(handle.objects)

    # -- objects --------------------------------------------------------

    def quick_info(self, key: int, name: bytes) -> tuple[int, int, int, int, int]:
        self._enter("fame_quick_info")
        if self.leak_marker and name.upper() == b"OTHER":
            raise OSError(13, PRIVATE_MARKER, PRIVATE_MARKER + ".db")
        obj = self._object(key, name)
        shift = self.shift_ranges.get(name.decode("ascii").upper(), 0)
        return obj.class_code, obj.type_code, obj.frequency, obj.first + shift, obj.last + shift

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
        self._enter("cfmnwob")
        handle = self._writable(key)
        text = name.decode("ascii").upper()
        if len(text) > 242:
            raise FakeStatus(S_NAME_TOO_LONG)
        if text in RESERVED_NAME_SAMPLE:
            raise FakeStatus(HNRESW)
        if text in handle.objects:
            raise FakeStatus(S_EXISTS)
        if text in self.refuse_objects:
            raise FakeStatus(self.refuse_objects[text])
        if class_code not in (1, 2):
            raise FakeStatus(HBOPT)
        if type_code == FREQUENCY_CASE or (type_code >= 8 and type_code not in FREQUENCY_NAMES):
            # The library's type boundary: the case frequency (and any unknown
            # code) is not an object type, whatever the index frequency.
            raise FakeStatus(HBOBJT)
        nc = self.profile.index_nc
        handle.objects[text] = FakeObject(
            class_code,
            type_code,
            frequency,
            nc if class_code == 1 else 0,
            nc if class_code == 1 else 0,
            basis,
            observed,
            None,
        )

    def delete_object(self, key: int, name: bytes) -> None:
        self._enter("cfmdlob")
        handle = self._writable(key)
        text = name.decode("ascii").upper()
        if text not in handle.objects:
            raise FakeStatus(HNOOBJ)
        del handle.objects[text]

    # -- data -----------------------------------------------------------

    def _read(
        self, key: int, name: bytes, kind: str, range_: FameRange | None, count: int
    ) -> list[Any]:
        obj = self._object(key, name)
        if obj.kind() != kind:
            raise FakeStatus(S_TYPE_MISMATCH)
        if obj.values is None:
            raise FakeStatus(S_RANGE)
        if range_ is None:
            if obj.class_code != 2:
                raise FakeStatus(S_RANGE)
            return [obj.values[0]]
        if obj.class_code != 1 or range_.frequency != obj.frequency:
            raise FakeStatus(S_RANGE)
        corrupted = self.corrupt_reads.get(name.decode("ascii").upper())
        if corrupted is not None:
            # A defective backend for the self-test: returns invented values.
            return list(corrupted)[:count]
        shift = self.shift_ranges.get(name.decode("ascii").upper(), 0)
        if shift:
            return list(obj.values[:count])
        if range_.first < obj.first or range_.last > obj.last:
            raise FakeStatus(S_RANGE)
        offset = range_.first - obj.first
        return list(obj.values[offset : offset + count])

    def _write(
        self, key: int, name: bytes, kind: str, range_: FameRange | None, values: list[Any]
    ) -> None:
        handle = self._writable(key)
        obj = self._object(key, name)
        del handle
        if obj.kind() != kind:
            raise FakeStatus(S_TYPE_MISMATCH)
        if range_ is None:
            if obj.class_code != 2:
                raise FakeStatus(S_RANGE)
            obj.values = [values[0]]
            return
        if obj.class_code != 1 or range_.frequency != obj.frequency:
            raise FakeStatus(S_RANGE)
        if self.trim_nd:
            # Alternative endpoint rule for the runner self-test only: leading
            # and trailing ND observations are not stored. Not vendor evidence.
            values, first = self._trimmed(kind, list(values), range_.first)
            if not values:
                return
            range_ = FameRange(range_.frequency, first, first + len(values) - 1)
        if obj.values is None:
            obj.values = list(values)
            obj.first, obj.last = range_.first, range_.last
            return
        # Extend or overwrite within an existing range.
        new_first = min(obj.first, range_.first)
        new_last = max(obj.last, range_.last)
        filler = self._filler(kind)
        merged = [filler] * (new_last - new_first + 1)
        merged[obj.first - new_first : obj.first - new_first + len(obj.values)] = obj.values
        merged[range_.first - new_first : range_.first - new_first + len(values)] = values
        obj.values, obj.first, obj.last = merged, new_first, new_last

    def _trimmed(self, kind: str, values: list[Any], first: int) -> tuple[list[Any], int]:
        def is_nd(value: Any) -> bool:
            return self.missing_type(kind, value) == 3

        trailing = 0
        while values and is_nd(values[-1]):
            values.pop()
            trailing += 1
        if self.drop_neighbour and trailing and values:
            values.pop()  # a defective rule: the normal neighbour is lost too
        while values and is_nd(values[0]):
            values.pop(0)
            first += 1
        return values, first

    def _filler(self, kind: str) -> Any:
        return {
            "precision": self.profile.precision_na,
            "numeric": self.profile.numeric_na,
            "boolean": self.profile.boolean_na,
            "date": self.profile.index_na,
            "string": self.profile.string_na,
        }[kind]

    def _get(
        self,
        function: str,
        kind: str,
        key: int,
        name: bytes,
        range_: FameRange | None,
        out: np.ndarray,
    ) -> None:
        self._enter(function)
        values = self._read(key, name, kind, range_, out.shape[0])
        out[:] = np.array(values, dtype=out.dtype)

    def get_precisions(
        self, key: int, name: bytes, range_: FameRange | None, out: np.ndarray
    ) -> None:
        self._get("fame_get_precisions", "precision", key, name, range_, out)

    def get_numerics(
        self, key: int, name: bytes, range_: FameRange | None, out: np.ndarray
    ) -> None:
        self._get("fame_get_numerics", "numeric", key, name, range_, out)

    def get_booleans(
        self, key: int, name: bytes, range_: FameRange | None, out: np.ndarray
    ) -> None:
        self._get("fame_get_booleans", "boolean", key, name, range_, out)
        if self.boolean_missing_as_one:
            # A defective backend: missing Boolean codes come back as true.
            missing = (
                (out == self.profile.boolean_nc)
                | (out == self.profile.boolean_na)
                | (out == self.profile.boolean_nd)
            )
            out[missing] = 1

    def get_dates(self, key: int, name: bytes, range_: FameRange | None, out: np.ndarray) -> None:
        self._get("fame_get_dates", "date", key, name, range_, out)

    def get_strings(
        self, key: int, name: bytes, range_: FameRange | None, count: int
    ) -> list[bytes]:
        self._enter("fame_len_strings")
        self._enter("fame_get_strings")
        values = [bytes(v) for v in self._read(key, name, "string", range_, count)]
        if self.truncate_multibyte:
            # A defective library: the last byte of a value whose last byte is
            # not ASCII is lost on read (stored bytes intact).
            values = [v[:-1] if v and v[-1] >= 0x80 else v for v in values]
        return values

    def _put(
        self, function: str, kind: str, key: int, name: bytes, range_: FameRange | None, values: Any
    ) -> None:
        self._enter(function)
        # Keep exact-width NumPy scalars so that float32 bit patterns survive.
        array = np.array(values, dtype=_DTYPES[kind], copy=True)
        if kind == "precision" and self.canonicalize_nan:
            array[np.isnan(array)] = np.nan
        if (
            kind == "precision"
            and self.nc_to_na_nonmonthly
            and range_ is not None
            and range_.frequency != FREQUENCY_MONTHLY
        ):
            # A defective backend: every NC becomes NA outside the monthly
            # calendar, while everything else is stored exactly.
            nc = np.array(self.profile.precision_nc, dtype=np.float64).view(np.uint64)
            array[array.view(np.uint64) == nc] = self.profile.precision_na
        self._write(key, name, kind, range_, list(array))

    def write_precisions(
        self, key: int, name: bytes, range_: FameRange | None, values: np.ndarray
    ) -> None:
        self._put("fame_write_precisions", "precision", key, name, range_, values)

    def write_numerics(
        self, key: int, name: bytes, range_: FameRange | None, values: np.ndarray
    ) -> None:
        self._put("fame_write_numerics", "numeric", key, name, range_, values)

    def write_booleans(
        self, key: int, name: bytes, range_: FameRange | None, values: np.ndarray
    ) -> None:
        self._put("fame_write_booleans", "boolean", key, name, range_, values)

    def write_dates(
        self, key: int, name: bytes, range_: FameRange | None, type_code: int, values: np.ndarray
    ) -> None:
        obj = self._object(key, name)
        if obj.type_code != type_code:
            self.calls.append("fame_write_dates")
            raise FakeStatus(S_TYPE_MISMATCH)
        self._put("fame_write_dates", "date", key, name, range_, values)

    def write_strings(self, key: int, name: bytes, range_: FameRange | None, values: Any) -> None:
        self._enter("fame_write_strings")
        self._write(key, name, "string", range_, [bytes(v) for v in values])

    def get_namelist(self, key: int, name: bytes) -> bytes:
        self._enter("cfmnlen")
        self._enter("cfmgtnl")
        obj = self._object(key, name)
        if obj.kind() != "namelist":
            raise FakeStatus(S_TYPE_MISMATCH)
        stored = bytes(obj.values[0]) if obj.values else b""
        if self.namelist_layout is None and self.namelist_corruption is None:
            return stored
        members = [m.strip() for m in stored[1:-1].split(b",") if m.strip()]
        if self.namelist_corruption == "reorder" and len(members) > 1:
            members = members[::-1]
        elif self.namelist_corruption == "drop" and members:
            members = members[:-1]
        separator = b", " if self.namelist_layout == "blank_after_comma" else b","
        return b"{" + separator.join(members) + b"}"

    def write_namelist(self, key: int, name: bytes, value: bytes) -> None:
        self._enter("cfmwtnl")
        self._writable(key)
        obj = self._object(key, name)
        if obj.kind() != "namelist":
            raise FakeStatus(S_TYPE_MISMATCH)
        obj.values = [bytes(value)]

    # -- options, wildcards, commands ----------------------------------

    def set_option(self, name: bytes, value: bytes) -> None:
        self._enter("cfmsopt")
        words = name.split(b" ")
        if (
            len(words) not in (2, 3)
            or words[0] != b"ITEM"
            or words[1] not in OPTION_LABELS
            or (len(words) == 3 and words[2] not in OPTION_LABELS[words[1]])
            or value not in (b"ON", b"OFF")
            or name in self.refuse_options
        ):
            raise FakeStatus(HBOPT)
        if name.count(b" ") == 1:
            # Setting the option itself clears its per-value selections.
            for key in [k for k in self.options if k.startswith(name + b" ")]:
                del self.options[key]
        self.options[name] = value

    def _filter(self, obj: FakeObject) -> bool:
        from famepy._constants import ObjectClass, ObjectType

        def allowed(option: bytes, label: bytes) -> bool:
            if self.options.get(b"ITEM " + option, b"ON") == b"ON":
                return True
            return self.options.get(b"ITEM " + option + b" " + label, b"OFF") == b"ON"

        class_label = ObjectClass(obj.class_code).name.encode()
        type_label = b"DATE" if obj.type_code >= 8 else ObjectType(obj.type_code).name.encode()
        if not (allowed(b"CLASS", class_label) and allowed(b"TYPE", type_label)):
            return False
        if obj.class_code != ObjectClass.SERIES:
            return True  # frequency and index selectors are about series
        if obj.frequency == FREQUENCY_CASE:
            return allowed(b"INDEX", b"CASE")
        family = FREQUENCY_FAMILIES.get(obj.frequency)
        return allowed(b"INDEX", b"DATE") and (
            family is None or allowed(b"FREQUENCY", family.encode("ascii"))
        )

    def init_wildcard(self, key: int, pattern: bytes) -> int:
        self._enter("fame_init_wildcard")
        handle = self._handle(key)
        text = pattern.decode("ascii").upper()
        if not text:
            raise FakeStatus(S_BAD_WILDCARD)
        regex = (
            "^"
            + "".join(".*" if ch == "?" else "." if ch == "^" else re.escape(ch) for ch in text)
            + "$"
        )
        matched = [
            (name, obj)
            for name, obj in sorted(handle.objects.items())
            if re.match(regex, name) and self._filter(obj) and name not in self.omit_from_listing
        ]
        cursor = self.next_cursor
        self.next_cursor += 1
        self.cursors[cursor] = matched
        return cursor

    def next_wildcard(self, wildcard_key: int, capacity: int) -> WildcardEntry:
        self._enter("fame_get_next_wildcard")
        try:
            entries = self.cursors[wildcard_key]
        except KeyError:
            raise FakeStatus(S_BAD_KEY) from None
        if not entries:
            return WildcardEntry(HNOOBJ, b"", -1, -1, -1, -1, -1, 0)
        name, obj = entries.pop(0)
        encoded = name.encode("ascii")
        status = HTRUNC if len(encoded) > capacity else HSUCC
        # The reference notes that wildcard ranges are unreliable for scalars.
        first, last = (obj.first, obj.last) if obj.class_code == 1 else (-7, -7)
        return WildcardEntry(
            status,
            encoded[:capacity],
            obj.class_code,
            obj.type_code,
            obj.frequency,
            first,
            last,
            len(encoded),
        )

    def free_wildcard(self, wildcard_key: int) -> None:
        self._enter("fame_free_wildcard")
        self.cursors.pop(wildcard_key, None)

    def execute(self, command: bytes) -> int:
        self.calls.append("cfmfame")
        status = self.fail_next.pop("cfmfame", None)
        if status is not None:
            return status
        if not self.initialized:
            return S_NOT_INITIALIZED
        self.commands.append(command)
        lowered = command.strip().lower()
        if lowered.startswith(b'output file("') and lowered.endswith(b'!")'):
            if self.refuse_redirect is not None:
                # A refused redirection leaves the terminal active, and the
                # library's own diagnostic goes to the C-level stdout.
                _write_descriptor(1, b"synthetic redirection diagnostic text 4\n")
                return self.refuse_redirect
            self.output_path = Path(command.strip()[13:-3].decode("ascii"))
            # The library creates the file itself; the package never pre-creates it.
            with open(self.output_path, "ab"):
                pass
            return HSUCC
        if lowered == b"output terminal":
            self.output_path = None
            if self.refuse_restore is not None:
                return self.refuse_restore
            return HSUCC
        if lowered.startswith(b"fail"):
            self.error_text = b"synthetic failure for " + lowered
            if self.leak_marker:
                self.error_text += b" " + PRIVATE_MARKER.encode()
            self._emit(b"partial output before failure\n")
            return int(lowered.split()[1]) if len(lowered.split()) > 1 else HFAMER
        if self.leak_marker:
            self._emit(PRIVATE_MARKER.encode() + b"\n")
        for line in command.splitlines():
            expression = line.strip().lower()
            match = re.fullmatch(rb"display[ \t]+(-?\d+)[ \t]*\+[ \t]*(-?\d+)", expression)
            if match is not None:
                self._emit(b"%d\n" % (int(match.group(1)) + int(match.group(2))))
            elif expression:
                self._emit(b"echo: " + line.strip() + b"\n")
        return HSUCC

    def extended_error_length(self) -> int:
        self._enter("cfmlerr")
        if self.extended_length_override is not None:
            return self.extended_length_override
        return len(self.error_text)

    def extended_error_fetch(self, buffer: Any) -> None:
        self._enter("cfmferr")
        # The library truncates to the caller's buffer length.
        capacity = len(buffer) - 1
        ct.memmove(buffer, self.error_text, min(capacity, len(self.error_text)))
        buffer[min(capacity, len(self.error_text))] = b"\0"

    def _emit(self, text: bytes) -> None:
        if self.output_path is not None:
            with open(self.output_path, "ab") as stream:
                stream.write(text)
        else:
            # "Terminal" output is the C-level standard output of the process.
            _write_descriptor(1, text)

    # -- classification and calendar -----------------------------------

    def missing_type(self, kind: str, value: Any) -> int:
        self._enter(
            {
                "precision": "cfmispm",
                "numeric": "cfmisnm",
                "boolean": "cfmisbm",
                "string": "cfmissm",
                "date": "fame_date_missing_type",
            }[kind]
        )
        table = {
            "precision": (
                self.profile.precision_nc,
                self.profile.precision_na,
                self.profile.precision_nd,
            ),
            "numeric": (self.profile.numeric_nc, self.profile.numeric_na, self.profile.numeric_nd),
            "boolean": (self.profile.boolean_nc, self.profile.boolean_na, self.profile.boolean_nd),
            "string": (self.profile.string_nc, self.profile.string_na, self.profile.string_nd),
            "date": (self.profile.index_nc, self.profile.index_na, self.profile.index_nd),
        }[kind]
        if kind in ("precision", "numeric"):
            dtype = np.float64 if kind == "precision" else np.float32
            bits = np.array(value, dtype=dtype).tobytes()
            for code, sentinel in enumerate(table, start=1):
                if np.array(sentinel, dtype=dtype).tobytes() == bits:
                    return code
            return 0
        for code, sentinel in enumerate(table, start=1):
            if value == sentinel:
                return code
        return 0

    def index_to_year_period(self, frequency: int, index: int) -> tuple[int, int]:
        self._enter("fame_index_to_year_period")
        if frequency == FREQUENCY_MONTHLY:
            year, period = divmod(index, 12)[0], divmod(index, 12)[1] + 1
        else:
            year, period = _fake_index_to_year_period(frequency, index)
        if self.shift_periods and frequency != FREQUENCY_MONTHLY:
            period += 1
        return year, period

    def year_period_to_index(self, frequency: int, year: int, period: int) -> int:
        self._enter("fame_year_period_to_index")
        if frequency == FREQUENCY_MONTHLY:
            if not 1 <= period <= 12:
                raise FakeStatus(HBOPT)
            return year * 12 + period - 1
        if not 100 <= year <= 9999:
            raise FakeStatus(S_BAD_YEAR)
        return _fake_year_period_to_index(frequency, year, period, self.ignore_leap_days)


_PPY = {
    **{code: 1 for code in range(192, 204)},
    **{code: 2 for code in range(204, 210)},
    **{code: 4 for code in range(160, 163)},
}
_WEEK_END_DAY = {16: 7, 17: 1, 18: 2, 19: 3, 20: 4, 21: 5, 22: 6}


def _first_business_day(year: int) -> Any:
    import datetime

    first = datetime.date(year, 1, 1)
    weekday = first.isoweekday()
    return first + datetime.timedelta(days=8 - weekday if weekday > 5 else 0)


def _fake_year_period_to_index(frequency: int, year: int, period: int, no_leap: bool) -> int:
    import datetime

    if frequency in _PPY:
        ppy = _PPY[frequency]
        if not 1 <= period <= ppy:
            raise FakeStatus(S_BAD_DATE)
        return ppy * year + period - 1 + CALENDAR_OFFSET
    if frequency == 8:
        days = 365 if no_leap else 366 if _leap(year) else 365
        if not 1 <= period <= days:
            raise FakeStatus(S_BAD_DATE)
        if no_leap:
            # A defective calendar: every year has 365 days, so days after
            # February in a leap year are shifted by one.
            date = datetime.date(year, 1, 1) + datetime.timedelta(days=period - 1)
            if datetime.date(year, 1, 1) + datetime.timedelta(days=59) < date and _leap(year):
                date += datetime.timedelta(days=1)
            return daily(date).value + CALENDAR_OFFSET
        return daily(datetime.date(year, 1, 1) + datetime.timedelta(days=period - 1)).value + (
            CALENDAR_OFFSET
        )
    if frequency == 9:
        start = bdaily(_first_business_day(year))
        end = bdaily(datetime.date(year, 12, 31), bias="previous")
        if not 1 <= period <= end.value - start.value + 1:
            raise FakeStatus(S_BAD_DATE)
        return start.value + period - 1 + CALENDAR_OFFSET
    if frequency in _WEEK_END_DAY:
        if not 1 <= period <= 53:
            raise FakeStatus(S_BAD_DATE)
        start = datetime.date(year, 1, 1) + datetime.timedelta(days=7 * (period - 1))
        moment = weekly(start, _WEEK_END_DAY[frequency])
        if _fake_index_to_year_period(frequency, moment.value + CALENDAR_OFFSET) != (year, period):
            raise FakeStatus(S_BAD_DATE)
        return moment.value + CALENDAR_OFFSET
    raise FakeStatus(S_BAD_FREQUENCY)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _fake_index_to_year_period(frequency: int, index: int) -> tuple[int, int]:
    value = index - CALENDAR_OFFSET
    if frequency in _PPY:
        year, remainder = divmod(value, _PPY[frequency])
        return year, remainder + 1
    try:
        if frequency == 8:
            date = mit_to_date(MIT(Daily(), value))
            return date.year, date.timetuple().tm_yday
        if frequency == 9:
            date = mit_to_date(MIT(BDaily(), value))
            return date.year, value - bdaily(_first_business_day(date.year)).value + 1
        if frequency in _WEEK_END_DAY:
            date = mit_to_date(MIT(Weekly(_WEEK_END_DAY[frequency]), value))
            return date.year, -(-date.timetuple().tm_yday // 7)
    except (ValueError, OverflowError):
        raise FakeStatus(S_BAD_DATE) from None
    raise FakeStatus(S_BAD_FREQUENCY)


def _write_descriptor(descriptor: int, data: bytes) -> None:
    try:
        os.write(descriptor, data)
    except OSError:
        pass


class StatusAdapter:
    """Wrap FakeNative so FakeStatus surfaces as HLIError like the real binding."""

    def __init__(self, fake: FakeNative) -> None:
        self.fake = fake

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.fake, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            from famepy._errors import HLIError

            try:
                return attribute(*args, **kwargs)
            except FakeStatus as failure:
                raise HLIError(failure.status, operation=name) from None

        return call


def make_fake(*, persist: bool = True) -> StatusAdapter:
    return StatusAdapter(FakeNative(persist=persist))


def make_validation_backend() -> StatusAdapter:
    """Factory used by the validation runner's self-test (persisting fake)."""
    return make_fake(persist=True)


def make_failing_backend() -> StatusAdapter:
    """A backend whose initialization fails with a licensing-style status."""
    adapter = make_fake(persist=True)
    adapter.fake.fail_next["cfmini"] = 97
    return adapter


# -- intentionally faulty backends: each must make the campaign FAIL/BLOCKED --


def make_negative_version_backend() -> StatusAdapter:
    """Reports a nonsensical version; the lifecycle predicate must fail."""
    adapter = make_fake(persist=True)
    adapter.fake.version_value = -1.0
    return adapter


def make_nan_canonicalizing_backend() -> StatusAdapter:
    """Loses NaN payloads on precision writes; missing categories collapse."""
    adapter = make_fake(persist=True)
    adapter.fake.canonicalize_nan = True
    return adapter


def make_nonpersisting_backend() -> StatusAdapter:
    """Accepts posts but never writes them; cross-process checks must fail."""
    adapter = make_fake(persist=True)
    adapter.fake.discard_posts = True
    return adapter


def make_leaky_backend() -> StatusAdapter:
    """Emits private-looking markers in output, error text and exceptions."""
    adapter = make_fake(persist=True)
    adapter.fake.leak_marker = True
    return adapter


def make_hanging_backend() -> StatusAdapter:
    """Blocks inside initialization long enough to trip a short timeout."""
    adapter = make_fake(persist=True)
    adapter.fake.init_delay = 60.0
    return adapter


def make_noisy_backend() -> StatusAdapter:
    """Writes forged JSON and diagnostics to the C-level streams.

    A correct runner ignores the streams entirely, so the campaign must still
    PASS and only the size of the stray output may appear in the report.
    """
    adapter = make_fake(persist=True)
    adapter.fake.stream_noise = True
    return adapter


def make_redirect_refusing_backend() -> StatusAdapter:
    """Every output redirection fails with the command-error status.

    Reproduces the shape of the first commands campaign: each command fails
    at the redirect stage, terminal output goes to the C-level stdout, and
    the report must name the stage and still be well formed.
    """
    adapter = make_fake(persist=True)
    adapter.fake.refuse_redirect = HFAMER
    return adapter


def make_restore_failing_backend() -> StatusAdapter:
    """The ``output terminal`` restoration fails after every payload."""
    adapter = make_fake(persist=True)
    adapter.fake.refuse_restore = 44
    return adapter


def make_partial_write_backend() -> StatusAdapter:
    """One object of the raw matrix cannot be created (synthetic status).

    The group must FAIL, every other object must still be written, read and
    verified across processes, the cases that depend on the missing object
    must be reported as blocked, and replacement/deletion (which use their
    own fixtures) must pass.
    """
    adapter = make_fake(persist=True)
    adapter.fake.refuse_objects = {"S_MISSING_SERIES": S_REFUSED_OBJECT}
    return adapter


def make_nd_trimming_backend() -> StatusAdapter:
    """Stores no leading or trailing ND observation (an alternative endpoint rule).

    The runner records endpoint behavior as observations, so the campaign must
    PASS under this rule as well as under exact storage; only the recorded
    ranges and codes differ. Interior missing values are unaffected.
    """
    adapter = make_fake(persist=True)
    adapter.fake.trim_nd = True
    return adapter


def make_value_dropping_backend() -> StatusAdapter:
    """Trims trailing ND and also loses the normal value before it.

    The endpoint interior assertion must catch this; observations alone
    would not.
    """
    adapter = make_fake(persist=True)
    adapter.fake.trim_nd = True
    adapter.fake.drop_neighbour = True
    return adapter


def make_endpoint_corrupting_backend() -> StatusAdapter:
    """Endpoint fixtures read back corrupted while their neighbours survive.

    One trailing-ND series returns an invented ordinary value in place of
    the ND, one all-ND series returns a changed missing code, and one
    leading-NC series reports a shifted range. The retained-value
    assertions must fail every one of them.
    """
    adapter = make_fake(persist=True)
    fake = adapter.fake
    fake.corrupt_reads = {
        "P_TRAILING_ND": [1.0, 99.0],
        "N_ALL_ND": [fake.profile.numeric_na, fake.profile.numeric_nd],
    }
    fake.shift_ranges = {"D_LEADING_NC": 1}
    return adapter


def make_classifier_failing_backend() -> StatusAdapter:
    """The numeric classifier fails partway through one object's verification.

    Only that object may fail; every object verified after it must still be
    checked.
    """
    adapter = make_fake(persist=True)
    adapter.fake.fail_after = {"cfmisnm": [2, S_CLASSIFIER]}
    return adapter


def make_mode_accepting_backend() -> StatusAdapter:
    """A local open that accepts the write and direct-write modes.

    That contradicts the documented local open, so the database group must
    FAIL rather than treat the unexpected success as parity.
    """
    adapter = make_fake(persist=True)
    adapter.fake.refuse_modes = {}
    return adapter


def make_mode_side_effect_backend() -> StatusAdapter:
    """Refuses the connection modes but leaves a file behind on a new path."""
    adapter = make_fake(persist=True)
    adapter.fake.create_on_refusal = True
    return adapter


def make_family_option_refusing_backend() -> StatusAdapter:
    """``ITEM FREQUENCY MONTHLY`` is a bad option here.

    The option error must surface in the listing cases that narrow by that
    family (never be hidden), while every other discovery case still runs.
    """
    adapter = make_fake(persist=True)
    adapter.fake.refuse_options = {b"ITEM FREQUENCY MONTHLY"}
    return adapter


def make_index_option_refusing_backend() -> StatusAdapter:
    """``ITEM INDEX CASE`` is a bad option here (see the family variant)."""
    adapter = make_fake(persist=True)
    adapter.fake.refuse_options = {b"ITEM INDEX CASE"}
    return adapter


def make_namelist_relayout_backend() -> StatusAdapter:
    """Namelists read back with a blank after each comma.

    Only the layout differs, which the library documents as its own
    choice, so the campaign must still PASS with the layout observed.
    """
    adapter = make_fake(persist=True)
    adapter.fake.namelist_layout = "blank_after_comma"
    return adapter


def make_namelist_corrupting_backend() -> StatusAdapter:
    """Namelists read back with their members reversed: must FAIL in both processes."""
    adapter = make_fake(persist=True)
    adapter.fake.namelist_layout = "blank_after_comma"
    adapter.fake.namelist_corruption = "reorder"
    return adapter


def make_namelist_dropping_backend() -> StatusAdapter:
    """Namelists read back without their last member: must FAIL in both processes."""
    adapter = make_fake(persist=True)
    adapter.fake.namelist_corruption = "drop"
    return adapter


def make_calendar_shifting_backend() -> StatusAdapter:
    """Reports every non-monthly index one period late: round trips must fail."""
    adapter = make_fake(persist=True)
    adapter.fake.shift_periods = True
    return adapter


def make_leap_ignoring_backend() -> StatusAdapter:
    """A daily calendar without leap days: adjacency across February must fail."""
    adapter = make_fake(persist=True)
    adapter.fake.ignore_leap_days = True
    return adapter


def make_boolean_coercing_backend() -> StatusAdapter:
    """Missing Boolean observations read back as true (the reference's bug shape)."""
    adapter = make_fake(persist=True)
    adapter.fake.boolean_missing_as_one = True
    return adapter


def make_nc_to_na_backend() -> StatusAdapter:
    """Stores NA in place of every NC on non-monthly precision writes.

    The bridge reads both as NaN, so only raw category assertions and
    fixture-built manifests can catch it: frequencies and workspace must FAIL.
    """
    adapter = make_fake(persist=True)
    adapter.fake.nc_to_na_nonmonthly = True
    return adapter


def make_kind_refusing_backend() -> StatusAdapter:
    """Refuses the creation of one bridge kind object; later kinds must still verify.

    The status is the library's reserved-name refusal, so the runner's
    per-object containment is exercised with a realistic primary failure.
    """
    adapter = make_fake(persist=True)
    adapter.fake.refuse_objects = {"K_BOOLEAN_SCALAR": HNRESW, "F_DAILY": HNRESW}
    return adapter


def make_listing_omitting_backend() -> StatusAdapter:
    """One written object never appears in wildcard listings: workspace reads must fail."""
    adapter = make_fake(persist=True)
    adapter.fake.omit_from_listing = {"C_BETA"}
    return adapter


# -- alternative sentinel profile: distinct finite (non-NaN) floating sentinels --

FINITE_SENTINELS = Sentinels(
    index_nc=SENTINELS.index_nc,
    index_na=SENTINELS.index_na,
    index_nd=SENTINELS.index_nd,
    # Values chosen away from anything the tests or runner write as data.
    precision_nc=1.125e301,
    precision_na=2.125e301,
    precision_nd=3.125e301,
    numeric_nc=np.float32(1.125e37),
    numeric_na=np.float32(2.125e37),
    numeric_nd=np.float32(3.125e37),
    boolean_nc=SENTINELS.boolean_nc,
    boolean_na=SENTINELS.boolean_na,
    boolean_nd=SENTINELS.boolean_nd,
    # A different non-ASCII shape (leading byte 0x80, one byte) so that the
    # two profiles disagree on string sentinels as well.
    string_nc=b"\x80C",
    string_na=b"\x80A",
    string_nd=b"\x80D",
)


def make_finite_sentinel_backend() -> StatusAdapter:
    """Synthetic profile whose floating sentinels are distinct finite values.

    Nothing here is a vendor value; it exists so that no code path may assume
    that missing floating observations are IEEE NaNs.
    """
    adapter = make_fake(persist=True)
    adapter.fake.profile = FINITE_SENTINELS
    return adapter


def make_overlong_error_backend() -> StatusAdapter:
    """The extended-error length call reports a length beyond the package bound.

    The retrieval must refuse to allocate, record the capture failure and
    leave the status untouched; the extended-errors group cannot pass.
    """
    adapter = make_fake(persist=True)
    adapter.fake.extended_length_override = 2**16 + 1
    return adapter


def make_range_failing_backend() -> StatusAdapter:
    """The first precision write fails with a range-style status.

    The benchmark failure record must carry the numeric status, the
    operation and the phase, and no timing data.
    """
    adapter = make_fake(persist=True)
    adapter.fake.fail_next["fame_write_precisions"] = 9
    return adapter


def make_text_truncating_backend() -> StatusAdapter:
    """Returns string values without their last byte when it is not ASCII.

    The text group's raw byte comparison and its decoded reads must catch
    this for every corpus label whose last character is multibyte, in the
    group and in the verification child; the ASCII labels must still pass.
    """
    adapter = make_fake(persist=True)
    adapter.fake.truncate_multibyte = True
    return adapter


def make_benchmark_corrupting_backend() -> StatusAdapter:
    """One benchmark series reads back with invented values.

    The harness verifies every read outside its timed regions, so this
    backend must yield a failed measurement and never accepted timings.
    """
    adapter = make_fake(persist=True)
    adapter.fake.corrupt_reads = {"S00003": [0.5] * 24}
    return adapter
