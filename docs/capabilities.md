# Capability status

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
Three evidence levels are distinguished: *offline* (Python tests with the
in-memory fake backend and the independent C shim), *native-verified* (the
consolidated validation campaign passed on an installed FAME) and *planned*.
Two campaigns on an installed FAME (one Windows host, one Linux host) have
passed the lifecycle group and the implemented monthly precision bridge. The
other groups have run and failed on both hosts, and the failures traced to
runner defects and to vendor behaviors that are not yet established (listed
in the *Native* column). Nothing beyond lifecycle and the monthly bridge is
native-verified; the rest of the bridge surface has no native evidence.

| Reference surface | Python status | Evidence | Native |
|---|---|---|---|
| Library discovery and platform handling | Implemented | explicit path / `FAMEPY_LIBRARY` / `FAME` precedence, trusted root directories, redacted errors | discovery only |
| check_status / HLIError | Implemented as `check_status` / `FameError` | known/unknown codes retained; no native text by default | pending |
| Probe diagnostics | Implemented | failure classes, OS numbers, presence-only symbols | discovery only |
| CHLI ABI declarations | Candidate inventory | 37 functions, 15 globals, [per-function checklist](abi-checklist.md); shim exercises every call | pending header check |
| version, init_chli, close_chli | Implemented as `initialize` / `version` / `finalize`; `reset` deliberately unsupported | one-shot per process (initialize once, finalization terminal, no restart in the same process), failed startup and failed finalization terminal, fork rejection, licensing environment, process-fixed library | verified on both hosts: initialize, version, sentinel reads, terminal finalize, rejection of every restart route, fresh process |
| FameDatabase, workdb, opendb, postdb, closedb! | Implemented as `Database`, `work_database`, `open_database`, `post`, `close` | seven modes, read-only default, explicit posting, handle validation inside the operation lock, failed close kept tracked, redacted names | read-only, create, update, shared, overwrite, work database, posting, cross-process persistence and stale handles passed on both hosts; `write` and `direct_write` opens returned status 5 on an existing database on both hosts (prerequisites not established; the campaign now also records the outcome on a new path) |
| FameObject, Period, quick_info | Implemented as `ObjectInfo`, `Period`, `quick_info` | class/type/frequency/range, 64-bit indices, date-valued types | pending |
| listdb / ITEM filters | Implemented as `list_objects` | `?` and `^`, alias/class/type filters, exact-frequency filter enforced on listed metadata, 242-byte names, truncation error, cursor cleanup, documented ITEM normalization (not restoration) | wildcards, class/type filters, capacities and truncation passed on both hosts; the native `ITEM FREQUENCY <name>` selection had no observed effect, so the frequency filter is a package contract and the native selection's semantics remain open |
| do_read! / do_write | Implemented as `read_object` / `write_object` | all six kinds, subranges, empty series, replacement scope, complete validation before any native call, exact-width scalar reads, missing preservation | precision and string series with interior missing values passed on both hosts; numeric, Boolean and date series that ended in ND read back one observation shorter (the campaign now records endpoint handling as observations and asserts interior preservation); scalars, replacement and deletion were not reached because of a runner defect |
| fame and string macro | Implemented as `run_command` | captured/quiet/stream output, recursive INPUT (consecutive statements included) with literal FILE(), suffix rules, cycle/depth/size limits, redacted file errors, cleanup on failure, failing stage named on the error, output file created by the library in a private directory | every command failed on both hosts (status 513 on Windows; the Linux child's stream output was unusable), with the stage not yet identified; the redirection syntax and output handling are open vendor questions |
| Extended error text (status 513) | Mechanics only | opt-in retrieval with dynamic capacity, captured at the failure under the lock and attached to the error; no vendor declaration is shipped, so `extended_error_text` raises `UnsupportedOperationError` until one is configured | blocked on cfmlerr declaration |
| refame / unfame | Monthly precision only (`famepy.bridge`) | scalars and series, NaN as NC, strict mode, explicit empty conventions | monthly precision scalars and series, NaN as NC, strict mode, empty conventions and cross-process persistence passed on both hosts; Linux values also matched an unpinned FAME.jl tree (qualified) |
| Bridge frequency maps | Monthly only | other frequencies raise `UnsupportedFrequencyError` | planned |
| readfame / writefame | Planned | workspace and multivariate flattening, names, collections | planned |
| Julia differential checks | Runner support | optional `--julia` group compares bit patterns and qualifies an unpinned FAME.jl tree | pending |
| Text encoding | ASCII validation boundary | non-ASCII input is refused; bytes are preserved; the library's string missing sentinels are two non-ASCII bytes and are handled as bytes everywhere | vendor encoding unknown; sentinel lengths observed on both hosts |

Formula and global-object classes are not offered as structured I/O; FAME
commands remain available for them. Frequencies outside the bridge remain raw
codes on `RawSeries` and `ObjectInfo` and are never silently converted.

The `python -m famepy.validation` runner covers lifecycle, databases, the raw
type matrix, discovery, commands and the bridge with cross-process persistence
checks; see [native validation](native-validation.md).
