# Capability status

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
Three evidence levels are distinguished: *offline* (Python tests with the
in-memory fake backend and the independent C shim), *native-verified* (the
consolidated validation campaign passed on an installed FAME) and *planned*.

Native evidence to date: the consolidated campaign has passed all six groups
of the operational core (lifecycle, databases, raw type matrix, discovery,
commands and the monthly precision bridge) on one Windows host and one Linux
host. That acceptance covers the tested local database modes, the raw
scalar/series kinds with preserved missing encodings, name-lists, listing
filters, commands and the monthly bridge; it is not full reference parity.
Measured behavior is reported for the inspected installations only, never as
a rule for every FAME version. The full bridge and workspace surface (every
reference frequency anchor, every value kind, workspace reads and writes)
had one paired native run at revision 8f6eab7: the six core groups and the
`workspace` group passed on both hosts, all configured Linux Julia
comparisons passed (against an unpinned FAME.jl tree, qualified), the
calendar anchor, inverse, adjacency and raw-category checks passed, and the
`bridge` and `frequencies` groups failed on two invalid fixtures (an object
named by a reserved word, status 25; a date series typed by the case
frequency, status 16), with the remaining failures cascading from the
missing objects. This revision corrects both inputs, separates index from
value frequencies, contains fixture failures per object and models both
library boundaries offline; it has not yet run natively, so the two groups
stay *pending* until a full paired verification passes.

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
| Extended error text (status 513) | Mechanics only | opt-in retrieval with dynamic capacity, captured at the failure under the lock and attached to the error; no vendor declaration is shipped, so `extended_error_text` raises `UnsupportedOperationError` until one is configured | blocked on a verified `cfmlerr` declaration; an explicit follow-up gate |
| refame / unfame | Implemented as `bridge.to_fame` / `bridge.from_fame` with `read_value` / `write_value` | every reference kind: precision, numeric, exactness-checked integers, Boolean, date scalars and series, strings, name-lists, string vectors and tuples; explicit `Text`, `NameList`, `DateSeries`, `StringSeries` carriers; missing (`nan`/`strict`), empty (`preserve`/`reference`) and text (`ascii`/`bytes`) policies; NaN as NC; the reference's missing-Boolean-as-true and scalar-NaN-equality behaviors are deliberately not reproduced, and a case moment is refused as a date value before any native call because the library does not type objects by the case frequency (see [contracts](contracts.md)) | monthly precision scalars and series verified on both hosts (NaN as NC, strict mode, empty conventions, cross-process persistence; Linux values also matched an unpinned FAME.jl tree, qualified); the other kinds ran once natively at 8f6eab7 and failed on a reserved fixture name, now corrected and pending a new paired run |
| Bridge frequency maps | Implemented for the reference set | case, daily, business, the seven weekly endings, monthly, the three quarterly anchors, the six half-yearly anchors and the twelve annual anchors; indices through the library's year/period conversion; reference year/period conventions (day and business day of year, week of the ending day); ten-day, biweekly, twice-monthly, bimonthly, ypp/ppy, intraday and weekly-pattern frequencies refused with `UnsupportedFrequencyError`, never remapped; case is an index frequency only and is refused as a date value frequency | monthly verified on both hosts; at 8f6eab7 every anchor's inverse conversion, adjacency, year boundary, period count, round trip and raw categories passed on both hosts and the group failed only on the case-valued fixture, now replaced; the group stays pending until a new paired run passes |
| readfame / writefame | Implemented as `bridge.read_workspace` / `bridge.write_workspace` and the `*_report` variants | explicit names and wildcards with listing filters, `namecase`, `prefix`/`glue` stripping, nested `collect`, recursive flattening of workspaces, mappings and multivariate series, collision, cycle and name validation before any write, strict default with per-object reporting mode, raw-carrier fallback | `workspace` group passed on both hosts at 8f6eab7; re-verification pending with the corrected candidate |
| Multivariate series reconstruction | Not implemented (as in the reference) | columns are written as separate series and read back as separate series; `collect` nests them under the original name | not applicable |
| Julia differential checks | Runner support | optional `--julia` group compares bit patterns and moment integers for every frequency anchor and value kind in both directions, and qualifies an unpinned FAME.jl tree | every configured comparison passed on Linux at 8f6eab7 against an unpinned tree (qualified, not pinned parity); Windows had no Julia |
| Remote databases | Connection strings pass through the local open only | no server write API is bound; `write` and `direct_write` are refused before any native call | server-connection reads and writes remain an explicit follow-up gate |
| Text encoding | ASCII validation boundary | non-ASCII input is refused; bytes are preserved; the library's string missing sentinels are two non-ASCII bytes and are handled as bytes everywhere | vendor encoding unknown; sentinel lengths observed on both hosts |

Formula and global-object classes are not offered as structured I/O; FAME
commands remain available for them. Frequencies outside the bridge remain raw
codes on `RawSeries` and `ObjectInfo` and are never silently converted.

The `python -m famepy.validation` runner covers lifecycle, databases, the raw
type matrix, discovery, commands, the bridge (with every value kind), the
frequency anchors and workspaces, each with cross-process persistence checks;
see [native validation](native-validation.md).
