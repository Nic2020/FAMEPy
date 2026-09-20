# Benchmarks and profiling

`python -m famepy.benchmarks` times the package's paths on synthetic data of
fixed shapes and seeds and writes one JSON report. The numbers are inputs
for a review of where time goes; the package makes no speed claim from
them, and the report says whether they are vendor timings at all.

```sh
python -m famepy.benchmarks --native --scratch <new-dir> --report bench.json \
    --scale standard [--library <chli>] [--root <fame-root>] \
    [--wheel <installed-wheel>] [--source-sha <revision>] \
    [--scenarios many_small,dates] [--repetitions 5] [--profile] [--timeout 600] \
    [--julia <julia> --julia-project <project> [--julia-selfcheck]]
```

Without `--native` (and without an injected test backend) the report is
`blocked`; the `FAME` environment variable and the trusted library apply as
for every other native use. With an injected backend (the offline tests)
the report carries `"vendor_timing": false` and measures only the package's
own overhead over a fake library. The exit status is 0 only when every
requested measurement completed and was accepted; otherwise it is 1 and
the report lists each failed, timed-out, blocked or rejected measurement
under `failures` with `"result": "incomplete"`.

## Scenarios and phases

| Scenario | Shape (`small` / `standard`) | Phases |
|---|---|---|
| `many_small` | 20 / 500 monthly float64 series of 24 observations from 2000M1 | `convert_to_fame`, `write_raw`, `post`, `read_raw`, `convert_from_fame`, `read_bridge`, `write_workspace`, `read_workspace` |
| `few_large` | 3 daily float64 series of 2 000 / 500 000 observations from 2000-01-03 | same |
| `dates` | one monthly series from 2000M1 of 200 / 20 000 daily dates, 5 % missing | `convert_to_fame`, `write_raw`, `post`, `read_raw`, `convert_from_fame` |
| `strings` | one case string series of 200 / 20 000 strings, 5 % missing | same |
| `missing_density` | one daily float64 series from 2000-01-03 of 2 000 / 200 000 observations at 0, 10, 50 and 90 % NaN | `write_density_*`, `read_density_*` through the single-object bridge forms, then `write_workspace`, `read_workspace` of the four series |
| `migration` | the `many_small` batch retired into DataEcon | `plan`, `migrate`, `read_back` (blocked when the DataEcon native extension is unavailable) |

Values come from a 64-bit linear congruential generator with fixed seeds
(`lcg_values`), so every run and the Julia script see the same numbers; the
report records an FNV-1a digest of every fixture's exact bytes
(`fixture_hashes`) and the index of every fixture (`fixture_domains`:
frequency, first moment as its integer, length), and each scenario's
`parameters` name its index frequency and start. Every fixture ends
inside the calendar at both scales: the standard `missing_density`
fixture spans 200 000 days from 2000-01-03 (to 2547), whereas 200 000
months would end far beyond year 9999, outside the range the library
accepts; the offline tests pin both endpoints and the cross-language
index. Conversion (`refame`/`unfame`) is timed apart from
the native writes and reads (`do_write`, and `quick_info` followed by
`do_read`), posting apart from writing, and the workspace forms
(`writefame`/`readfame`) apart from the single-object forms, so that
Python conversion cost, native I/O and posting are distinguishable. The
phase labels in the report (`convert_to_fame`, `read_raw`, `read_bridge`,
`write_workspace`, ...) are fixed identifiers and did not change with the
API names.

Every scenario verifies, outside its timed regions, that what it read back
is what it wrote (exact float bits, dates, strings, first dates; for the
migration, the archive descriptions). A scenario whose verification fails
raises `BenchmarkFidelityError`, its worker exits with that class, and the
measurement is reported as `scenario_failed` without timings: a fast wrong
result is never timing evidence.

A failed measurement carries bounded diagnostics only: the exception
class name, the numeric native status when the error has one, the
operation from the package's own native-boundary method names, and the
phase label in progress from the scenario's fixed phase set (for example
`HLIError`, status 9, `write_precisions`, `write_density_00`). No
message text, extended bytes, path or free label is exported; a worker
record with any other field or an out-of-range value contributes no
diagnostics at all (`diagnostics: rejected`), and a failure record never
carries timing data.

## How a measurement runs

Every measurement is one worker process launched under the validation
runner's worker protocol (`famepy.validation._process`): the launcher
reserves a fresh result file and token, the worker redirects its standard
streams to a local log inside the scratch directory (the library may print
there; the log is counted, never parsed), writes its result atomically with
the token, and is terminated with its whole process tree when it exceeds
`--timeout`. A missing, stale, partial, oversized or malformed result, a
result for another scenario or mode, or a result with any field outside the
allowlist (numbers, booleans, `None` and the fixed labels the harness
defines) is rejected and reported as such. No native call runs in the
coordinating process.

- Warm (`"warm"`): one untimed pass, then `--repetitions` timed passes
  without any instrumentation, then one instrumented pass whose timings are
  not reported: the counting proxy over the native boundary gives the
  native call count per phase, `tracemalloc` the Python-side peak
  allocation, and `--profile` the `cProfile` statistics
  (`<scratch>/warm-<scenario>/work/profile.prof`, local only).
- Cold (`"cold"`): a fresh interpreter process per scenario that times
  initialization and the first database open separately and then runs a
  single timed pass without instrumentation. "Cold" means a fresh process;
  the file-system cache is not cleared and the report says so.
  `--no-cold` skips these runs.

