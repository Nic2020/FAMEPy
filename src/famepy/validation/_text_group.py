# SPDX-License-Identifier: MIT
"""The ``text`` group: string values under the value text policies, natively.

A fixed synthetic corpus of string scalars and string vectors is written and
read through the bridge under the ``utf-8``, ``bytes`` and ``ascii``
policies, the stored bytes are compared with independently listed expected
bytes (hex literals kept next to the text, not derived from the codec at
run time), decoded values are compared with the Python text by predicate,
every refusal that must happen before a native call is asserted, and a
verification child reopens the database and compares the bytes again.
Only fixed corpus labels, predicates, lengths, error class names and the
runner's own expected bytes are exported; a value the library returns
that differs from its fixture is reduced to its length.

When Julia is configured the group also runs the reference wrapper on the
same corpus: it writes every value with the reference's low-level calls,
reads them back and reads the Python-written objects, reporting per label
the stage outcome (``ok`` or the error class), string validity, code-unit
length and equality as predicates. The reference reads a string by
slicing its buffer with the native byte length on a character index, so a
value whose last character is multibyte fails there with a string index
error while the bytes are stored intact; those corpus labels are recorded
as *reference limitations* (observations), and the labels the reference
is known to read are required. Python's raw and decoded reads of the
reference-written objects are required for every label. Nothing here
claims that the library itself interprets text.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import famepy
from famepy import bridge
from famepy._constants import MISSING_NC, MISSING_NORMAL
from famepy._data import case_frequency, classify_by_sentinel, value_type_code
from famepy._errors import HNOOBJ
from famepy._text import TextEncodingError

from ._manifest import _read, manifest_object, verify_case_ids
from ._process import read_result, reserve_result, run_child
from ._report import Case

if TYPE_CHECKING:
    from ._groups import Context

# Expectation classes for the reference wrapper: ``supported`` labels must
# be written and read back by the reference (required cases), ``limitation``
# labels are known to fail in its read slicing (observations), ``observed``
# labels have no established reference outcome (observations), ``python``
# labels are not given to the reference at all.
SUPPORTED, LIMITATION, OBSERVED, PYTHON_ONLY = "supported", "limitation", "observed", "python"


@dataclass(frozen=True)
class TextFixture:
    label: str
    value: Any  # str or list[str | None]
    expected_hex: str | tuple[str | None, ...]  # independently listed UTF-8 bytes
    reference: str

    @property
    def name(self) -> str:
        return f"TXT_{self.label.upper()}"

    @property
    def is_vector(self) -> bool:
        return isinstance(self.value, list)


# The corpus. Hex columns are written out by hand from the Unicode tables
# (U+00E9 = c3 a9, U+65E5 = e6 97 a5, U+672C = e6 9c ac, U+8A9E = e8 aa 9e,
# U+1F600 = f0 9f 98 80) so the expected bytes do not come from the codec.
TEXT_CORPUS: tuple[TextFixture, ...] = (
    TextFixture("ascii", "Hello, FAME 2026", "48656c6c6f2c2046414d452032303236", SUPPORTED),
    TextFixture(
        "internal_multibyte", "caf\xe9 au lait", "636166c3a920617520" + "6c616974", SUPPORTED
    ),
    TextFixture("terminal_multibyte", "caf\xe9", "636166c3a9", LIMITATION),
    TextFixture(
        "multibyte_only", "\U000065e5\U0000672c\U00008a9e", "e697a5e69cace8aa9e", LIMITATION
    ),
    TextFixture("supplementary_terminal", "ok \U0001f600", "6f6b20f09f9880", LIMITATION),
    TextFixture("supplementary_internal", "\U0001f600 ok", "f09f9880206f6b", OBSERVED),
    TextFixture("vector_ascii", ["alpha", "beta"], ("616c706861", "62657461"), SUPPORTED),
    TextFixture("vector_mixed", ["alpha", "caf\xe9"], ("616c706861", "636166c3a9"), LIMITATION),
    TextFixture(
        "vector_missing",
        ["", None, "caf\xe9 x"],
        ("", None, "636166c3a92078"),
        PYTHON_ONLY,
    ),
)
TEXT_LABELS = tuple(fixture.label for fixture in TEXT_CORPUS)
REFERENCE_LABELS = tuple(f.label for f in TEXT_CORPUS if f.reference != PYTHON_ONLY)
SUPPORTED_LABELS = tuple(f.label for f in TEXT_CORPUS if f.reference == SUPPORTED)

_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TREE = re.compile(r"^[0-9a-f]{40}$")


def expected_bytes(fixture: TextFixture) -> Any:
    """The independently listed bytes (scalar) or list of bytes/None (vector)."""
    if fixture.is_vector:
        assert isinstance(fixture.expected_hex, tuple)
        return [None if h is None else bytes.fromhex(h) for h in fixture.expected_hex]
    assert isinstance(fixture.expected_hex, str)
    return bytes.fromhex(fixture.expected_hex)


def write_form(fixture: TextFixture) -> Any:
    """The value handed to ``write_value``: the text, or a case ``StringSeries``."""
    if fixture.is_vector:
        from tsecon import MIT, Unit

        return bridge.StringSeries(MIT(Unit(), 1), fixture.value)
    return fixture.value


def _corpus_cases(prefix: str, labels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}:{label}" for label in labels)


TEXT_REQUIRED: tuple[str, ...] = (
    "initialize",
    "corpus_bytes_agree",
    "refuse_invalid_policy",
    "refuse_invalid_policy_no_file",
    "refuse_nonascii_default",
    "refuse_surrogate",
    "refuse_nul",
    "refusals_left_no_object",
    *_corpus_cases("write", TEXT_LABELS),
    *_corpus_cases("raw", TEXT_LABELS),
    *_corpus_cases("utf8", TEXT_LABELS),
    *_corpus_cases("bytes", TEXT_LABELS),
    *_corpus_cases("ascii", TEXT_LABELS),
    "write_default_ascii",
    "raw_default_ascii",
    "text_carrier_utf8",
    "text_carrier_utf8_read",
    "missing_written",
    "missing_before_decode",
    "missing_categories_utf8",
    "invalid_utf8_refused",
    "invalid_utf8_bytes_readable",
    "invalid_utf8_ascii_refused",
    "workspace_report_contains",
    "workspace_report_written",
    "workspace_utf8_read",
    "workspace_ascii_read_fails",
    "workspace_raw_fallback",
    *verify_case_ids("cross_process", [f.name for f in TEXT_CORPUS]),
    "finalize",
)


def julia_text_required_cases() -> tuple[str, ...]:
    """Cases the text group must pass when the reference wrapper is configured."""
    return (
        "julia_text_result",
        *_corpus_cases("julia_write", REFERENCE_LABELS),
        *_corpus_cases("julia_read", SUPPORTED_LABELS),
        *_corpus_cases("julia_read_equal", SUPPORTED_LABELS),
        *_corpus_cases("julia_reads_python", SUPPORTED_LABELS),
        *_corpus_cases("julia_reads_python_equal", SUPPORTED_LABELS),
        *_corpus_cases("python_reads_julia_raw", REFERENCE_LABELS),
        *_corpus_cases("python_reads_julia_utf8", REFERENCE_LABELS),
    )


TEXT_JULIA_REQUIRED = julia_text_required_cases()


# -- the group ---------------------------------------------------------------


def group_text(ctx: Context) -> None:
    session, r = ctx.session, ctx.recorder
    r.check("initialize", session.initialize)
    r.permit(session.sentinels.string_nc, session.sentinels.string_na, session.sentinels.string_nd)
    # The hex column and the text column of the corpus must agree; this is
    # the one place the codec is compared with the hand-written bytes.
    r.equal(
        "corpus_bytes_agree",
        [_encoded(fixture) == expected_bytes(fixture) for fixture in TEXT_CORPUS],
        [True] * len(TEXT_CORPUS),
    )
    path = ctx.path("text.db")
    absent = ctx.path("never-created.db")

    r.expect_error(
        "refuse_invalid_policy",
        lambda: bridge.write_value(absent, "X", "text", mode="create", text="latin-1"),
        (ValueError,),
    )
    r.equal("refuse_invalid_policy_no_file", absent.exists(), False)

    with famepy.open_database(path, "create", session=session) as database:
        r.expect_error(
            "refuse_nonascii_default",
            lambda: bridge.write_value(database, "REFUSED_A", "caf\xe9"),
            (TextEncodingError,),
        )
        r.expect_error(
            "refuse_surrogate",
            lambda: bridge.write_value(database, "REFUSED_B", "\ud800", text="utf-8"),
            (TextEncodingError,),
        )
        r.expect_error(
            "refuse_nul",
            lambda: bridge.write_value(database, "REFUSED_C", "a\0b", text="utf-8"),
            (TextEncodingError,),
        )

        def refused_names_absent() -> list[str]:
            present = []
            for name in ("REFUSED_A", "REFUSED_B", "REFUSED_C"):
                try:
                    famepy.quick_info(database, name)
                    present.append(name)
                except famepy.FameError as error:
                    if error.status != HNOOBJ:
                        raise
            return present

        r.expect("refusals_left_no_object", refused_names_absent, [])

        for fixture in TEXT_CORPUS:
            _write_and_read(ctx, database, fixture)

        r.check(
            "write_default_ascii",
            lambda: bridge.write_value(database, "TXT_ASCII_DEFAULT", TEXT_CORPUS[0].value),
        )
        r.expect(
            "raw_default_ascii",
            lambda: _read(database, "TXT_ASCII_DEFAULT").value,
            expected_bytes(TEXT_CORPUS[0]),
        )
        carrier = bridge.Text("{caf\xe9}")
        r.check(
            "text_carrier_utf8",
            lambda: bridge.write_value(database, "TXT_CARRIER", carrier, text="utf-8"),
        )
        r.expect(
            "text_carrier_utf8_read",
            lambda: [
                famepy.quick_info(database, "TXT_CARRIER").kind,
                bridge.read_value(database, "TXT_CARRIER", text="utf-8") == carrier.value,
            ],
            ["string", True],
        )
        _missing_cases(ctx, database)
        _invalid_bytes_cases(ctx, database)
        _workspace_cases(ctx, database)
        database.post()

    manifest = {
        "database": str(path),
        "objects": [_manifest_entry(fixture, session) for fixture in TEXT_CORPUS],
    }
    ctx.verify_in_new_process("cross_process", manifest)

    if ctx.julia is None:
        r.unsupported("julia_text", "no Julia configured")
    else:
        run_julia_text(ctx, path)
    r.check("finalize", session.finalize)


def _encoded(fixture: TextFixture) -> Any:
    if fixture.is_vector:
        return [None if v is None else v.encode("utf-8") for v in fixture.value]
    return fixture.value.encode("utf-8")


def _write_and_read(ctx: Context, database: famepy.Database, fixture: TextFixture) -> None:
    r = ctx.recorder
    label, name = fixture.label, fixture.name
    expected = expected_bytes(fixture)
    value = write_form(fixture)
    if not r.ok(f"write:{label}", lambda: bridge.write_value(database, name, value, text="utf-8")):
        for prefix in ("raw", "utf8", "bytes", "ascii"):
            r.blocked(f"{prefix}:{label}", "object not created")
        return

    def raw_values() -> Any:
        raw = _read(database, name)
        if fixture.is_vector:
            codes = classify_by_sentinel(raw.values, "string", ctx.session.sentinels)
            return [
                None if code != MISSING_NORMAL else item
                for item, code in zip(raw.values, codes, strict=True)
            ]
        return raw.value

    r.expect(f"raw:{label}", raw_values, expected)

    def decoded(policy: str) -> Any:
        read = bridge.read_value(database, name, text=policy)
        return list(read.values) if fixture.is_vector else read

    python_value = fixture.value
    r.expect(f"utf8:{label}", lambda: decoded("utf-8") == python_value, True)
    r.expect(f"bytes:{label}", lambda: decoded("bytes") == expected, True)
    if _is_ascii(fixture):
        r.expect(f"ascii:{label}", lambda: decoded("ascii") == python_value, True)
    else:
        r.expect_error(f"ascii:{label}", lambda: decoded("ascii"), (TextEncodingError,))


def _is_ascii(fixture: TextFixture) -> bool:
    if fixture.is_vector:
        return all(v is None or v.isascii() for v in fixture.value)
    return bool(fixture.value.isascii())


def _first_case() -> Any:
    from tsecon import MIT, Unit

    return MIT(Unit(), 1)


def _missing_cases(ctx: Context, database: famepy.Database) -> None:
    """Missing observations are classified by their sentinel bytes, never decoded."""
    r = ctx.recorder
    sentinels = ctx.session.sentinels
    series = bridge.StringSeries(_first_case(), ["caf\xe9", None, "x"])
    r.check(
        "missing_written",
        lambda: bridge.write_value(database, "TXT_MISSING", series, text="utf-8"),
    )

    def read_missing() -> Any:
        raw = _read(database, "TXT_MISSING")
        codes = classify_by_sentinel(raw.values, "string", sentinels).tolist()
        back = bridge.read_value(database, "TXT_MISSING", text="utf-8")
        return [codes, list(back.values) == ["caf\xe9", None, "x"]]

    r.expect("missing_before_decode", read_missing, [[0, MISSING_NC, 0], True])

    def categories() -> Any:
        raw = famepy.series(
            "string",
            case_frequency(),
            1,
            [sentinels.string_nc, sentinels.string_na, sentinels.string_nd, b"caf\xc3\xa9"],
        )
        famepy.write_object(database, "TXT_CATEGORIES", raw)
        back = bridge.read_value(database, "TXT_CATEGORIES", text="utf-8")
        strict_failed = False
        try:
            bridge.read_value(database, "TXT_CATEGORIES", text="utf-8", missing="strict")
        except bridge.MissingValueError:
            strict_failed = True
        return [list(back.values) == [None, None, None, "caf\xe9"], strict_failed]

    r.expect("missing_categories_utf8", categories, [True, True])


def _invalid_bytes_cases(ctx: Context, database: famepy.Database) -> None:
    """Stored bytes that are not UTF-8 are refused by the decoder, never replaced."""
    r = ctx.recorder
    invalid = b"caf\xe9"  # Latin-1 bytes; not UTF-8, not ASCII
    r.permit(invalid)
    famepy.write_object(database, "TXT_INVALID", famepy.scalar("string", invalid))
    r.expect_error(
        "invalid_utf8_refused",
        lambda: bridge.read_value(database, "TXT_INVALID", text="utf-8"),
        (TextEncodingError,),
    )
    r.expect(
        "invalid_utf8_bytes_readable",
        lambda: bridge.read_value(database, "TXT_INVALID", text="bytes"),
        invalid,
    )
    r.expect_error(
        "invalid_utf8_ascii_refused",
        lambda: bridge.read_value(database, "TXT_INVALID", text="ascii"),
        (TextEncodingError,),
    )


def _workspace_cases(ctx: Context, database: famepy.Database) -> None:
    """Contained writes isolate the value that cannot be encoded; reads decode per policy."""
    r = ctx.recorder
    data = {"ws_ok": "caf\xe9 au lait", "ws_bad": "\ud800", "ws_plain": "plain"}

    def contained() -> Any:
        report = bridge.write_workspace_report(database, data, text="utf-8")
        return [
            sorted(report.written),
            [(f.name, f.error_type) for f in report.failures],
            report.complete,
        ]

    r.expect(
        "workspace_report_contains",
        contained,
        [["ws_ok", "ws_plain"], [("ws_bad", "TextEncodingError")], False],
    )
    r.expect(
        "workspace_report_written",
        lambda: [
            _read(database, "WS_OK").value,
            _read(database, "WS_PLAIN").value,
        ],
        [b"caf\xc3\xa9 au lait", b"plain"],
    )
    r.expect(
        "workspace_utf8_read",
        lambda: (
            dict(bridge.read_workspace(database, "ws_ok", "ws_plain", text="utf-8"))
            == {"ws_ok": "caf\xe9 au lait", "ws_plain": "plain"}
        ),
        True,
    )
    r.expect_error(
        "workspace_ascii_read_fails",
        lambda: bridge.read_workspace(database, "ws_ok", "ws_plain"),
        (TextEncodingError,),
    )

    def fallback() -> Any:
        report = bridge.read_workspace_report(database, "ws_ok", "ws_plain", raw_fallback=True)
        return [
            list(report.raw),
            [f.name for f in report.failures],
            report.workspace["ws_ok"].value,
            report.workspace["ws_plain"],
        ]

    r.expect("workspace_raw_fallback", fallback, [["WS_OK"], [], b"caf\xc3\xa9 au lait", "plain"])


def _manifest_entry(fixture: TextFixture, session: Any) -> dict[str, Any]:
    expected = expected_bytes(fixture)
    if fixture.is_vector:
        sentinel = session.sentinels.string_nc
        values = [sentinel if v is None else v for v in expected]
        return manifest_object(
            fixture.name,
            "string",
            values,
            class_name="series",
            type_code=value_type_code("string"),
            frequency=case_frequency(),
            first_index=1,
        )
    return manifest_object(
        fixture.name,
        "string",
        [expected],
        class_name="scalar",
        type_code=value_type_code("string"),
    )


# -- the reference wrapper on the same corpus --------------------------------


def _julia_literal(value: str) -> str:
    """A Julia string literal spelled with ASCII escapes only."""
    out = []
    for ch in value:
        code = ord(ch)
        if ch == '"' or ch == "\\" or ch == "$":
            out.append("\\" + ch)
        elif 32 <= code < 127:
            out.append(ch)
        else:
            out.append(f"\\U{code:08x}")
    return '"' + "".join(out) + '"'


def _julia_value(fixture: TextFixture) -> str:
    if fixture.is_vector:
        return "[" + ", ".join(_julia_literal(v) for v in fixture.value) + "]"
    return _julia_literal(fixture.value)


_PREAMBLE = r"""
using Pkg
using FAME

