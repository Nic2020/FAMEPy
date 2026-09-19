# SPDX-License-Identifier: MIT
"""Same-host FAME.jl timings on the comparable scenarios (optional, reported apart).

The Julia script mirrors the comparable Python scenarios only: the same
64-bit LCG values, shapes and missing densities for ``many_small``,
``few_large`` and ``missing_density``, timing the reference's
``writefame``/``readfame`` of a whole workspace, which is the boundary of
the Python ``write_workspace``/``read_workspace`` phases. The date and
string scenarios are not mirrored because their Python fixtures contain
missing observations the reference script would have to replace. The
script also computes an FNV-1a digest of every fixture's exact bytes;
the coordinator compares those digests with the Python side's and treats
a scenario as comparable only when they agree.

The script runs under the validation runner's worker protocol (reserved
result file and token, local log, process-tree deadline) and its result
is accepted only when it carries exactly the expected fields.
"""

from __future__ import annotations

import math
import re
import statistics
import subprocess
from pathlib import Path
from typing import Any

from famepy.validation._julia import PINNED_FAME_TREE_HASH
from famepy.validation._process import read_result, reserve_result, run_child

_TREE = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{16}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]{0,32}$")

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

json_string(s::AbstractString) = "\"" * replace(replace(s, "\\" => "\\\\"), "\"" => "\\\"") * "\""
json_value(v::AbstractString) = json_string(v)
json_value(v::Bool) = v ? "true" : "false"
json_value(v::Real) = string(Float64(v))
json_value(v::AbstractVector) = "[" * join(map(json_value, v), ",") * "]"
function json_value(d::AbstractDict)
    return "{" * join([json_string(string(k)) * ":" * json_value(v) for (k, v) in d], ",") * "}"
end

const MULT = UInt64(6364136223846793005)
const INC = UInt64(1442695040888963407)

function lcg_values(count::Int, seed::Int)
    out = Vector{Float64}(undef, count)
    state = UInt64(seed)
    for i in 1:count
        state = MULT * state + INC
        out[i] = Float64(state >> 11) / 9007199254740992.0
    end
    return out
end

function with_missing(values, density, seed)
    picks = lcg_values(length(values), seed)
    out = copy(values)
    out[picks .< density] .= NaN
    return out
end

function fnv1a(h::UInt64, values::Vector{Float64})
    for b in reinterpret(UInt8, values)
        h = xor(h, UInt64(b)) * UInt64(0x100000001b3)
    end
    return h
end

const FNV_OFFSET = UInt64(0xcbf29ce484222325)
hex16(h::UInt64) = string(h, base=16, pad=16)

scratch = ARGS[1]
result_path = ARGS[2]
token = ARGS[3]
reps = parse(Int, ARGS[4])
many_count = parse(Int, ARGS[5])
many_length = parse(Int, ARGS[6])
large_count = parse(Int, ARGS[7])
large_length = parse(Int, ARGS[8])
missing_length = parse(Int, ARGS[9])

function batch(count, len, first, seed)
    w = Workspace()
    h = FNV_OFFSET
    for i in 0:count-1
        values = lcg_values(len, seed + i)
        h = fnv1a(h, values)
        w[Symbol("s" * lpad(string(i), 5, "0"))] = TSeries(first, values)
    end
    return w, hex16(h)
end

function missing_batch(len)
    values = lcg_values(len, 17)
    w = Workspace()
    h = FNV_OFFSET
    for density in (0.0, 0.1, 0.5, 0.9)
        data = with_missing(values, density, 19)
        h = fnv1a(h, data)
        w[Symbol("d" * lpad(string(Int(density * 100)), 2, "0"))] = TSeries(2000M1, data)
    end
    return w, hex16(h)
end

timed(f) = @elapsed f()

phases = Dict{String,Dict{String,Vector{Float64}}}()
function record(scn, phase, seconds)
    bucket = get!(phases, scn, Dict{String,Vector{Float64}}())
    push!(get!(bucket, phase, Float64[]), seconds)
end

hashes = Dict{String,String}()
fixtures = Dict{String,Any}()
fixtures["many_small"], hashes["many_small"] = batch(many_count, many_length, 2000M1, 1)
first_day = daily("2000-01-03")
fixtures["few_large"], hashes["few_large"] = batch(large_count, large_length, first_day, 7)
fixtures["missing_density"], hashes["missing_density"] = missing_batch(missing_length)

