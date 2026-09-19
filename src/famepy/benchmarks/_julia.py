# SPDX-License-Identifier: MIT
"""Same-host FAME.jl timings on the comparable scenarios (optional, reported apart).

The Julia script mirrors the comparable Python scenarios only: the same
64-bit LCG values, shapes, index (monthly from 2000M1 for ``many_small``,
daily from 2000-01-03 for ``few_large`` and ``missing_density``) and
missing densities, timing the reference's ``writefame``/``readfame`` of a
whole workspace, which is the boundary of the Python
``write_workspace``/``read_workspace`` phases. The date and string
scenarios are not mirrored because their Python fixtures contain missing
observations the reference script would have to replace.

The reference's workspace write catches and logs per-object errors instead
of raising, so a timing alone proves nothing. Every ``readfame`` result,
including the untimed warm-up pass, is verified outside the timed regions
against fixtures generated a second time: the exact key set, the series
type, index frequency, first moment and length, the missing positions and
the bitwise values at the finite positions. Any disagreement stops the
script with a bounded failure record (scenario and check label from fixed
sets) and a nonzero exit, so no timing record is accepted. The script also
reports an FNV-1a digest and the index domain of every fixture; the
coordinator compares both with the Python side.

The script runs under the validation runner's worker protocol (reserved
result file and token, local log, process-tree deadline) and its result
is accepted only when it carries exactly the expected fields. A negative
self-check (``run_julia_selfcheck``) reruns the script with each documented
corruption injected into a read-back workspace and requires the matching
check to fail; it is the executable evidence that the verification runs,
and it needs Julia with FAME.jl on the host.
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

SCENARIOS = ("many_small", "few_large", "missing_density")
PHASES = ("write_workspace", "read_workspace")
FREQUENCIES = ("monthly", "daily")
# The checks the Julia verification applies, in order; a failure names one.
CHECKS = ("key_set", "type", "frequency", "start", "length", "missing_positions", "values")
# Corruptions the negative self-check injects into a read-back workspace,
# and the check that must catch each one.
NEGATIVE_CASES: dict[str, str] = {
    "drop_key": "key_set",
    "extra_key": "key_set",
    "not_series": "type",
    "wrong_element_type": "type",
    "wrong_frequency": "frequency",
    "shift_start": "start",
    "truncate": "length",
    "flip_missing": "missing_positions",
    "corrupt_value": "values",
}
VERIFICATION_EXIT = 3

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
json_value(v::Integer) = string(v)
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
negative = length(ARGS) >= 10 ? ARGS[10] : "none"

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

function missing_batch(len, first)
    values = lcg_values(len, 17)
    w = Workspace()
    h = FNV_OFFSET
    for density in (0.0, 0.1, 0.5, 0.9)
        data = with_missing(values, density, 19)
        h = fnv1a(h, data)
        w[Symbol("d" * lpad(string(Int(density * 100)), 2, "0"))] = TSeries(first, data)
    end
    return w, hex16(h)
end

freq_name(::Type{Monthly}) = "monthly"
freq_name(::Type{Daily}) = "daily"
freq_name(::Type{F}) where {F} = lowercase(string(nameof(F)))

function domain(w)
    s = w[first(sort(collect(keys(w))))]
    return Dict{String,Any}(
        "frequency" => freq_name(frequencyof(s)),
        "start" => Int(firstdate(s)),
        "length" => length(s),
    )
end

function build_fixtures()
    fixtures = Dict{String,Any}()
    hashes = Dict{String,String}()
    fixtures["many_small"], hashes["many_small"] = batch(many_count, many_length, 2000M1, 1)
    first_day = daily("2000-01-03")
    fixtures["few_large"], hashes["few_large"] = batch(large_count, large_length, first_day, 7)
    missing = missing_batch(missing_length, first_day)
    fixtures["missing_density"], hashes["missing_density"] = missing
    return fixtures, hashes
end

# Returns "" when the read-back workspace equals the expected one, else the
# label of the first check that fails.
function verify(got, expected)
    got isa Workspace || return "type"
    Set(keys(got)) == Set(keys(expected)) || return "key_set"
    for key in sort(collect(keys(expected)))
        e = expected[key]
        g = got[key]
        g isa TSeries || return "type"
        eltype(g) == eltype(e) || return "type"
        frequencyof(g) == frequencyof(e) || return "frequency"
        firstdate(g) == firstdate(e) || return "start"
        length(g) == length(e) || return "length"
        gv = g.values
        ev = e.values
        gn = isnan.(gv)
        en = isnan.(ev)
        gn == en || return "missing_positions"
        keep = .!en
        reinterpret(UInt64, gv[keep]) == reinterpret(UInt64, ev[keep]) || return "values"
    end
    return ""
end

function corrupt(w, case)
    key = first(sort(collect(keys(w))))
    s = w[key]
    if case == "drop_key"
        delete!(w, key)
    elseif case == "extra_key"
        w[:zz_extra] = s
    elseif case == "not_series"
        w[key] = collect(s.values)
    elseif case == "wrong_element_type"
        w[key] = TSeries(firstdate(s), copy(reinterpret(UInt64, s.values)))
    elseif case == "wrong_frequency"
        w[key] = TSeries(1U, copy(s.values))
    elseif case == "shift_start"
        w[key] = TSeries(firstdate(s) + 1, copy(s.values))
    elseif case == "truncate"
        w[key] = TSeries(firstdate(s), s.values[1:end-1])
    elseif case == "flip_missing"
        v = copy(s.values)
        v[1] = NaN
        w[key] = TSeries(firstdate(s), v)
    elseif case == "corrupt_value"
        v = copy(s.values)
        v[1] = nextfloat(v[1])
        w[key] = TSeries(firstdate(s), v)
    end
    return w
end

timed(f) = @elapsed f()

phases = Dict{String,Dict{String,Vector{Float64}}}()
function record(scn, phase, seconds)
    bucket = get!(phases, scn, Dict{String,Vector{Float64}}())
    push!(get!(bucket, phase, Float64[]), seconds)
end

function deliver(result)
    part = result_path * ".part"
    open(part, "w") do io
        print(io, json_value(result))
    end
    mv(part, result_path; force=true)
end

fixtures, hashes = build_fixtures()
expected, _ = build_fixtures()
domains = Dict{String,Any}(scn => domain(fixtures[scn]) for scn in keys(fixtures))
verified = Dict{String,Int}(scn => 0 for scn in keys(fixtures))
base = Dict{String,Any}(
    "group" => "benchmark",
    "token" => token,
    "fame_tree_hash" => tree_hash(),
    "julia_version" => string(VERSION),
    "fixture_hashes" => hashes,
    "fixture_domains" => domains,
)

for rep in 0:reps
    for scn in ("many_small", "few_large", "missing_density")
        w = fixtures[scn]
        path = joinpath(scratch, "julia-" * scn * "-" * string(rep) * ".db")
        tw = timed(() -> writefame(path, w; mode=:create))
        got = nothing
        tr = timed(() -> (got = readfame(path)))
        if negative != "none" && rep == 0 && scn == "many_small"
            got = corrupt(got, negative)
        end
        check = verify(got, expected[scn])
        if check != ""
            failure = copy(base)
            failure["verified"] = false
            failure["failure"] = Dict{String,Any}("scenario" => scn, "check" => check)
            failure["complete"] = true
            deliver(failure)
            exit(3)
        end
        verified[scn] += 1
        if rep > 0
            record(scn, "write_workspace", tw)
            record(scn, "read_workspace", tr)
        end
    end
end

result = copy(base)
result["phases"] = phases
result["verified"] = true
result["verification"] = verified
result["complete"] = true
deliver(result)
"""


