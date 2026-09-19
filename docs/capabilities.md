# Capability status

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
Three evidence levels are distinguished: *offline* (Python tests with the
in-memory fake backend and the independent C shim), *native-verified* (the
consolidated validation campaign passed on an installed FAME) and *planned*.

Native evidence to date: at revision 82e7662 the consolidated campaign
passed all eight groups then defined (lifecycle, databases, raw type
matrix, discovery, commands, bridge with every value kind, every frequency
anchor, workspaces) on one Windows host (CHLI release 11.8 file metadata)
and one Linux host, with no failed, blocked or missing required case, and
every configured Julia comparison passed on Linux. That acceptance covers
the tested local database modes, the raw kinds with preserved missing
encodings, name-lists, listing filters, commands, the full bridge and the
workspace forms on those two installations; it is not blanket version
coverage and not publication approval. Measured behavior is reported for
the inspected installations only, never as a rule for every FAME version.
The Linux Julia comparisons ran against a FAME.jl tree that differs from
the pinned reference in `README.md` only (source, build scripts, metadata
and tests identical); the campaign report stays qualified, and the
[parity ledger](parity.md) records that source equivalence.

Since that acceptance this revision adds the opt-in extended-error
retrieval over the declared `cfmlerr` call, the FAME-to-DataEcon migration
workflow, the benchmark harness and two validation groups
(`extended_errors`, `migration`); these are offline-tested and *pending*
their first native run. See the [parity ledger](parity.md) for every
reference export with its class and evidence.