function tree_hash()
    for (uuid, dep) in Pkg.dependencies()
        if dep.name == "FAME"
            return string(dep.tree_hash)
        end
    end
    return "unknown"
end

json_string(s::AbstractString) = "\"" * replace(replace(s, "\\" => "\\\\"), "\"" => "\\\"") * "\""
json_value(v::AbstractString) = json_string(v)
json_value(v::Bool) = v ? "true" : "false"
json_value(v::Integer) = string(v)
json_object(d::Dict) = "{" * join([json_string(k) * ":" * json_value(v) for (k, v) in d], ",") * "}"

python_path = ARGS[1]
julia_path = ARGS[2]
result_path = ARGS[3]
token = ARGS[4]
result = Dict{String,Any}("fame_tree_hash" => tree_hash(), "group" => "text", "token" => token)

classname(e) = String(nameof(typeof(e)))
allvalid(v::AbstractString) = isvalid(v)
allvalid(v::AbstractVector) = all(isvalid, v)
units(v::AbstractString) = ncodeunits(v)
units(v::AbstractVector) = sum(ncodeunits, v; init=0)
payload(obj) = obj.data isa AbstractVector ? obj.data : obj.data[]

function record_read!(result, prefix, label, db, name, expected)
    try
        obj = FAME.quick_info(db, name)
        FAME.do_read!(obj, db)
        got = payload(obj)
        result[prefix * ":" * label] = "ok"
        result[prefix * "_valid:" * label] = allvalid(got)
        result[prefix * "_units:" * label] = units(got)
        result[prefix * "_equal:" * label] = got == expected
    catch e
        result[prefix * ":" * label] = classname(e)
    end