## What is recorded

- Per phase: every sample in seconds, minimum, median and maximum over the
  repetitions, and the number of native calls the phase made through the
  package's own boundary in the instrumented pass (the library's internal
  work is not visible and not claimed).
- Per scenario: the parameters, the object and observation counts, the
  database size in bytes, `verified: true`, the Python-side peak allocation
  of the instrumented pass and the worker's peak resident size since start
  where the platform reports it (Windows working set, POSIX `ru_maxrss`),
  with the scope stated in the report; and the stray output size of the
  worker.
- Identity: the FAME version the workers report, the library file size and
  SHA-256 (never its path), the installed package identity (source digest,
  import location class, distribution version, and the wheel comparison
  when `--wheel` is given, exactly as the validation runner records it),
  the Python, NumPy, TimeSeriesEconPy and FAMEPy versions, the platform
  and architecture.

The report never contains paths or native text. Tuning options, cache
settings or startup files of the installation are not changed by the
harness; if they influence a measurement, that belongs in the review of
the run, not in a package default.

## Julia comparison

With `--julia` and `--julia-project` the harness also runs a FAME.jl script
on the same host, under the same worker protocol, on the scenarios whose
fixtures and phase boundaries are equivalent: `many_small`, `few_large` and
`missing_density`, timing the reference's `writefame` and `readfame` of the
whole workspace, which is the boundary of the Python `writefame` and
`readfame` phases (database opened and closed inside on both sides).
The reference's workspace write catches per-object errors and logs them
instead of raising, so the script verifies every `readfame` result,
including the untimed warm-up pass, outside the timed regions against
fixtures it generates a second time: the exact key set, the series and element types,
index frequency, first moment and length, the NaN positions and the
bitwise values elsewhere. Any disagreement stops the script with a
failure record naming the scenario and the check (`key_set`, `type`,
`frequency`, `start`, `length`, `missing_positions`, `values`) and a
nonzero exit, reported as `julia_verification_failed`; a completed run
must state the number of verified read-backs per scenario (repetitions
plus one) or its timings are rejected. The script also reports the same
FNV-1a digests and index domains of its fixtures; the report records
whether both match the Python side. `--julia-selfcheck` additionally reruns
the script at the small scale with each of nine documented corruptions
injected into a read-back workspace (a dropped or extra key, a non-series
value, a wrong element type, another frequency, a shifted start, a truncation, a flipped NaN, a
changed value) and requires the matching check to stop it; any case that
completes or stops at another check is a failure of the run. That
self-check is the executable evidence that the verification runs; the
offline tests exercise the Python side of the protocol with a stand-in
process only and claim nothing about Julia. The `dates` and `strings`
scenarios are not compared: their Python fixtures contain missing
observations that the reference script would have to replace with other
values, which is different data and different native work; `migration` has
no reference equivalent. The Julia timings, Julia version and FAME.jl tree
hash are placed under `"julia"` next to the Python numbers, never merged
with them and never as a ratio; the tree hash is compared with the pinned
reference and recorded as pinned or not (`julia_qualification`). The
Python comparable samples ran without instrumentation and the reference
script has none, so neither side carries tracing overhead. A Julia result
with any unexpected field is rejected as `invalid_result`.

The `comparison` block separates what the two workloads define alike
from what was actually measured: `candidate_pairs` lists the
scenario/phase pairs by definition; `python_accepted` says which
comparable Python warm measurements completed; `julia` is `absent`,
`failed` or `verified`; and `verified_pairs` lists only the pairs whose
Python measurement was accepted, whose Julia run was verified, and whose
fixture digests and index domains agree (`julia_fixtures_match`,
`julia_domains_match`). A failed or absent Python measurement, or a
failed, rejected or unverified Julia run, never appears as a verified
pair.

## Profiling and optimization policy

`--profile` writes `cProfile` statistics per scenario into the scratch
directory (local only), from the instrumented pass. Optimizations are
implemented only where a profile of a native run locates a bottleneck and an
equivalence test covers the change; the package stays pure Python and adds
no compiled extension or compiler requirement on the strength of an
expectation.

What the accepted runs (both hosts, revision 85e46c5) locate, as inputs
for a later revision rather than as a change made now: in the `dates`
scenario the two conversion phases make one native call per date
observation (each date value goes through the library's own year/period
conversion, which is what the frequency anchors are verified against) and
take far longer than the raw transfer of the same object, on both hosts;
in every other scenario the native calls per phase are proportional to
the object count, not the observation count, and no phase stands out
against the native work it wraps. Date value conversion is therefore the
recorded profiling candidate. A change there would have to keep the
verified per-anchor conversion as its oracle (for example by converting
each distinct date once, or by a batched conversion checked against the
library's on every anchor in the `frequencies` group) and would be
qualified by the campaign before it is relied on; the numbers themselves
are not published as a performance claim and are not compared across the
two hosts. Timings from the fake backend or the C shim are never vendor
performance, and a schema-valid report is not proof of vendor performance
either: the `vendor_timing` flag and the library identity say what was
measured.


The canonical `do_write` default performs a delete attempt before creation,
matching FAME.jl, including when the name is new. Instrumented direct-write
phases therefore count one additional native call per object compared with
0.1.0rc1. This is part of the workload being measured; the benchmark does not
silently opt out of replacement to retain the old count. Historical timings
remain tied to their original candidate.
