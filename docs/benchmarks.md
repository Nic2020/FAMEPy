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
    [--julia <julia> --julia-project <project>]
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
| `many_small` | 20 / 500 monthly float64 series of 24 observations | `convert_to_fame`, `write_raw`, `post`, `read_raw`, `convert_from_fame`, `read_bridge`, `write_workspace`, `read_workspace` |
| `few_large` | 3 daily float64 series of 2 000 / 500 000 observations | same |
| `dates` | one monthly series of 200 / 20 000 daily dates, 5 % missing | `convert_to_fame`, `write_raw`, `post`, `read_raw`, `convert_from_fame` |
| `strings` | one case string series of 200 / 20 000 strings, 5 % missing | same |
| `missing_density` | one monthly float64 series of 2 000 / 200 000 observations at 0, 10, 50 and 90 % NaN | `write_density_*`, `read_density_*` through the single-object bridge forms, then `write_workspace`, `read_workspace` of the four series |
| `migration` | the `many_small` batch retired into DataEcon | `plan`, `migrate`, `read_back` (blocked when the DataEcon native extension is unavailable) |

Values come from a 64-bit linear congruential generator with fixed seeds
(`lcg_values`), so every run and the Julia script see the same numbers; the
report records an FNV-1a digest of every fixture's exact bytes
(`fixture_hashes`). Conversion (`to_fame`/`from_fame`) is timed apart from
the native writes and reads, posting apart from writing, and the workspace
forms apart from the single-object forms, so that Python conversion cost,
native I/O and posting are distinguishable.

Every scenario verifies, outside its timed regions, that what it read back
is what it wrote (exact float bits, dates, strings, first dates; for the
migration, the archive descriptions). A scenario whose verification fails
raises `BenchmarkFidelityError`, its worker exits with that class, and the
measurement is reported as `scenario_failed` without timings: a fast wrong
result is never timing evidence.

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
  `initialize` and the first database open separately and then runs a
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
whole workspace, which is the boundary of the Python `write_workspace` and
`read_workspace` phases (database opened and closed inside on both sides).
The script computes the same FNV-1a digests of its fixtures; the report
records whether they match the Python digests, and only matching scenarios
keep their entries in `comparison.comparable`. The `dates` and `strings`
scenarios are not compared: their Python fixtures contain missing
observations that the reference script would have to replace with other
values, which is different data and different native work; `migration` has
no reference equivalent. The Julia timings, Julia version and FAME.jl tree
hash are placed under `"julia"` next to the Python numbers, never merged
with them and never as a ratio; the tree hash is compared with the pinned
reference and recorded as pinned or not. The Python comparable samples ran
without instrumentation and the reference script has none, so neither side
carries tracing overhead. A Julia result with any unexpected field is
rejected as `invalid_result`.

## Profiling and optimization policy

`--profile` writes `cProfile` statistics per scenario into the scratch
directory (local only), from the instrumented pass. Optimizations are
implemented only where a profile of a native run locates a bottleneck and an
equivalence test covers the change; the package stays pure Python and adds
no compiled extension or compiler requirement on the strength of an
expectation. Timings from the fake backend or the C shim are never vendor
performance, and a schema-valid report is not proof of vendor performance
either: the `vendor_timing` flag and the library identity say what was
measured.