end
"""

_EPILOGUE = r"""
result["complete"] = true
part = result_path * ".part"
open(part, "w") do io
    print(io, json_object(result))
end
mv(part, result_path; force=true)
"""


def build_text_script() -> str:
    """The Julia worker script for the corpus; ASCII only, no paths inside."""
    lines = [_PREAMBLE, "corpus = ["]
    for fixture in TEXT_CORPUS:
        if fixture.reference == PYTHON_ONLY:
            continue
        lines.append(f'    ("{fixture.label}", "{fixture.name}", {_julia_value(fixture)}),')
    lines.append("]")
    lines.append(
        r"""
db = FAME.opendb(julia_path, :create)
try
    for (label, name, value) in corpus
        try
            FAME.do_write(FAME.refame(Symbol(name), value), db)
            result["write:" * label] = "ok"
        catch e
            result["write:" * label] = classname(e)
        end
    end
    FAME.postdb(db)
finally
    FAME.closedb!(db)
end

db = FAME.opendb(julia_path, :readonly)
try
    for (label, name, value) in corpus
        record_read!(result, "read", label, db, name, value)
    end
finally
    FAME.closedb!(db)
end

db = FAME.opendb(python_path, :readonly)
try
    for (label, name, value) in corpus
        record_read!(result, "python", label, db, name, value)
    end
