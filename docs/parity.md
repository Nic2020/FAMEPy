# Parity ledger

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
Every exported name and qualified capability of the reference is mapped to
its Python counterpart, classified, and tied to its evidence. Evidence
levels: *offline* (tests against the in-memory fake backend and the
independent C shim), *native* (the consolidated campaign passed on one
Windows and one Linux installation at revision 82e7662) and *differential*
(the Linux campaign's Julia comparisons; see the note on the tested tree).
"Parity" means the reference's implemented behavior is reproduced;
"difference" means a deliberate, documented departure covered by tests;
"extension" means something the reference does not offer; "gap" means not
implemented.

## Runtime and status

| Reference | Python | Class | Evidence |
|---|---|---|---|
| `version` | `famepy.version()` | parity | native (both hosts) |
| `init_chli`, `close_chli` | `initialize()`, `finalize()`; `reset()` refuses | difference: one-shot per process, finalization terminal, no restart | native: every restart route rejected, fresh process verified |
| `check_status`, `HLIError` | `check_status`, `FameError` with numeric status | parity; no vendor text by default | native through every failing case |
| generated message table (`FAMEMessages.jl`) | fixed table of known codes in the package's own words, numeric fallback | difference: nothing parsed from an installation | offline |
| extended error text (`cfmferr` after status 513) | opt-in `Session.enable_extended_errors()` over the declared `cfmlerr`/`cfmferr` calls, bounded buffer, captured at the failure, never in messages | difference: opt-in and redacted by default | offline (fake and shim); the `extended_errors` group is the native gate of the new `cfmlerr` binding, pending its first paired run |

## Databases

| Reference | Python | Class | Evidence |
|---|---|---|---|
| `FameDatabase`, `opendb`, `closedb!` | `Database`, `open_database`, `close` | parity for the five local modes | native (all five modes, stale handles, cross-process) |
| `workdb` | `work_database()` | parity | native |
| `postdb` | `Database.post()`; high-level path writers post on success | parity; close never posts (documented) | native |
| `:write`, `:direct_write` modes | refused before any native call | difference: server-connection modes are not bound | native (the bad-mode status recorded) |
| scoped `opendb(f, ...)` | `with open_database(...)` | parity | native |
| remote connection strings | passed through the local open only | gap: no server-connection fixture; reads unverified, writes unbound | none |

## Objects and raw I/O

| Reference | Python | Class | Evidence |
|---|---|---|---|
| `FameObject`, `quick_info` | `ObjectInfo`, `quick_info` | parity | native |
| `Period`, `FameIndex`, `FameRange` | `Period`, `RangeSpec`; 64-bit indices throughout | difference: the reference's narrower `FameIndex(Period)` output width is not copied | native (year/period conversions on every anchor) |
| `listdb` and ITEM filters | `list_objects` | parity plus an exact-frequency post-filter; ITEM options normalized to ON afterwards (the reference restores nothing either) | native (family and index words, cursor cleanup, truncation) |
| `do_read!` | `read_object` (raw carriers) | parity plus subranges and exact-width scalars | native (every kind, endpoints, subranges) |
| `do_write` | `write_object` | parity; every check before the first native call; `replace` deletes first as the reference does | native |
| namelist text | ordered `NameList` members parsed under a strict grammar | difference: members, not the library's layout | native (the library's layout recorded) |
| missing encodings | preserved on raw carriers; `classify_by_sentinel` and the library's classifier agree | parity at the raw layer | native (classifier agreement per value) |
| formulas, globals | not offered as structured I/O (commands only) | parity with the reference's rejection | offline |

## Commands

| Reference | Python | Class | Evidence |
|---|---|---|---|
| `fame`, `@fame_str` | `run_command` | parity for capture, quiet and stream output | native |
| `INPUT` expansion, `.inp` suffix, literal `FILE()` | `expand_input` | parity plus cycle, depth and size limits; computed `FILE()` refused | native |
| output restoration | always restored, stage named on the error | difference: the reference does not restore in a finally block | native |

## Bridge (Bridge.jl)

| Reference | Python | Class | Evidence |
|---|---|---|---|
| `refame`, `unfame` | `bridge.to_fame`, `bridge.from_fame`, `read_value`, `write_value` | parity for every kind | native (every kind, every missing category, empties) |
| integer scalars | exact float64 or refusal | difference: the reference rounds to float32 | offline, native |
| scalar NaN | written as NC | difference: the reference's NaN test never matches | native |
| missing Boolean read as `true` | refused (`MissingValueError`) | difference | native |
| single missing collapsed to empty | `empty="reference"` opt-in only | difference: `preserve` is the default | native |
| string series as bare vector | `StringSeries` keeps the first date | extension | native |
| case (`Unit`) as a date value | refused before any native call | difference: the reference maps it and lets the library refuse (status 16) | native (the library's status asserted) |
| frequency maps: case, daily, business, 7 weekly, monthly, 3 quarterly, 6 half-yearly, 12 annual | `fame_frequency`, `tsecon_frequency`, `mit_to_index`, `index_to_mit` | parity | native (inverse, adjacency, year boundary, period counts, round trips per anchor); differential on Linux |
| other library frequencies | `UnsupportedFrequencyError`, never remapped | parity | native |
| `readfame` (names, wildcards, `namecase`, `prefix`/`glue`, `collect`) | `read_workspace`, `read_workspace_report` | parity; collisions refused instead of overwritten; contained variant is an extension | native |
| `writefame` (workspaces, MVTSeries, prefix/glue) | `write_workspace`, `write_workspace_report` | parity; explicit `mode` required for paths (the reference defaults to overwrite) | native |
| MVTSeries reconstruction on read | not implemented | parity (the reference leaves it commented out) | offline |
| per-object error logging | `*_report` variants with `ObjectFailure` records | extension | native |

## Extensions beyond the reference

| Capability | Python | Evidence |
|---|---|---|
| Consolidated validation runner | `python -m famepy.validation` (ten groups, cross-process manifests, sanitized report) | native on both hosts for the eight groups accepted at 82e7662; `extended_errors` and `migration` added since, pending |
| Julia differential checks | `--julia` group | Linux at 82e7662 (see below) |
| FAME-to-DataEcon migration | `famepy.migration` ([guide](migration.md)) | offline with the installed DataEcon extension; the `migration` group is its native gate, pending |
| Benchmarks and profiling | `python -m famepy.benchmarks` ([guide](benchmarks.md)) | offline harness tests only; no vendor timing yet |

## The tested Julia tree

The Linux campaign at 82e7662 ran its differential checks against a FAME.jl
tree whose hash differs from the pinned reference. A tree comparison shows
the two trees differ only in `README.md` (two added lines); source, build
scripts, package metadata and tests are identical. The campaign's own
report therefore stays *qualified* (it says what it measured against), and
this ledger records source equivalence of the tested tree with the pinned
reference. That equivalence is a statement about those two trees, not
about any later reference revision, and it does not establish dependency
or environment identity.

## Remaining gaps

- Server-connection reads need an approved fixture; writes stay unbound.
- Non-ASCII text: the input boundary is ASCII until the library's encoding
  is established from runtime evidence.
- The `extended_errors` and `migration` groups have not yet run natively.
- No performance figure is published; the harness exists so that the next
  native campaign can produce measured ones.