def _duration(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _numbers(value: Any, count: int) -> bool:
    return isinstance(value, list) and len(value) == count and all(_duration(v) for v in value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _domain(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"frequency", "start", "length"}
        and value["frequency"] in FREQUENCIES
        and _integer(value["start"])
        and _integer(value["length"])
        and value["length"] > 0
    )


def _common(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The fields every Julia result carries, validated, or None."""
    if payload.get("group") != "benchmark":
        return None
    tree = payload.get("fame_tree_hash")
    version = payload.get("julia_version")
    hashes = payload.get("fixture_hashes")
    domains = payload.get("fixture_domains")
    if not isinstance(tree, str) or not (tree == "unknown" or _TREE.fullmatch(tree)):
        return None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return None
    if not isinstance(hashes, dict) or set(hashes) != set(SCENARIOS):
        return None
    if not all(isinstance(h, str) and _HASH.fullmatch(h) for h in hashes.values()):
        return None
    if not isinstance(domains, dict) or set(domains) != set(SCENARIOS):
        return None
    if not all(_domain(d) for d in domains.values()):
        return None
    return {
        "fame_tree_hash": tree,
        "fame_tree_pinned": tree == PINNED_FAME_TREE_HASH,
        "julia_version": version,
        "fixture_hashes": dict(hashes),
        "fixture_domains": {name: dict(domains[name]) for name in SCENARIOS},
    }


_RESULT_KEYS = frozenset(
    {
        "group",
        "token",
        "complete",
        "fame_tree_hash",
        "julia_version",
        "phases",
        "fixture_hashes",
        "fixture_domains",
        "verified",
        "verification",
    }
)
_FAILURE_KEYS = frozenset(
    {
        "group",
        "token",
        "complete",
        "fame_tree_hash",
        "julia_version",
        "fixture_hashes",
        "fixture_domains",
        "verified",
        "failure",
    }
)


def validate_julia_result(payload: Any, repetitions: int) -> dict[str, Any] | None:
    """The accepted, reduced Julia result, or None when any field is not as expected.

    Besides the timings, the result must state ``verified: true`` and a
    verification count of ``repetitions + 1`` read-backs per scenario (the
    untimed pass included); without that record no timing is accepted.
    """
    if not isinstance(payload, dict) or set(payload) != _RESULT_KEYS:
        return None
    common = _common(payload)
    if common is None or payload["verified"] is not True:
        return None
    verification = payload["verification"]
    if not isinstance(verification, dict) or set(verification) != set(SCENARIOS):
        return None
    if any(
        not _integer(verification[name]) or verification[name] != repetitions + 1
        for name in SCENARIOS
    ):
        return None
    phases = payload["phases"]
    if not isinstance(phases, dict) or set(phases) != set(SCENARIOS):
        return None
    reduced: dict[str, Any] = {}
    for scenario, bucket in phases.items():
        if not isinstance(bucket, dict) or set(bucket) != set(PHASES):
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
        **common,
        "phases": reduced,
        "verification": {name: repetitions + 1 for name in SCENARIOS},
    }


def validate_julia_failure(payload: Any) -> dict[str, str] | None:
    """The bounded failure record of a verification stop, or None."""
    if not isinstance(payload, dict) or set(payload) != _FAILURE_KEYS:
        return None
    if _common(payload) is None or payload["verified"] is not False:
        return None
    failure = payload["failure"]
    if not isinstance(failure, dict) or set(failure) != {"scenario", "check"}:
        return None
    if failure["scenario"] not in SCENARIOS or failure["check"] not in CHECKS:
        return None
    return {"scenario": failure["scenario"], "check": failure["check"]}


def _launch(
    julia: dict[str, str],
    directory: Path,
    scale: dict[str, int],
    repetitions: int,
    timeout: float,
    negative: str,
) -> dict[str, Any]:
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
        negative,
    ]
    try:
        completed = run_child(command, "", timeout, output_path=Path(tokens["log"]))
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except OSError as error:
        return {"error": "start_failed", "errno": error.errno}
    payload, kind = read_result(Path(tokens["result"]), tokens["token"])
    if completed.returncode == VERIFICATION_EXIT:
        failure = validate_julia_failure(payload)
        if failure is None:
            return {"error": "julia_verification_failed"}
        return {"error": "julia_verification_failed", "failure": failure}
    if completed.returncode != 0:
        return {"error": "julia_failed", "exit_code": completed.returncode}
    if payload is None:
        return {"error": kind or "invalid_result"}
    accepted = validate_julia_result(payload, repetitions)
    if accepted is None:
        return {"error": "invalid_result"}
    return accepted


def run_julia_benchmark(
    julia: dict[str, str], scratch: Path, scale: dict[str, int], repetitions: int, timeout: float
) -> dict[str, Any]:
    """The verified reference timings, or an error record; never a partial timing."""
    result = _launch(julia, scratch / "julia", scale, repetitions, timeout, "none")
    if "error" not in result:
        result["note"] = (
            "reference timings of writefame/readfame of the same workspaces on the same host, "
            "each read-back verified against regenerated fixtures outside the timed regions; "
            "fixture digests and index domains are compared with the Python side before any "
            "pair is called comparable"
        )
    return result


def run_julia_selfcheck(
    julia: dict[str, str], scratch: Path, timeout: float
) -> dict[str, dict[str, Any]]:
    """Run every negative case at the small scale; each must stop at its check.

    A case passes only when the script exits through the verification path
    with a failure record naming ``many_small`` and the expected check. A
    completed run, another error or another check is a failed case: the
    verification did not catch that corruption.
    """
    from . import SCALES

    outcomes: dict[str, dict[str, Any]] = {}
    for case, check in NEGATIVE_CASES.items():
        result = _launch(
            julia, scratch / f"julia-negative-{case}", SCALES["small"], 1, timeout, case
        )
        failed = result.get("error") == "julia_verification_failed"
        observed = result.get("failure") if failed else None
        expected = {"scenario": "many_small", "check": check}
        outcomes[case] = {
            "expected": expected,
            "observed": observed if observed is not None else result.get("error", "completed"),
            "outcome": "pass" if observed == expected else "fail",
        }
    return outcomes


__all__ = [
    "CHECKS",
    "NEGATIVE_CASES",
    "SCRIPT",
    "VERIFICATION_EXIT",
    "run_julia_benchmark",
    "run_julia_selfcheck",
    "validate_julia_failure",
    "validate_julia_result",
]