finally
    FAME.closedb!(db)
end
"""
    )
    lines.append(_EPILOGUE)
    return chr(10).join(lines)


TEXT_SCRIPT = build_text_script()


def _outcome(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and _CLASS.fullmatch(value) else None


def _flag(payload: dict[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _count(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def run_julia_text(ctx: Context, python_path: Path) -> None:
    """Run the reference wrapper on the corpus and adopt its predicates."""
    assert ctx.julia is not None
    r = ctx.recorder
    script = ctx.path("text.jl")
    script.write_text(TEXT_SCRIPT, encoding="ascii")
    julia_path = ctx.path("julia_text.db")
    tokens = reserve_result(ctx.scratch, "julia_text")
    command = [
        ctx.julia["executable"],
        f"--project={ctx.julia['project']}",
        "--startup-file=no",
        str(script),
        str(python_path),
        str(julia_path),
        tokens["result"],
        tokens["token"],
    ]
    try:
        result = run_child(
            command, "", max(ctx.timeout, 300), nested=True, output_path=Path(tokens["log"])
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        r.add(
            Case(
                "julia_text_result",
                "fail",
                error_type=type(error).__name__,
                note="Julia process did not complete",
            )
        )
        return
    if result.returncode != 0:
        r.add(
            Case(
                "julia_text_result",
                "fail",
                errno=result.returncode,
                note="Julia exited with a nonzero status; raw output kept local",
            )
        )
        return
    payload, kind = read_result(Path(tokens["result"]), tokens["token"])
    if payload is not None and payload.get("group") != "text":
        payload, kind = None, "wrong_group"
    if payload is None:
        r.add(
            Case(
                "julia_text_result",
                "fail",
                note="Julia " + (kind or "invalid_result").replace("_", " "),
            )
        )
        return
    r.add(Case("julia_text_result", "pass"))
    tree = payload.get("fame_tree_hash")
    tree = tree if isinstance(tree, str) and _TREE.match(tree) else None
    from ._julia import PINNED_FAME_TREE_HASH

    pinned = tree == PINNED_FAME_TREE_HASH
    r.add(
        Case(
            "julia_text_tree_pinned",
            "pass" if pinned else "unsupported",
            expected=PINNED_FAME_TREE_HASH,
            actual=tree,
            note=None if pinned else "FAME.jl tree differs from the pinned reference; qualified",
        )
    )
    qualification = None if pinned else "compared against an unpinned FAME.jl tree"
    adopt_julia_text(ctx, payload, julia_path, qualification)


def adopt_julia_text(
    ctx: Context, payload: dict[str, Any], julia_path: Path, qualification: str | None
) -> None:
    """Turn the script's per-label outcomes into cases and read its database."""
    r = ctx.recorder
    for fixture in TEXT_CORPUS:
        if fixture.reference == PYTHON_ONLY:
            continue
        label = fixture.label
        r.equal(f"julia_write:{label}", _outcome(payload, f"write:{label}"), "ok")
        for prefix, case in (("read", "julia_read"), ("python", "julia_reads_python")):
            outcome = _outcome(payload, f"{prefix}:{label}")
            record = [
                outcome,
                _flag(payload, f"{prefix}_valid:{label}"),
                _count(payload, f"{prefix}_units:{label}"),
                _flag(payload, f"{prefix}_equal:{label}"),
            ]
            if fixture.reference == SUPPORTED:
                r.equal(f"{case}:{label}", outcome, "ok", note=qualification)
                r.equal(
                    f"{case}_equal:{label}",
                    record[1:],
                    [True, _units(fixture), True],
                    note=qualification,
                )
            else:
                note = (
                    "reference limitation: read slicing on a byte length"
                    if fixture.reference == LIMITATION
                    else "reference outcome recorded, not required"
                )
                r.fact(f"{case}:{label}", record, note=note)
    # Python reads what the reference wrote: exact bytes, then the decoded text.
    with famepy.open_database(julia_path, "readonly", session=ctx.session) as database:
        for fixture in TEXT_CORPUS:
            if fixture.reference == PYTHON_ONLY:
                continue
            label, name = fixture.label, fixture.name
            expected = expected_bytes(fixture)

            def raw_read(name: str = name, vector: bool = fixture.is_vector) -> Any:
                raw = _read(database, name)
                return list(raw.values) if vector else raw.value

            r.expect(f"python_reads_julia_raw:{label}", raw_read, expected, note=qualification)

            def utf8_read(name: str = name, vector: bool = fixture.is_vector) -> Any:
                read = bridge.read_value(database, name, text="utf-8")
                return list(read.values) if vector else read

            def utf8_equal(utf8_read: Any = utf8_read, value: Any = fixture.value) -> bool:
                return bool(utf8_read() == value)

            r.expect(f"python_reads_julia_utf8:{label}", utf8_equal, True, note=qualification)


def _units(fixture: TextFixture) -> int:
    encoded = _encoded(fixture)
    if fixture.is_vector:
        return sum(len(v) for v in encoded if v is not None)
    return len(encoded)


__all__ = [
    "TEXT_CORPUS",
    "TEXT_JULIA_REQUIRED",
    "TEXT_LABELS",
    "TEXT_REQUIRED",
    "TEXT_SCRIPT",
    "TextFixture",
    "build_text_script",
    "expected_bytes",
    "group_text",
    "run_julia_text",
    "write_form",
]
