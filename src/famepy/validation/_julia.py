# SPDX-License-Identifier: MIT
"""Optional Julia differential checks for the bridge group.

Requires a Julia executable and a project containing FAME.jl and
TimeSeriesEcon. The script below is written to the scratch directory and run
as a worker like the Python children: a fresh result path and token are
reserved per launch, the script writes its JSON document atomically with the
token and a completion marker, its standard streams go to a local log that
is never parsed, and a missing, stale, partial or oversized result fails the
case. The process tree is terminated on timeout. Numeric values are
exchanged as IEEE bit patterns and moments as their integer values (the two
libraries share the moment encoding), never as printed dates. The FAME.jl
tree identity is compared with the pinned reference; a mismatch qualifies
the comparison (reported as ``unsupported``) rather than passing silently.
The script does not modify any Julia project.

Coverage: the monthly precision cases of the first release, one precision
series per calendar frequency anchor in both directions (Python writes,
Julia echoes the moment it read and writes its own independently
constructed moment; Python reads that back), and the value kinds the
reference converts (Boolean, string, namelist, numeric, integer, date,
Boolean series, date series, string vector, reference-empty series).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

import famepy
from famepy import bridge
from famepy._constants import FREQUENCIES

from ._process import read_result, reserve_result, run_child
from ._report import Case

if TYPE_CHECKING:
    from ._groups import Context

# Tree hash of the pinned FAME.jl reference (see docs/capabilities.md).
PINNED_FAME_TREE_HASH = "a5b58b221aa3b47e4f2cd52f081f41c3653881ab"
_TREE = re.compile(r"^[0-9a-f]{40}$")
_BITS = re.compile(r"^[0-9a-f]{16}$")
_BITS32 = re.compile(r"^[0-9a-f]{8}$")
_INT = re.compile(r"^-?[0-9]{1,19}$")
_TEXT = re.compile(r"^[A-Za-z0-9 _,{}]{0,64}$")

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

JULIA_KINDS: dict[str, str] = {
    "jkw_bool": "true",
    "jkw_str": '"Hello"',
    "jkw_nl": '"{A,B}"',
    "jkw_num": "1.5f0",
    "jkw_int": "3",
    "jkw_date": "2021Q3",
    "jkw_boolts": "TSeries(2020Q1, [true, false])",
    "jkw_datets": "TSeries(2020Q1, [2021Y, 2022Y])",
    "jkw_vec": '["x", "y"]',
    "jkw_empty": "TSeries(1995Q1)",
}


def frequency_specs() -> list[tuple[str, int, str]]:
    """``(label, code, julia constructor of the anchor moment)`` per calendar frequency."""
    specs: list[tuple[str, int, str]] = []
    specs.append(("daily", FREQUENCIES["daily"], 'daily("2020-02-28")'))
    specs.append(("business", FREQUENCIES["business"], 'bdaily("2020-02-28")'))
    for day, name in enumerate(_DAYS, start=1):
        specs.append(
            (f"weekly_{name}", FREQUENCIES[f"weekly_{name}"], f'weekly("2020-02-28", {day})')
        )
    specs.append(("monthly", FREQUENCIES["monthly"], "MIT{Monthly}(2020, 1)"))
    for anchor, name in enumerate(("october", "november", "december"), start=1):
        specs.append(
            (
                f"quarterly_{name}",
                FREQUENCIES[f"quarterly_{name}"],
                f"MIT{{Quarterly{{{anchor}}}}}(2020, 1)",
            )
        )
    for anchor, name in enumerate(_MONTHS[6:], start=1):
        specs.append(
            (
                f"semiannual_{name}",
                FREQUENCIES[f"semiannual_{name}"],
                f"MIT{{HalfYearly{{{anchor}}}}}(2020, 1)",
            )
        )
    for month, name in enumerate(_MONTHS, start=1):
        specs.append(
            (f"annual_{name}", FREQUENCIES[f"annual_{name}"], f"MIT{{Yearly{{{month}}}}}(2020, 1)")
        )
    return specs


_PREAMBLE = r"""
using Pkg
using FAME, TimeSeriesEcon

function tree_hash()
    for (uuid, dep) in Pkg.dependencies()
        if dep.name == "FAME"
            return string(dep.tree_hash)
        end
    end
    return "unknown"
end

