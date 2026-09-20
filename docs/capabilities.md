# Capability status

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
Three evidence levels are distinguished: *offline* (Python tests with the
in-memory fake backend and the independent C shim), *native-verified* (the
consolidated validation campaign passed on an installed FAME) and *planned*.

Native evidence to date: at revision 889d219 the consolidated campaign
passed all eleven groups (lifecycle, databases, raw type matrix,
discovery, commands, bridge with every value kind, every frequency anchor,
workspaces, extended errors, migration, value text policies) in one fresh
run on one Windows host (CHLI release 11.8 file metadata) and one Linux
host, with no failed, blocked or missing required case, and every
configured FAME.jl comparison (the bridge differential and the text
comparison) passed on both hosts. Earlier revisions had passed the eight
groups then defined at 82e7662 and the ten at 040fed3 on the same two
installations. At revision 85e46c5 the benchmark harness completed all
six scenarios in the warm and cold modes at the standard scale on both
hosts with vendor timing, and on Linux the reference script verified
every read-back of the three comparable workloads and rejected all nine
injected corruptions. That acceptance covers the tested local database
modes, the raw kinds with preserved missing encodings, name-lists,
listing filters, commands, extended error retrieval, the full bridge, the
workspace forms, the migration and the string value policies on those two
installations; it is not blanket version coverage and not a statement
about other FAME versions. Measured behavior is reported for the
inspected installations only. The FAME.jl tree the comparisons ran
against, on both hosts, differs from the pinned reference in `README.md`
only (source, build scripts, metadata and tests identical); the reports
stay qualified, and the [parity ledger](parity.md) records that source
equivalence.

Bounded remote evidence: the reference's documented connection-string
route is read-only. The same small approved selection (three nonempty
annual numeric and precision series without missing observations) was
read through the reference and through this package on both hosts, in
fresh read-only processes, and compared item by item on the host: class,
type, frequency, range, length, stored value bits, missing categories,
the wrappers' NaN-normalized values and the bridge index agreed for every
item on both hosts. That establishes value equality of that selection
over that route; it does not establish listing coverage, other object
types, string values or missing categories over the route, other servers,
or any write path (neither wrapper binds one).

See the [parity ledger](parity.md) for every reference export with its
class and evidence, and [releasing](releasing.md) for how a candidate is
qualified and published.