| Reference surface | Python status | Evidence | Native |
|---|---|---|---|
| Library discovery and platform handling | Implemented | explicit path / `FAMEPY_LIBRARY` / `FAME` precedence, trusted root directories, redacted errors | discovery verified on both hosts |
| check_status / HLIError | Implemented as `check_status` / `FameError` | known/unknown codes retained; no native text by default | verified through every failing case of the campaign |
| Probe diagnostics | Implemented | failure classes, OS numbers, presence-only symbols | verified on both hosts |
| CHLI ABI declarations | Candidate inventory | 37 functions, 15 globals, [per-function checklist](abi-checklist.md) with the direction of every text argument; documented in/output text travels in owned writable buffers; shim exercises every call and really rewrites those buffers | every declaration the core uses exercised natively on both hosts; the writable text positions reviewed per host |
| version, init_chli, close_chli | Implemented as `initialize` / `version` / `finalize`; `reset` deliberately unsupported | one-shot per process (initialize once, finalization terminal, no restart in the same process), failed startup and failed finalization terminal, fork rejection, licensing environment, process-fixed library | verified on both hosts: initialize, version, sentinel reads, terminal finalize, rejection of every restart route, fresh process |
| FameDatabase, workdb, opendb, postdb, closedb! | Implemented as `Database`, `work_database`, `open_database`, `post`, `close` | the five local modes open; `write` and `direct_write` are modes of a database opened on a named server connection (an API neither the package nor the reference binds) and are refused before any native call; seven constants kept for parity; read-only default, explicit posting, handle validation inside the operation lock, failed close kept tracked, redacted names | verified on both hosts: read-only, create, update, shared, overwrite, work database, posting, cross-process persistence, stale handles, and the bad-mode status for `write` and `direct_write` |
| FameObject, Period, quick_info | Implemented as `ObjectInfo`, `Period`, `quick_info` | class/type/frequency/range, 64-bit indices, date-valued types | verified on both hosts |
| listdb / ITEM filters | Implemented as `list_objects` | `?` and `^`, alias/class/type filters, exact-frequency filter enforced on listed metadata, native narrowing with documented family (`ITEM FREQUENCY MONTHLY`, ...) and index (`ITEM INDEX CASE`) words only where it cannot exclude a requested object, 242-byte names, truncation error, cursor cleanup, documented ITEM normalization (not restoration) | verified on both hosts, including the family and index words and the exact-code post-filter |
| do_read! / do_write | Implemented as `read_object` / `write_object` | all six kinds, subranges, empty series, replacement scope, complete validation before any native call, exact-width scalar reads, missing preservation, namelist members parsed under a strict grammar from the raw bytes | verified on both hosts for every kind, interior missing values, endpoint fixtures, scalars, replacement, deletion and cross-process readback; the namelist layout the library returns differs from what was written (blank after each comma) and the ordered members are asserted |
| Endpoint handling of missing observations | Recorded per campaign | the package neither pads nor trims; the campaign asserts that whatever is retained equals what was written | measured on both inspected installations: leading and trailing ND dropped, an all-ND range reads back empty, leading and trailing NC and NA retained; behavior of those installations, not a rule |
| fame and string macro | Implemented as `run_command` | captured/quiet/stream output, recursive INPUT (consecutive statements included) with literal FILE(), suffix rules, cycle/depth/size limits, redacted file errors, cleanup on failure, failing stage named on the error, output file created by the library in a private directory | verified on both hosts |
| Extended error text (status 513) | Implemented, opt-in | `session.enable_extended_errors()` binds the retrieval to the declared `cfmlerr` (length) and `cfmferr` (fetch) calls: bounded buffer sized by the library, captured at the failure under the lock, attached to the error, never in messages; off by default because the text can carry private command text | the `cfmlerr` declaration was recorded as the older convention on both hosts; the `extended_errors` group is the native gate, pending its first paired run |
| refame / unfame | Implemented as `bridge.to_fame` / `bridge.from_fame` with `read_value` / `write_value` | every reference kind: precision, numeric, exactness-checked integers, Boolean, date scalars and series, strings, name-lists, string vectors and tuples; explicit `Text`, `NameList`, `DateSeries`, `StringSeries` carriers; missing (`nan`/`strict`), empty (`preserve`/`reference`) and text (`ascii`/`bytes`) policies; NaN as NC; the reference's missing-Boolean-as-true and scalar-NaN-equality behaviors are deliberately not reproduced, and a case moment is refused as a date value before any native call because the library does not type objects by the case frequency (see [contracts](contracts.md)) | every kind, every missing category and the empty conventions verified on both hosts at 82e7662 with cross-process persistence; the library's reserved-name status (25) asserted natively; Linux values matched the README-only different FAME.jl tree (qualified) |
| Bridge frequency maps | Implemented for the reference set | case, daily, business, the seven weekly endings, monthly, the three quarterly anchors, the six half-yearly anchors and the twelve annual anchors; indices through the library's year/period conversion; reference year/period conventions (day and business day of year, week of the ending day); ten-day, biweekly, twice-monthly, bimonthly, ypp/ppy, intraday and weekly-pattern frequencies refused with `UnsupportedFrequencyError`, never remapped; case is an index frequency only and is refused as a date value frequency | every anchor's inverse conversion, adjacency, year boundary, period count, round trip, raw categories and cross-process readback verified on both hosts at 82e7662; the library's case-type status (16) asserted natively; the week-53 reading recorded as an observation |
| readfame / writefame | Implemented as `bridge.read_workspace` / `bridge.write_workspace` and the `*_report` variants | explicit names and wildcards with listing filters, `namecase`, `prefix`/`glue` stripping, nested `collect`, recursive flattening of workspaces, mappings and multivariate series, collision, cycle and name validation before any write, strict default with per-object reporting mode, raw-carrier fallback | `workspace` group passed on both hosts at 82e7662 |
| Multivariate series reconstruction | Not implemented (as in the reference) | columns are written as separate series and read back as separate series; `collect` nests them under the original name | not applicable |
| Julia differential checks | Runner support | optional `--julia` group compares bit patterns and moment integers for every frequency anchor and value kind in both directions, and qualifies an unpinned FAME.jl tree | every configured comparison passed on Linux at 82e7662 against a tree that differs from the pinned reference in README only (qualified in the report; source equivalence recorded in the ledger); Windows had no Julia |
| Remote databases | Connection strings pass through the local open only | no server write API is bound; `write` and `direct_write` are refused before any native call | server-connection reads and writes remain an explicit follow-up gate |
| Text encoding | ASCII validation boundary | non-ASCII input is refused; bytes are preserved; the library's string missing sentinels are two non-ASCII bytes and are handled as bytes everywhere | vendor encoding unknown; sentinel lengths observed on both hosts |
| FAME-to-DataEcon migration (extension) | Implemented as `famepy.migration` | metadata plan before any write, plan re-checked against the source at run time, refuse-by-default loss policies, one new file per run (exclusive claim, partial file moved into place, no append), versioned attribute/mask layout over public DataEcon operations, per-object report, `complete`/`incomplete` mark, structural verification on read ([guide](migration.md)) | offline against the installed DataEcon extension and the fake backend, including structural-corruption and unchanged-file cases; the `migration` group is the native gate, pending |
| Benchmarks and profiling (extension) | Implemented as `python -m famepy.benchmarks` | deterministic scenarios with fixture digests, one worker process per measurement under the runner's result-file protocol with process-tree deadlines and an allowlisted result, read-back verification outside the timed regions, timed samples apart from the instrumented pass (native call counts, memory, `cProfile`), cold fresh-process runs, package identity, optional same-host Julia timings on the equivalent scenarios only ([guide](benchmarks.md)) | harness tests offline only, including hang, crash, corrupted-read and forged-result cases; no vendor timing yet, no speed claim |

Formula and global-object classes are not offered as structured I/O; FAME
commands remain available for them. Frequencies outside the bridge remain raw
codes on `RawSeries` and `ObjectInfo` and are never silently converted.

The `python -m famepy.validation` runner covers lifecycle, databases, the raw
type matrix, discovery, commands, the bridge (with every value kind), the
frequency anchors, workspaces, extended errors and the migration, each with
cross-process checks; see [native validation](native-validation.md).
