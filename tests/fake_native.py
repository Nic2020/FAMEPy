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
string missing sentinels are two bytes that are not ASCII text; the
``ITEM FREQUENCY <name>`` selection is accepted but has no effect on the
wildcard (class, type and alias selections do); a command displayed while
no redirection is active goes to the C-level standard output of the
process. Endpoint behavior of missing observations and the prerequisites
of the write/direct-write modes are not established, so the fake stores
exactly what is written and accepts those modes on an existing database.

Intentionally faulty variants (``make_*_backend``) exist so that the
validation runner can be shown to report FAIL/BLOCKED for each defect.
"""

from __future__ import annotations

import copy
import os
import pickle
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from famepy._constants import FREQUENCY_MONTHLY, FREQUENCY_UNDEFINED
from famepy._native import RangeSpec, Sentinels, WildcardEntry

HSUCC, HFIN, HNOOBJ, HTRUNC, HBOPT, HFAMER = 0, 3, 13, 18, 67, 513
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
    refuse_modes: dict[int, int] = field(default_factory=dict)
    refuse_objects: dict[str, int] = field(default_factory=dict)
    trim_nd: bool = False
    drop_neighbour: bool = False
    corrupt_reads: dict[str, Any] = field(default_factory=dict)
    shift_ranges: dict[str, int] = field(default_factory=dict)
    fail_after: dict[str, list[int]] = field(default_factory=dict)

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
        if text in handle.objects:
            raise FakeStatus(S_EXISTS)
        if text in self.refuse_objects:
            raise FakeStatus(self.refuse_objects[text])
        if class_code not in (1, 2):
            raise FakeStatus(HBOPT)
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
        self, key: int, name: bytes, kind: str, range_: RangeSpec | None, count: int
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
        self, key: int, name: bytes, kind: str, range_: RangeSpec | None, values: list[Any]
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
            range_ = RangeSpec(range_.frequency, first, first + len(values) - 1)
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
        range_: RangeSpec | None,
        out: np.ndarray,
    ) -> None:
        self._enter(function)
        values = self._read(key, name, kind, range_, out.shape[0])
        out[:] = np.array(values, dtype=out.dtype)

    def get_precisions(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None:
        self._get("fame_get_precisions", "precision", key, name, range_, out)

    def get_numerics(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None:
        self._get("fame_get_numerics", "numeric", key, name, range_, out)

    def get_booleans(
        self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray
    ) -> None:
        self._get("fame_get_booleans", "boolean", key, name, range_, out)

    def get_dates(self, key: int, name: bytes, range_: RangeSpec | None, out: np.ndarray) -> None:
        self._get("fame_get_dates", "date", key, name, range_, out)

    def get_strings(
        self, key: int, name: bytes, range_: RangeSpec | None, count: int
    ) -> list[bytes]:
        self._enter("fame_len_strings")
        self._enter("fame_get_strings")
        return [bytes(v) for v in self._read(key, name, "string", range_, count)]

    def _put(
        self, function: str, kind: str, key: int, name: bytes, range_: RangeSpec | None, values: Any
    ) -> None:
        self._enter(function)
        # Keep exact-width NumPy scalars so that float32 bit patterns survive.
        array = np.array(values, dtype=_DTYPES[kind], copy=True)
        if kind == "precision" and self.canonicalize_nan:
            array[np.isnan(array)] = np.nan
        self._write(key, name, kind, range_, list(array))

    def write_precisions(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None:
        self._put("fame_write_precisions", "precision", key, name, range_, values)

    def write_numerics(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None:
        self._put("fame_write_numerics", "numeric", key, name, range_, values)

    def write_booleans(
        self, key: int, name: bytes, range_: RangeSpec | None, values: np.ndarray
    ) -> None:
        self._put("fame_write_booleans", "boolean", key, name, range_, values)

    def write_dates(
        self, key: int, name: bytes, range_: RangeSpec | None, type_code: int, values: np.ndarray
    ) -> None:
        obj = self._object(key, name)
        if obj.type_code != type_code:
            self.calls.append("fame_write_dates")
            raise FakeStatus(S_TYPE_MISMATCH)
        self._put("fame_write_dates", "date", key, name, range_, values)

    def write_strings(self, key: int, name: bytes, range_: RangeSpec | None, values: Any) -> None:
        self._enter("fame_write_strings")
        self._write(key, name, "string", range_, [bytes(v) for v in values])

    def get_namelist(self, key: int, name: bytes) -> bytes:
        self._enter("cfmnlen")
        self._enter("cfmgtnl")
        obj = self._object(key, name)
        if obj.kind() != "namelist":
            raise FakeStatus(S_TYPE_MISMATCH)
        return bytes(obj.values[0]) if obj.values else b""

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
        if not name.startswith(b"ITEM ") or value not in (b"ON", b"OFF"):
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
        # ITEM FREQUENCY selections are accepted but not applied (observed on
        # both hosts); the package filters by metadata instead.
        return allowed(b"CLASS", class_label) and allowed(b"TYPE", type_label)

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
            if re.match(regex, name) and self._filter(obj)
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
            return divmod(index, 12)[0], divmod(index, 12)[1] + 1
        if frequency == FREQUENCY_UNDEFINED:
            raise FakeStatus(HBOPT)
        return divmod(index, 1000)[0], divmod(index, 1000)[1]

    def year_period_to_index(self, frequency: int, year: int, period: int) -> int:
        self._enter("fame_year_period_to_index")
        if frequency == FREQUENCY_MONTHLY:
            if not 1 <= period <= 12:
                raise FakeStatus(HBOPT)
            return year * 12 + period - 1
        if frequency == FREQUENCY_UNDEFINED:
            raise FakeStatus(HBOPT)
        return year * 1000 + period


def _write_descriptor(descriptor: int, data: bytes) -> None:
    try:
        os.write(descriptor, data)
    except OSError:
        pass


class StatusAdapter:
    """Wrap FakeNative so FakeStatus surfaces as FameError like the real binding."""

    def __init__(self, fake: FakeNative) -> None:
        self.fake = fake

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.fake, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            from famepy._errors import FameError

            try:
                return attribute(*args, **kwargs)
            except FakeStatus as failure:
                raise FameError(failure.status, operation=name) from None

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


def make_mode_refusing_backend() -> StatusAdapter:
    """Write and direct-write opens return status 5, as both hosts did.

    The database group must FAIL (not downgrade or relabel) until the mode
    prerequisites are established.
    """
    adapter = make_fake(persist=True)
    adapter.fake.refuse_modes = {6: 5, 7: 5}
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