bits(v::Float64) = isnan(v) ? "nan" : string(reinterpret(UInt64, v), base=16, pad=16)
bits32(v::Float32) = string(reinterpret(UInt32, v), base=16, pad=8)
json_string(s::AbstractString) = "\"" * replace(replace(s, "\\" => "\\\\"), "\"" => "\\\"") * "\""
json_value(v::AbstractString) = json_string(v)
json_value(v::Bool) = v ? "true" : "false"
json_value(v::Integer) = json_string(string(v))
json_value(v::AbstractVector) = "[" * join(map(json_value, v), ",") * "]"
json_object(d::Dict) = "{" * join([json_string(k) * ":" * json_value(v) for (k, v) in d], ",") * "}"

python_path = ARGS[1]
julia_path = ARGS[2]
result_path = ARGS[3]
token = ARGS[4]
result = Dict{String,Any}("fame_tree_hash" => tree_hash(), "group" => "julia", "token" => token)

# Read what Python wrote and echo the values as bit patterns.
w = readfame(python_path, "ts", "sc")
result["python_ts_first"] = string(firstdate(w.ts))
result["python_ts_bits"] = [bits(Float64(v)) for v in w.ts.values]
result["python_sc_bits"] = bits(Float64(w.sc))
f = readfame(python_path, "jf?", "jk?")
"""

_EPILOGUE = r"""
writefame(julia_path, jw; mode=:create)
result["complete"] = true
part = result_path * ".part"
open(part, "w") do io
    print(io, json_object(result))