| Reference surface | Python status | Evidence | Native |
|---|---|---|---|
| Library discovery and platform handling | Implemented | explicit path / `FAMEPY_LIBRARY` / `FAME` precedence, trusted root directories, redacted errors | discovery verified on both hosts |
| check_status / HLIError | Implemented as `check_status` / `HLIError` | known/unknown codes retained; no native text by default | verified through every failing case of the campaign |
| Probe diagnostics | Implemented | failure classes, OS numbers, presence-only symbols | verified on both hosts |
| CHLI ABI declarations | Candidate inventory | 37 functions, 15 globals, [per-function checklist](abi-checklist.md) with the direction of every text argument; documented in/output text travels in owned writable buffers; shim exercises every call and really rewrites those buffers | every declaration the core uses exercised natively on both hosts; the writable text positions reviewed per host |
| version, init_chli, close_chli | Implemented as `version` / `init_chli` / `close_chli`; `reset` deliberately unsupported | one-shot per process (initialize once, finalization terminal, no restart in the same process), failed startup and failed finalization terminal, fork rejection, licensing environment, process-fixed library | verified on both hosts: initialize, version, sentinel reads, terminal finalize, rejection of every restart route, fresh process |
| FameDatabase, workdb, opendb, postdb, closedb! | Implemented as `FameDatabase`, `workdb`, `opendb`, `postdb`, `closedb` | the five local modes open; `write` and `direct_write` are modes of a database opened on a named server connection (an API neither the package nor the reference binds) and are refused before any native call; seven constants kept for parity; read-only default, explicit posting, handle validation inside the operation lock, failed close kept tracked, redacted names | verified on both hosts: read-only, create, update, shared, overwrite, work database, posting, cross-process persistence, stale handles, and the bad-mode status for `write` and `direct_write` |
| FameObject, Period, quick_info | Implemented as `FameObject`, `Period`, `quick_info` | class/type/frequency/range, 64-bit indices, date-valued types | verified on both hosts |
| listdb / ITEM filters | Implemented as `listdb` (`alias`, `class_`, `type`, `freq`) | `?` and `^`, alias/class/type filters, exact-frequency filter enforced on listed metadata, native narrowing with documented family (`ITEM FREQUENCY MONTHLY`, ...) and index (`ITEM INDEX CASE`) words only where it cannot exclude a requested object, 242-byte names, truncation error, cursor cleanup, documented ITEM normalization (not restoration) | verified on both hosts, including the family and index words and the exact-code post-filter |
| do_read! / do_write | Implemented as `do_read` / `do_write` on a `FameObject` | all six kinds, subranges, empty series, replacement scope, complete validation before any native call, exact-width scalar reads, missing preservation, namelist members parsed under a strict grammar from the raw bytes | verified on both hosts for every kind, interior missing values, endpoint fixtures, scalars, replacement, deletion and cross-process readback; the namelist layout the library returns differs from what was written (blank after each comma) and the ordered members are asserted |
| Endpoint handling of missing observations | Recorded per campaign | the package neither pads nor trims; the campaign asserts that whatever is retained equals what was written | measured on both inspected installations: leading and trailing ND dropped, an all-ND range reads back empty, leading and trailing NC and NA retained; behavior of those installations, not a rule |
| fame and string macro | Implemented as `fame` (no macro form) | captured/quiet/stream output, recursive INPUT (consecutive statements included) with literal FILE(), suffix rules, cycle/depth/size limits, redacted file errors, cleanup on failure, failing stage named on the error, output file created by the library in a private directory | verified on both hosts |
| Extended error text (status 513) | Implemented, opt-in | `session.enable_extended_errors()` binds the retrieval to the declared `cfmlerr` (length) and `cfmferr` (fetch) calls: bounded buffer sized by the library, captured at the failure under the lock, attached to the error, never in messages; off by default because the text can carry private command text | the `cfmlerr` declaration was recorded as the older convention on both hosts; the `extended_errors` group passed on both hosts at 040fed3 |
| refame / unfame | Implemented as `refame` / `unfame` on a `FameObject` | every reference kind: precision, numeric, exactness-checked integers, Boolean, date scalars and series, strings, name-lists, string vectors and tuples; explicit `Text`, `NameList`, `DateSeries`, `StringSeries` carriers; missing (`nan`/`strict`), empty (`preserve`/`reference`) and text (`ascii`/`bytes`/`utf-8`) policies; NaN as NC; the reference's missing-Boolean-as-true and scalar-NaN-equality behaviors are deliberately not reproduced, and a case moment is refused as a date value before any native call because the library does not type objects by the case frequency (see [contracts](contracts.md)) | every kind, every missing category and the empty conventions verified on both hosts at 82e7662 and again at 040fed3 with cross-process persistence; the library's reserved-name status (25) asserted natively; values matched the README-only different FAME.jl tree on both hosts at 889d219 (qualified); the `utf-8` policy passed the `text` group on both hosts at 889d219 |
| Bridge frequency maps | Implemented for the reference set | case, daily, business, the seven weekly endings, monthly, the three quarterly anchors, the six half-yearly anchors and the twelve annual anchors; indices through the library's year/period conversion; reference year/period conventions (day and business day of year, week of the ending day); ten-day, biweekly, twice-monthly, bimonthly, ypp/ppy, intraday and weekly-pattern frequencies refused with `UnsupportedFrequencyError`, never remapped; case is an index frequency only and is refused as a date value frequency | every anchor's inverse conversion, adjacency, year boundary, period count, round trip, raw categories and cross-process readback verified on both hosts at 82e7662; the library's case-type status (16) asserted natively; the week-53 reading recorded as an observation |
| readfame / writefame | Implemented as `readfame` / `writefame` with `bridge.readfame_report` / `bridge.writefame_report` | explicit names and wildcards with listing filters, `namecase`, `prefix`/`glue` stripping, nested `collect`, recursive flattening of workspaces, mappings and multivariate series, collision, cycle and name validation before any write, strict default with per-object reporting mode, raw-carrier fallback | `workspace` group passed on both hosts at 82e7662 |
| Multivariate series reconstruction | Not implemented (as in the reference) | columns are written as separate series and read back as separate series; `collect` nests them under the original name | not applicable |
| Julia differential checks | Runner support | optional `--julia` group compares bit patterns and moment integers for every frequency anchor and value kind in both directions, and qualifies an unpinned FAME.jl tree | every configured comparison passed on Linux at 82e7662 and 040fed3 and on both hosts at 889d219 (bridge and text), against a tree that differs from the pinned reference in README only (qualified in the report; source equivalence recorded in the ledger) |
| Remote databases | Connection strings pass through the local open, as the reference does | the reference's documented connection-string route is read-only; no named-connection or server write API is bound by either wrapper, and `write` and `direct_write` are refused before any native call | the same approved three-item read-only selection (nonempty annual numeric and precision series, no missing observations) read through both wrappers on both hosts and equal on every compared property (class, type, frequency, range, length, stored bits, missing categories, normalized values, bridge index); nothing beyond those items was listed, and remote writes are outside the route |
| Text encoding | ASCII boundary for names, paths and commands; per-value policy for string values | names, namelist members, database strings and commands accept ASCII `str` or NUL-free bytes; string values take `text="ascii"` (default), `"bytes"` or strict `"utf-8"` on every bridge reader and writer; nothing is replaced or guessed; the library's string missing sentinels are two non-ASCII bytes, classified before any decoding | the `text` group passed on both hosts at 889d219: every corpus value stored the listed bytes and read back under each policy, the refusals preceded any native call, sentinels were classified before decoding, and the reference wrapper wrote every corpus label, read back ASCII, internal-multibyte-with-ASCII-suffix, the ASCII vector and the supplementary-inside case with equal text, and failed in its own read slicing (a string index error, recorded as a reference limitation) on the values whose last character is multibyte and on the mixed vector; this package's raw reads of every reference-written object returned the listed bytes and its `utf-8` reads the expected text |
| FAME-to-DataEcon migration (extension) | Implemented as `famepy.migration` | metadata plan before any write, plan re-checked against the source at run time, refuse-by-default loss policies, one new file per run (exclusive claim, partial file moved into place, no append), versioned attribute/mask layout over public DataEcon operations, per-object report, `complete`/`incomplete` mark, structural verification on read ([guide](migration.md)) | offline against the installed DataEcon extension and the fake backend, including structural-corruption and unchanged-file cases; the `migration` group passed on both hosts at 040fed3 |
| Benchmarks and profiling (extension) | Implemented as `python -m famepy.benchmarks` | deterministic scenarios with fixture digests, one worker process per measurement under the runner's result-file protocol with process-tree deadlines and an allowlisted result, read-back verification outside the timed regions, timed samples apart from the instrumented pass (native call counts, memory, `cProfile`), cold fresh-process runs, package identity, optional same-host Julia timings on the equivalent scenarios only ([guide](benchmarks.md)) | all six scenarios completed warm and cold at the standard scale on both hosts at 85e46c5 with vendor timing; the Linux reference script verified every read-back and rejected the nine injected corruptions; the harness tests cover hang, crash, corrupted-read and forged-result cases offline; no speed claim is made from the numbers |

Formula and global-object classes are not offered as structured I/O; FAME
commands remain available for them. Frequencies outside the bridge remain raw
codes on a `FameObject` and are never silently converted.

The native evidence above was recorded under the first candidate's
descriptive names; the 0.1.0rc2 rename keeps the storage, conversion and
ABI code behind those names unchanged and reshapes the object model
(`FameObject` in place of the metadata record and the raw carriers). The
renamed surface has offline coverage only until the next campaign runs.

The `python -m famepy.validation` runner covers lifecycle, databases, the raw
type matrix, discovery, commands, the bridge (with every value kind), the
frequency anchors, workspaces, extended errors, the migration and the value
text policies, each with cross-process checks; see
[native validation](native-validation.md).
