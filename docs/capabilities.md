# Capability status

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
Three evidence levels are distinguished: *offline* (Python tests with the
in-memory fake backend and the independent C shim), *native-verified* (the
consolidated validation campaign passed on an installed FAME) and *planned*.
Three diagnostic campaigns have run on an installed FAME (one Windows host,
one Linux host each). The latest passed the lifecycle, commands and monthly
bridge groups on both hosts and failed three areas that traced to package
contracts contradicting the library's documented behavior; this revision
corrects those contracts (see the *Native* column) and has not yet been run
natively. Nothing beyond lifecycle, commands and the monthly bridge is
native-verified. Measured behavior is reported for the inspected
installations only, never as a rule for every FAME version.

| Reference surface | Python status | Evidence | Native |
|---|---|---|---|
| Library discovery and platform handling | Implemented | explicit path / `FAMEPY_LIBRARY` / `FAME` precedence, trusted root directories, redacted errors | discovery only |
| check_status / HLIError | Implemented as `check_status` / `FameError` | known/unknown codes retained; no native text by default | pending |
| Probe diagnostics | Implemented | failure classes, OS numbers, presence-only symbols | discovery only |
| CHLI ABI declarations | Candidate inventory | 37 functions, 15 globals, [per-function checklist](abi-checklist.md) with the direction of every text argument; documented in/output text travels in owned writable buffers; shim exercises every call and really rewrites those buffers | pending header check |
| version, init_chli, close_chli | Implemented as `initialize` / `version` / `finalize`; `reset` deliberately unsupported | one-shot per process (initialize once, finalization terminal, no restart in the same process), failed startup and failed finalization terminal, fork rejection, licensing environment, process-fixed library | verified on both hosts in every campaign: initialize, version, sentinel reads, terminal finalize, rejection of every restart route, fresh process |
| FameDatabase, workdb, opendb, postdb, closedb! | Implemented as `Database`, `work_database`, `open_database`, `post`, `close` | the five local modes open; `write` and `direct_write` are modes of a database opened on a named server connection (an API neither the package nor the reference binds) and are refused before any native call; seven constants kept for parity; read-only default, explicit posting, handle validation inside the operation lock, failed close kept tracked, redacted names | read-only, create, update, shared, overwrite, work database, posting, cross-process persistence and stale handles passed on both hosts in every campaign; the local open returned the bad-mode status (5) for `write` and `direct_write` on both hosts, which the campaign now asserts as the documented rejection instead of expecting a local write |
| FameObject, Period, quick_info | Implemented as `ObjectInfo`, `Period`, `quick_info` | class/type/frequency/range, 64-bit indices, date-valued types | quick_info and period conversion passed on both hosts |
| listdb / ITEM filters | Implemented as `list_objects` | `?` and `^`, alias/class/type filters, exact-frequency filter enforced on listed metadata, native narrowing with documented family (`ITEM FREQUENCY MONTHLY`, ...) and index (`ITEM INDEX CASE`) words only where it cannot exclude a requested object, 242-byte names, truncation error, cursor cleanup, documented ITEM normalization (not restoration) | wildcards, class/type filters, capacities and truncation passed on both hosts; the previous revision sent a per-anchor word (`ITEM FREQUENCY CASE`) that the library rejected as a bad option on both hosts, which is corrected here; the family and index words are documented but not yet exercised natively |
| do_read! / do_write | Implemented as `read_object` / `write_object` | all six kinds, subranges, empty series, replacement scope, complete validation before any native call, exact-width scalar reads, missing preservation, namelist members parsed under a strict grammar from the raw bytes | every precision, numeric, Boolean, date and string object (interior missing values, endpoint fixtures, scalars, replacement, deletion, cross-process readback) passed on both hosts; a namelist written as `{A,B,C}` read back as nine bytes on both hosts (a different layout of the same list, as the library documents it may produce), so the campaign now asserts the ordered members and records the layout |
| Endpoint handling of missing observations | Recorded per campaign | the package neither pads nor trims; the campaign asserts that whatever is retained equals what was written | measured on both inspected installations for all 35 fixtures: leading and trailing ND are dropped, an all-ND range reads back empty, leading and trailing NC and NA are retained; recorded as the behavior of those installations, not as a rule for every version |
| fame and string macro | Implemented as `run_command` | captured/quiet/stream output, recursive INPUT (consecutive statements included) with literal FILE(), suffix rules, cycle/depth/size limits, redacted file errors, cleanup on failure, failing stage named on the error, output file created by the library in a private directory | display, quiet redirection, input expansion, refusal cases, invalid-command stage, restoration and post-failure commands passed on both hosts in the latest campaign |
| Extended error text (status 513) | Mechanics only | opt-in retrieval with dynamic capacity, captured at the failure under the lock and attached to the error; no vendor declaration is shipped, so `extended_error_text` raises `UnsupportedOperationError` until one is configured | blocked on cfmlerr declaration |
| refame / unfame | Monthly precision only (`famepy.bridge`) | scalars and series, NaN as NC, strict mode, explicit empty conventions | monthly precision scalars and series, NaN as NC, strict mode, empty conventions and cross-process persistence passed on both hosts in every campaign; Linux values also matched an unpinned FAME.jl tree (qualified) |
| Bridge frequency maps | Monthly only | other frequencies raise `UnsupportedFrequencyError` | planned |
| readfame / writefame | Planned | workspace and multivariate flattening, names, collections | planned |
| Julia differential checks | Runner support | optional `--julia` group compares bit patterns and qualifies an unpinned FAME.jl tree | unpinned comparison on Linux only |
| Text encoding | ASCII validation boundary | non-ASCII input is refused; bytes are preserved; the library's string missing sentinels are two non-ASCII bytes and are handled as bytes everywhere | vendor encoding unknown; sentinel lengths observed on both hosts |

Formula and global-object classes are not offered as structured I/O; FAME
commands remain available for them. Frequencies outside the bridge remain raw
codes on `RawSeries` and `ObjectInfo` and are never silently converted.

The `python -m famepy.validation` runner covers lifecycle, databases, the raw
type matrix, discovery, commands and the bridge with cross-process persistence
checks; see [native validation](native-validation.md).
