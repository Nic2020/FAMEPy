# Capability status

Reference: FAME.jl 0.3.2 at `30586743f1c1bed549841e0309410da0134f3014`.
The package is not yet a functional replacement. This table distinguishes working
foundation features from the remaining port; native verification is outstanding.

| Reference surface | Python status | Evidence or required coverage |
|---|---|---|
| Library discovery and platform handling | Implemented, no-FAME tests | Explicit path precedence, missing libraries, unsupported hosts, lazy loading, redacted errors |
| check_status / HLIError | Implemented as check_status / FameError | Known/unknown signed status, invalid inputs; extended native text deferred |
| Probe failure diagnostics | Implemented | Import/load failure distinction, allowlisted OS numbers, typed missing-function error; no raw native messages |
| CHLI ABI declarations | Candidate inventory only | All 37 reference functions and 15 globals; independent C tests cover pointer status, 64-bit output, range layout, bulk buffers and writable string arrays |
| version, init_chli, close_chli | Planned | Native initialization, version, restart, failed startup/finalization, stale handles |
| FameDatabase, workdb, opendb, postdb, closedb! | Planned | Seven modes, work database, local/remote opening, scoped cleanup, persistence |
| FameObject, Period, quick_info | Planned | Class/type/frequency/range, date/index conversion, 64-bit output, unsupported classes |
| listdb / ITEM filters | Planned | Wildcards, alias/class/type/frequency filters, scalar ranges, long names, cursor cleanup |
| do_read! / do_write | Planned | Numeric/precision/Boolean/frequency-typed date/string scalar and series; namelists; empty/subranges; replacement and creation defaults |
| fame and string macro | Planned as Python command API | Output stream/quiet, recursive INPUT, literal FILE(), suffix rules, error cleanup |
| refame / unfame | Planned | Scalar/series mapping, exactness, missing categories, explicit empty compatibility |
| Bridge frequency maps | Planned | Unit, daily/business, seven weekly endings, monthly, three quarterly, six half-yearly, twelve yearly anchors |
| readfame | Planned | Names/wildcards, namecase, prefix/glue, recursive collect, errors and collisions |
| writefame | Planned | Workspace/MVTSeries flattening, nested prefixes, multiple inputs, persistence and reports |

Formula and global-object class constants do not imply structured formula I/O:
the reference FameObject constructor handles only scalar and series. Arbitrary
FAME commands remain in scope. Frequencies unsupported by the time-series bridge
need raw preservation or an explicit conversion refusal. Generic DATE dispatch
and the reference's empty/missing conventions need native evidence.

The upstream testsets cover workspaces, missing values, frequency interchange,
empty time series and string tuples/namelists. Their deterministic equivalents
will be added with the relevant implementation; none is claimed passed here.