for rep in 0:reps
    for scn in ("many_small", "few_large", "missing_density")
        w = fixtures[scn]
        path = joinpath(scratch, "julia-" * scn * "-" * string(rep) * ".db")
        tw = timed(() -> writefame(path, w; mode=:create))
        tr = timed(() -> readfame(path))
        if rep > 0
            record(scn, "write_workspace", tw)
            record(scn, "read_workspace", tr)
        end
    end
end

result = Dict{String,Any}(
    "group" => "benchmark",
    "token" => token,
    "fame_tree_hash" => tree_hash(),
    "julia_version" => string(VERSION),
    "phases" => phases,
    "fixture_hashes" => hashes,
    "complete" => true,
)
part = result_path * ".part"
open(part, "w") do io
    print(io, json_value(result))
end
mv(part, result_path; force=true)
"""

_SCENARIOS = ("many_small", "few_large", "missing_density")
_PHASES = ("write_workspace", "read_workspace")


def _duration(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _numbers(value: Any, count: int) -> bool:
    return isinstance(value, list) and len(value) == count and all(_duration(v) for v in value)


def validate_julia_result(payload: Any, repetitions: int) -> dict[str, Any] | None:
    """The accepted, reduced Julia result, or None when any field is not as expected."""
    if not isinstance(payload, dict) or set(payload) != {
        "group",
        "token",
        "complete",
        "fame_tree_hash",
        "julia_version",
        "phases",
        "fixture_hashes",
    }:
        return None
    if payload["group"] != "benchmark":
        return None
    tree = payload["fame_tree_hash"]
    version = payload["julia_version"]
    phases = payload["phases"]
    hashes = payload["fixture_hashes"]
    if not isinstance(tree, str) or not (tree == "unknown" or _TREE.fullmatch(tree)):
        return None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return None
    if not isinstance(phases, dict) or set(phases) != set(_SCENARIOS):
        return None
    if not isinstance(hashes, dict) or set(hashes) != set(_SCENARIOS):
        return None
    if not all(isinstance(h, str) and _HASH.fullmatch(h) for h in hashes.values()):
        return None
    reduced: dict[str, Any] = {}
    for scenario, bucket in phases.items():
        if not isinstance(bucket, dict) or set(bucket) != set(_PHASES):
            return None
        reduced[scenario] = {}
        for phase, samples in bucket.items():
            if not _numbers(samples, repetitions):
                return None
            reduced[scenario][phase] = {
                "seconds": {
                    "samples": [round(float(s), 6) for s in samples],
                    "min": round(min(samples), 6),
                    "median": round(statistics.median(samples), 6),
                    "max": round(max(samples), 6),
                }
            }
    return {
        "fame_tree_hash": tree,
        "fame_tree_pinned": tree == PINNED_FAME_TREE_HASH,
        "julia_version": version,
        "phases": reduced,
        "fixture_hashes": dict(hashes),
    }


def run_julia_benchmark(
    julia: dict[str, str], scratch: Path, scale: dict[str, int], repetitions: int, timeout: float
) -> dict[str, Any]:
    directory = scratch / "julia"
    directory.mkdir(exist_ok=True)
    script = directory / "benchmark.jl"
    script.write_text(SCRIPT, encoding="ascii")
    tokens = reserve_result(directory, "julia")
    command = [
        julia["executable"],
        f"--project={julia['project']}",
        "--startup-file=no",
        str(script),
        str(directory),
        tokens["result"],
        tokens["token"],
        str(repetitions),
        str(scale["many_small_count"]),
        str(scale["many_small_length"]),
        str(scale["few_large_count"]),
        str(scale["few_large_length"]),
        str(scale["missing_length"]),
    ]
    try:
        completed = run_child(command, "", timeout, output_path=Path(tokens["log"]))
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except OSError as error:
        return {"error": "start_failed", "errno": error.errno}
    if completed.returncode != 0:
        return {"error": "julia_failed", "exit_code": completed.returncode}
    payload, kind = read_result(Path(tokens["result"]), tokens["token"])
    if payload is None:
        return {"error": kind or "invalid_result"}
    accepted = validate_julia_result(payload, repetitions)
    if accepted is None:
        return {"error": "invalid_result"}
    accepted["note"] = (
        "reference timings of writefame/readfame of the same workspaces on the same host; "
        "fixture digests are compared with the Python side before any pair is called comparable"
    )
    return accepted


__all__ = ["SCRIPT", "run_julia_benchmark", "validate_julia_result"]
