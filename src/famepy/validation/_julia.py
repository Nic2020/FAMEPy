# SPDX-License-Identifier: MIT
"""Optional Julia differential checks for the bridge group.

Requires a Julia executable and a project containing FAME.jl and
TimeSeriesEcon. The script below is written to the scratch directory and run
as a worker like the Python children: a fresh result path and token are
reserved per launch, the script writes its JSON document atomically with the
token and a completion marker, its standard streams go to a local log that
is never parsed, and a missing, stale, partial or oversized result fails the
case. The process tree is terminated on timeout. Numeric values
are exchanged as IEEE bit patterns, not decimal spellings. The FAME.jl tree
identity is compared with the pinned reference; a mismatch qualifies the
comparison (reported as ``unsupported``) rather than passing silently. The
script does not modify any Julia project.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

import famepy
from famepy import bridge

from ._process import read_result, reserve_result, run_child
from ._report import Case

if TYPE_CHECKING:
    from ._groups import Context

# Tree hash of the pinned FAME.jl reference (see docs/capabilities.md).
PINNED_FAME_TREE_HASH = "a5b58b221aa3b47e4f2cd52f081f41c3653881ab"
_TREE = re.compile(r"^[0-9a-f]{40}$")
_BITS = re.compile(r"^[0-9a-f]{16}$")

SCRIPT = r"""
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
json_string(s::AbstractString) = "\"" * replace(replace(s, "\\" => "\\\\"), "\"" => "\\\"") * "\""
json_value(v::AbstractString) = json_string(v)
json_value(v::Bool) = v ? "true" : "false"
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

# Write a monthly series and scalar for Python to read back.
writefame(julia_path, Workspace(; jts=TSeries(2021M1, [1.0, NaN, 3.0]), jsc=7.5); mode=:create)
result["complete"] = true
part = result_path * ".part"
open(part, "w") do io
    print(io, json_object(result))
end
mv(part, result_path; force=true)
"""


def _expected_bits(values: np.ndarray) -> list[str]:
    return ["nan" if np.isnan(v) else np.array(v, dtype=">f8").tobytes().hex() for v in values]


def run_julia_differential(ctx: Context, python_path: Path) -> None:
    from ._groups import BRIDGE_VALUES

    assert ctx.julia is not None
    r = ctx.recorder
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

    def read_julia_written() -> None:
        from tsecon import mm

        back = bridge.read_tseries(julia_path, "jts")
        r.equal("python_reads_julia_firstdate", int(back.firstdate), int(mm(2021, 1)))
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

    r.check("python_reads_julia", read_julia_written, note=qualification)