end
mv(part, result_path; force=true)
"""


def build_script() -> str:
    """The Julia worker script; ASCII only, no paths or private values inside."""
    lines = [_PREAMBLE]
    for label, _code, _ctor in frequency_specs():
        lines.append(f'result["pf_{label}"] = Int(firstdate(f[:jf_{label}]))')
        lines.append(
            f'result["pf_{label}_freq"] = string(FAME._freq_to_fame(frequencyof(f[:jf_{label}])))'
        )
        lines.append(f'result["pf_{label}_eltype"] = string(eltype(f[:jf_{label}]))')
        lines.append(
            f'result["pf_{label}_bits"] = [bits(Float64(v)) for v in f[:jf_{label}].values]'
        )
    lines.extend(
        [
            'result["pk_bool"] = f.jk_bool',
            'result["pk_str"] = f.jk_str',
            'result["pk_nl"] = f.jk_nl',
            'result["pk_num"] = bits32(f.jk_num)',
            'result["pk_date"] = Int(f.jk_date)',
            "",
            "# Write independently constructed moments and kinds for Python to read back.",
            "jw = Workspace()",
            "jw[:jts] = TSeries(2021M1, [1.0, NaN, 3.0])",
            "jw[:jsc] = 7.5",
        ]
    )
    for label, _code, ctor in frequency_specs():
        lines.append(f"jw[:jw_{label}] = TSeries({ctor}, [1.0, NaN, 2.5])")
    for name, expression in JULIA_KINDS.items():
        lines.append(f"jw[:{name}] = {expression}")
    lines.append(_EPILOGUE)
    return chr(10).join(lines)


SCRIPT = build_script()


def julia_required_cases() -> tuple[str, ...]:
    """Cases the bridge group must pass when a Julia differential is configured.

    The tree identity case may legitimately be ``unsupported`` (an unpinned
    tree qualifies the comparison), so it is not required; every comparison
    is.
    """
    labels = [label for label, _code, _ctor in frequency_specs()]
    return (
        "julia_fixtures_written",
        "julia_reads_python_series",
        "julia_reads_python_scalar",
        "julia_reads_python_firstdate",
        *[f"julia_reads_python_frequency:{label}" for label in labels],
        "julia_reads_python_kinds",
        "python_reads_julia",
        "python_reads_julia_firstdate",
        "python_reads_julia_values",
        "python_reads_julia_scalar",
        "julia_nan_is_nc",
        *[f"python_reads_julia_frequency:{label}" for label in labels],
        *[f"python_reads_julia_kind:{name}" for name in JULIA_KINDS],
    )


JULIA_REQUIRED = julia_required_cases()


def _expected_bits(values: np.ndarray) -> list[str]:
    return ["nan" if np.isnan(v) else np.array(v, dtype=">f8").tobytes().hex() for v in values]


def _anchor(code: int) -> Any:
    from ._bridge_groups import anchor_moment

    return anchor_moment(code)


def write_python_fixtures(ctx: Context, python_path: Path) -> None:
    """Objects Julia reads back: one series per frequency anchor and the value kinds."""
    import tsecon as ts

    workspace = ts.Workspace()
    for label, code, _ctor in frequency_specs():
        workspace[f"jf_{label}"] = ts.TSeries(_anchor(code), np.array([1.0, np.nan, 2.5]))
    workspace["jk_bool"] = True
    workspace["jk_str"] = "Hello"
    workspace["jk_nl"] = "{A,B}"
    workspace["jk_num"] = np.float32(1.5)
    workspace["jk_date"] = ts.qq(2021, 3)
    bridge.write_workspace(python_path, workspace, mode="update")


def _int_field(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, str) and _INT.match(value):
        return int(value)
    return None


def _text_field(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and _TEXT.match(value) else None


def run_julia_differential(ctx: Context, python_path: Path) -> None:
    import tsecon as ts

    from ._groups import BRIDGE_VALUES

    assert ctx.julia is not None
    r = ctx.recorder
    if not r.ok("julia_fixtures_written", lambda: write_python_fixtures(ctx, python_path)):
        return
    script = ctx.path("differential.jl")
    script.write_text(SCRIPT, encoding="ascii")
    julia_path = ctx.path("julia_written.db")
    tokens = reserve_result(ctx.scratch, "julia")
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
                "julia_run",
                "fail",
                error_type=type(error).__name__,
                note="Julia process did not complete",
            )
        )
        return
    if result.returncode != 0:
        r.add(
            Case(
                "julia_run",
                "fail",
                errno=result.returncode,
                note="Julia exited with a nonzero status; raw output kept local",
            )
        )
        return
    payload, kind = read_result(Path(tokens["result"]), tokens["token"])
    if payload is not None and payload.get("group") != "julia":
        payload, kind = None, "wrong_group"
    if payload is None:
        r.add(
            Case(
                "julia_run",
                "fail",
                note="Julia " + (kind or "invalid_result").replace("_", " "),
            )
        )
        return
    tree = payload.get("fame_tree_hash")
    tree = tree if isinstance(tree, str) and _TREE.match(tree) else None
    pinned = tree == PINNED_FAME_TREE_HASH
    r.add(
        Case(
            "julia_fame_tree_pinned",
            "pass" if pinned else "unsupported",
            expected=PINNED_FAME_TREE_HASH,
            actual=tree,
            note=None if pinned else "FAME.jl tree differs from the pinned reference; qualified",
        )
    )
    qualification = None if pinned else "compared against an unpinned FAME.jl tree"
    series_bits = payload.get("python_ts_bits")
    if not (
        isinstance(series_bits, list)
        and all(isinstance(b, str) and (b == "nan" or _BITS.match(b)) for b in series_bits)
    ):
        series_bits = None
    scalar_bits = payload.get("python_sc_bits")
    scalar_bits = scalar_bits if isinstance(scalar_bits, str) and _BITS.match(scalar_bits) else None
    first = payload.get("python_ts_first")
    first = first if isinstance(first, str) and re.match(r"^[0-9]{4}M[0-9]{1,2}$", first) else None
    r.equal(
        "julia_reads_python_series", series_bits, _expected_bits(BRIDGE_VALUES), note=qualification
    )
    r.equal(
        "julia_reads_python_scalar",
        scalar_bits,
        _expected_bits(np.array([2.5]))[0],
        note=qualification,
    )
    r.equal("julia_reads_python_firstdate", first, "2020M1", note=qualification)

    # What Julia read from Python's per-anchor series: the moment integer, the
    # library frequency name of its frequency, the element type and the bits.
    for label, code, _ctor in frequency_specs():
        bits_field = payload.get(f"pf_{label}_bits")
        if not (
            isinstance(bits_field, list)
            and all(isinstance(b, str) and (b == "nan" or _BITS.match(b)) for b in bits_field)
        ):
            bits_field = None
        r.equal(
            f"julia_reads_python_frequency:{label}",
            [
                _int_field(payload, f"pf_{label}"),
                _text_field(payload, f"pf_{label}_freq"),
                _text_field(payload, f"pf_{label}_eltype"),
                bits_field,
            ],
            [int(_anchor(code)), label, "Float64", _expected_bits(np.array([1.0, np.nan, 2.5]))],
            note=qualification,
        )
    bool_value = payload.get("pk_bool")
    r.equal(
        "julia_reads_python_kinds",
        [
            bool_value if isinstance(bool_value, bool) else None,
            _text_field(payload, "pk_str"),
            _members(_text_field(payload, "pk_nl")),
            payload.get("pk_num")
            if isinstance(payload.get("pk_num"), str) and _BITS32.match(payload["pk_num"])
            else None,
            _int_field(payload, "pk_date"),
        ],
        [True, "Hello", ["A", "B"], np.float32(1.5).tobytes()[::-1].hex(), int(ts.qq(2021, 3))],
        note=qualification,
    )

    def read_julia_written() -> None:
        back = bridge.read_tseries(julia_path, "jts")
        r.equal("python_reads_julia_firstdate", int(back.firstdate), int(ts.mm(2021, 1)))
        r.equal(
            "python_reads_julia_values",
            np.array_equal(back.values, np.array([1.0, np.nan, 3.0]), equal_nan=True),
            True,
        )
        r.equal("python_reads_julia_scalar", bridge.read_scalar(julia_path, "jsc"), 7.5)
        with famepy.open_database(julia_path, session=ctx.session) as database:
            raw: Any = famepy.read_object(database, "jts")
            r.equal(
                "julia_nan_is_nc",
                famepy.classify_by_sentinel(
                    raw.values, "precision", ctx.session.sentinels
                ).tolist(),
                [0, 1, 0],
            )
        written = bridge.read_workspace(julia_path, "jw?", "jkw?", empty="reference")
        for label, code, _ctor in frequency_specs():
            series = written.get(f"jw_{label}")
            r.equal(
                f"python_reads_julia_frequency:{label}",
                None
                if series is None
                else [
                    int(series.firstdate),
                    bridge.fame_frequency(series.frequency),
                    series.values,
                ],
                [int(_anchor(code)), code, np.array([1.0, np.nan, 2.5])],
                note=qualification,
            )
        kinds = {
            "jkw_bool": (written.get("jkw_bool"), True),
            "jkw_str": (written.get("jkw_str"), "Hello"),
            "jkw_nl": (_members_of(written.get("jkw_nl")), ["A", "B"]),
            "jkw_num": (written.get("jkw_num"), np.float32(1.5)),
            # The reference promotes an integer to a float32 numeric scalar.
            "jkw_int": (written.get("jkw_int"), np.float32(3.0)),
            "jkw_date": (_int_or_none(written.get("jkw_date")), int(ts.qq(2021, 3))),
            "jkw_boolts": (
                _tseries_record(written.get("jkw_boolts")),
                [int(ts.qq(2020, 1)), [True, False]],
            ),
            "jkw_datets": (
                _dates_record(written.get("jkw_datets")),
                [int(ts.qq(2020, 1)), [int(ts.yy(2021)), int(ts.yy(2022))]],
            ),
            "jkw_vec": (_strings_record(written.get("jkw_vec")), [1, ["x", "y"]]),
            "jkw_empty": (_tseries_record(written.get("jkw_empty")), [int(ts.qq(1995, 1)), []]),
        }
        for name, (actual, expected) in kinds.items():
            r.equal(f"python_reads_julia_kind:{name}", actual, expected, note=qualification)

    r.check("python_reads_julia", read_julia_written, note=qualification)


def _members(text: str | None) -> list[str] | None:
    if text is None:
        return None
    try:
        return list(bridge.NameList(text).members)
    except ValueError:
        return None


def _members_of(value: Any) -> list[str] | None:
    return list(value.members) if isinstance(value, bridge.NameList) else None


def _int_or_none(value: Any) -> int | None:
    import tsecon as ts

    return int(value) if isinstance(value, ts.MIT) else None


def _tseries_record(value: Any) -> Any:
    import tsecon as ts

    if not isinstance(value, ts.TSeries):
        return None
    return [int(value.firstdate), value.values.tolist()]


def _dates_record(value: Any) -> Any:
    if not isinstance(value, bridge.DateSeries):
        return None
    return [int(value.firstdate), [None if m is None else int(m) for m in value.values]]


def _strings_record(value: Any) -> Any:
    if not isinstance(value, bridge.StringSeries):
        return None
    return [int(value.firstdate), list(value.values)]
