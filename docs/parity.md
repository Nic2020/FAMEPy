# Parity ledger

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
Every exported name and qualified capability of the reference is mapped to
its Python counterpart, classified, and tied to its evidence. Evidence
levels: *offline* (tests against the in-memory fake backend and the
independent C shim), *native* (the consolidated campaign passed on one
Windows and one Linux installation: eight groups at revision 82e7662,
ten at 040fed3 and all eleven at 889d219; the benchmarks completed on
both at 85e46c5), *differential* (the FAME.jl comparisons, on Linux at
82e7662 and 040fed3 and on both hosts at 889d219; see the note on the
tested tree) and *bounded remote* (the same small approved selection
read through both wrappers on both hosts and compared item by item;
reading only).
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
| extended error text (`cfmferr` after status 513) | opt-in `Session.enable_extended_errors()` over the declared `cfmlerr`/`cfmferr` calls, bounded buffer, captured at the failure, never in messages | difference: opt-in and redacted by default | native (`extended_errors` group, both hosts at 040fed3) |

## Databases

| Reference | Python | Class | Evidence |
|---|---|---|---|
| `FameDatabase`, `opendb`, `closedb!` | `Database`, `open_database`, `close` | parity for the five local modes | native (all five modes, stale handles, cross-process) |
| `workdb` | `work_database()` | parity | native |
| `postdb` | `Database.post()`; high-level path writers post on success | parity; close never posts (documented) | native |
| `:write`, `:direct_write` modes | refused before any native call | difference: server-connection modes are not bound by the reference either; this package refuses them ahead of the library | native (the bad-mode status recorded) |
| scoped `opendb(f, ...)` | `with open_database(...)` | parity | native |
| remote connection strings | passed through the local open, as the reference does; the documented route is read-only | parity for reading; remote writing is outside the route in both wrappers | bounded remote: the same approved three-item selection read through both wrappers on both hosts and equal in class, type, frequency, range, length, stored bits, missing categories, normalized values and bridge index (nonempty annual numeric and precision series without missing observations; nothing else was listed or read) |

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
| string values as Julia `String` | `str` under the value text policy: `ascii` (default), `bytes`, or strict `utf-8` | parity for ASCII; extension of the documented policy to UTF-8 values; difference: the reference slices its read buffer by the native byte length on a character index and fails when the last character is multibyte, which is not reproduced | native (`text` group, both hosts at 889d219); differential on both hosts: the reference wrote every corpus label, read back the ASCII, internal-multibyte, ASCII-vector and supplementary-inside labels with equal text, and failed in its read slicing on the terminal-multibyte labels and the mixed vector (recorded as reference limitations); this package read the listed bytes of every reference-written object |
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
| Consolidated validation runner | `python -m famepy.validation` (eleven groups, cross-process manifests, sanitized report) | native on both hosts for all eleven groups at 889d219 |
| Julia differential checks | `--julia` in the `bridge`, `text` and benchmark runs | Linux at 82e7662, 040fed3 and 85e46c5; both hosts at 889d219 for the bridge and text comparisons (see below) |
| FAME-to-DataEcon migration | `famepy.migration` ([guide](migration.md)) | native (`migration` group, both hosts at 040fed3) |
| Benchmarks and profiling | `python -m famepy.benchmarks` ([guide](benchmarks.md)) | all six scenarios warm and cold on both hosts at 85e46c5; Linux reference read-backs verified and nine corruptions rejected; no speed claim |

## The tested Julia tree

The campaigns ran their differential checks (Linux at 82e7662, 040fed3
and 85e46c5; both hosts at 889d219) against a FAME.jl tree whose hash
differs from the pinned reference. A tree comparison shows the two trees
differ only in `README.md` (two added lines); source, build scripts,
package metadata and tests are identical. The campaign reports therefore
stay *qualified* (they say what they measured against), and this ledger
records source equivalence of the tested tree with the pinned reference.
That equivalence is a statement about those two trees, not about any
later reference revision, and it does not establish dependency or
environment identity.

## Reference text decoding

The reference wrapper stores string values as the bytes of Julia strings
and reads them back by slicing its buffer with the byte length the library
reports, on a character index. When the last character of a value is
multibyte, that slice fails with a string index error before any value is
returned, although the bytes are stored intact; ASCII values and values
whose non-ASCII characters are followed by ASCII text read back as valid
strings. This package decodes the whole stored byte sequence and does not
reproduce the failure. On both inspected installations the reference
wrote all nine corpus labels, read back the ASCII value, the
multibyte-then-ASCII value, the ASCII vector and the value with a
supplementary character inside with text equal to what was written, and
raised its string index error on the value ending in a two-byte
character, the three-byte-only value, the value ending in a supplementary
character and the vector containing one; this package's raw reads of
every one of those objects returned the listed bytes and its `utf-8`
reads the expected text. Which byte sequences the library itself accepts
or transforms is not established beyond that synthetic corpus, and no
Unicode object name, command or remote text value was tested.

## Remaining qualifications

- The remote evidence is one approved selection of three nonempty annual
  numeric and precision series without missing observations on one
  read-only route per host; missing categories, other types, string
  values, listing and other servers were not exercised over a route.
- The UTF-8 evidence is the fixed synthetic corpus under the explicit
  policy; it does not establish every encoding, Unicode object names or
  remote text values.
- The FAME.jl comparisons ran against the README-only different tree on
  both hosts (qualified, see above).
- No performance figure is published from the benchmark numbers; the
  date conversion phases are the recorded profiling candidate (see
  [benchmarks](benchmarks.md)).
