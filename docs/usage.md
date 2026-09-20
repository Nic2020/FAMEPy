# Usage

Everything below runs against a separately installed, licensed FAME. Set the
`FAME` environment variable to the installation (the library requires it for
licensing) or pass an absolute library path to `init_chli`. The public names
are those of [FAME.jl](https://github.com/bankofcanada/FAME.jl), with three
adaptations Python forces: names that end in `!` lose the mark (`closedb`,
`do_read`), the `class` keyword of `listdb` and `readfame` is spelled
`class_`, and the reference's do-block forms are context managers. Names,
paths and commands must be ASCII `str` values or NUL-free `bytes` in this
release; string *values* are ASCII by default and can be exchanged as raw
bytes or, opt-in, as strict UTF-8 (see below).

## Runtime

```python
import famepy

session = famepy.init_chli()  # loads CHLI and initializes FAME, once per process
print(famepy.version())  # library version as a float
famepy.close_chli()  # terminal for this process; handles become stale
```

`init_chli` is idempotent while active. Unlike the reference, it does not
restart the library: CHLI initializes once per process and finalization is
terminal, so after `close_chli()` nothing in the same process can initialize
again (not another session, not a new wrapper, not another library), and
`famepy.reset()` raises `UnsupportedOperationError` without touching the
runtime. Start a spawned process for a fresh runtime; a runtime inherited
across `fork` is refused. A failed native initialization is terminal too and
keeps the numeric status on the `HLIError`; only checks that happen before
the native call (such as a missing `FAME` environment variable) leave the
session retryable. The library is fixed for the process once loaded.

## Databases

```python
db = famepy.opendb("synthetic.db", "create")  # read-only is the default mode
famepy.do_write(famepy.refame("x", 1.5), db)
famepy.postdb(db)  # closing never posts
famepy.closedb(db)  # returns db; closing twice is harmless

with famepy.opendb("synthetic.db") as db:  # the reference's do-block form
    obj = famepy.quick_info(db, "x")
    print(obj.kind, obj.frequency_label, famepy.do_read(obj, db).data)

work = famepy.workdb()  # the process work database, opened once
```

Modes: integers 1-5, names (`readonly`, `create`, `overwrite`, `update`,
`shared`) or `famepy.AccessMode` members. `write` and `direct_write` (6 and
7) exist for parity with the reference but belong to a database opened on a
named server connection, which this release does not bind; they raise
`UnsupportedOperationError` before any native call. Remote connection
strings are passed through unchanged and never appear in `repr` or error
messages. A `FameDatabase` handle is returned by `opendb` and `workdb`;
leaving its `with` block closes it without posting.

## Named objects

```python
import numpy as np

obj = famepy.FameObject("s", "series", "precision", "monthly", first_index, data=values)
famepy.do_write(obj, db, replace=True)

back = famepy.do_read(famepy.quick_info(db, "s"), db)  # data: an owning float64 array
codes = famepy.classify_by_sentinel(back.data, "precision", db.session.sentinels)
part = famepy.FameObject("s", "series", "precision", "monthly", first_index + 1, first_index + 2)
famepy.do_read(part, db)  # a subrange, selected by the object's range
```

A `FameObject` carries the name, class (`series` or `scalar`), type, index
frequency, range and data, as the reference's does. The type is a value kind
(`precision` float64, `numeric` float32, `boolean` int32 codes, `string`
bytes, `namelist` scalar bytes) or, for date values, the frequency of the
dates; a scalar has the `undefined` frequency. `quick_info` and `listdb`
return objects without data, `do_read(obj, db)` fills the object's data in
place and returns it, `do_write(obj, db)` creates the object and writes its
data, and `refame`/`unfame` convert to and from TimeSeriesEconPy values. The
data is the value itself: an exact-width NumPy scalar (`numpy.float32` for
numeric objects) or `bytes` for a scalar, an exact-dtype array or a list of
`bytes` for a series; `famepy.namelist_members(obj.data)` gives a namelist's
ordered members, since the library may lay the list text out differently
from what was written. Missing NC/NA/ND encodings are preserved; buffers
must already have the exact dtype, native byte order and contiguity. The
caller's array is never modified. Every input check runs before the first
native call, so an invalid object never deletes or creates anything.

`do_read` re-queries the object's metadata inside the locked read: an object
whose stored class, type or frequency changed since it was described is
refused (query it again) rather than read as something else, and the
object's range selects a subrange that must lie inside the stored one.
`do_write` replaces an existing object by default, matching the reference.
Use `replace=False` to refuse an existing name and preserve its stored data.
Replacement deletes the old object first; that deletion is not undone if
the following native write fails. `basis` and `observed` attributes accept members, codes or names.
`famepy.delete_object(db, name)` is the package's own spelling of the
deletion the reference performs inside `do_write`.

## Listing and commands

```python
objects = famepy.listdb(db, "sales?", class_="series", freq="monthly")
objects = famepy.listdb("synthetic.db")  # a path opens read-only and closes
output = famepy.fame("display 2+2")  # bytes of captured output
famepy.fame("input setup", base_dir=".", quiet=True)  # recursive INPUT expansion
```

`listdb` takes the reference's `alias`, `class_`, `type` and `freq` filters
as comma-separated strings or sequences (`""` means no filter) and returns
`FameObject` entries without data. Listing leaves `ITEM CLASS`, `ITEM TYPE`,
`ITEM FREQUENCY`, `ITEM INDEX` and `ITEM ALIAS` set to ON afterwards (a
documented normalization, not a restoration). The `freq` filter takes exact
frequency names or codes (`"monthly"`, `129`, `"monthly,case"`) and is
enforced on the listed metadata; a family word such as `"quarterly"` alone
is refused rather than interpreted. The library's family and index words
narrow the native listing only where they cannot exclude a requested object.
`fame` returns the captured output (the reference prints it); `output=`
takes a binary stream, as the reference's `fame(io, command)` does, and
there is no string-macro form. A failed command raises `CommandError`
(an `HLIError`) whose `stage` says whether the output redirection, the
command itself or the restoration returned the status; partial output stays
on `error.output`.

Extended error text is opt-in because it can contain private command text
or identifiers. After `session.enable_extended_errors()` a failure captures
the text immediately (sized by the library's own length call) and exposes
it as `error.extended_text` and `session.extended_error_text()`, never in
the exception message.

## TimeSeriesEconPy values

```python
from tsecon import TSeries, qq
from famepy import bridge

ts = TSeries(qq(2020, 1), [1.0, float("nan"), 3.0])
famepy.do_write(famepy.refame("ts", ts), db)  # NaN is written as NC
back = famepy.unfame(famepy.do_read(famepy.quick_info(db, "ts"), db))  # NaN for NC/NA/ND
strict = famepy.unfame(famepy.do_read(famepy.quick_info(db, "ts"), db), missing="strict")

famepy.do_write(famepy.refame("when", qq(2021, 3)), db)  # a date scalar
famepy.do_write(famepy.refame("names", bridge.NameList(["a", "b"])), db)
famepy.do_write(famepy.refame("text", bridge.Text("{not a list}")), db)
famepy.do_write(famepy.refame("labels", ["x", "y"]), db)  # a case string series
famepy.unfame(famepy.do_read(famepy.quick_info(db, "when"), db))  # -> MIT

famepy.do_write(famepy.refame("note", accented, text="utf-8"), db)  # str stored as UTF-8
note = famepy.do_read(famepy.quick_info(db, "note"), db)
famepy.unfame(note, text="utf-8")  # -> the same str
famepy.unfame(note, text="bytes")  # -> the stored bytes
famepy.unfame(note)  # TextEncodingError: not ASCII
```

`refame(name, value)` keeps the reference's argument order and returns a
`FameObject` ready for `do_write`; `unfame(obj)` converts an object that
holds data. Every reference frequency anchor is supported as an index
(case, daily, business, the seven weekly endings, monthly, three quarterly,
six half-yearly and twelve annual anchors); other library frequencies raise
`UnsupportedFrequencyError`. Date *values* carry a calendar frequency: a
case moment is refused as a date scalar or `DateSeries` observation
(`DataValidationError`), because the library does not type objects by the
case frequency; write numeric data for case numbers. Object names must be
legal for the library: a reserved word (for example `NAMELIST`) is refused
by the library with status 25. Values of every kind convert as listed in
[contracts](contracts.md): dates become `MIT`, date series
`bridge.DateSeries`, string series `bridge.StringSeries`, name-lists
`bridge.NameList`.

String values are ASCII by default. `text="bytes"` reads the stored bytes
unchanged, and `text="utf-8"` writes `str` values as strict UTF-8 and
decodes stored values strictly as UTF-8 on `refame`, `unfame`, `readfame`
and `writefame`; a value that cannot be encoded, an embedded NUL or stored
bytes that are not UTF-8 raise `TextEncodingError` (a `*_report` variant
records it per object). The policy applies to values only: object names,
namelist members, paths and commands stay ASCII. Which text the library
itself accepts is a property of the installation; the
[parity ledger](parity.md) records what has been observed and the `text`
validation group is the native gate.

## Workspaces

```python
from tsecon import Workspace

w = Workspace(a=1.0, b=ts, c=Workspace(alpha=0.1, n=Workspace(s="Hello")))
famepy.writefame("synthetic.db", w, mode="overwrite")  # A, B, C_ALPHA, C_N_S

everything = famepy.readfame("synthetic.db")  # keys a, b, c_alpha, c_n_s
nested = famepy.readfame("synthetic.db", collect=[("c", ["n"])])  # c.alpha, c.n.s
some = famepy.readfame("synthetic.db", "a", "c?", prefix="c")  # a, alpha, n_s
series = famepy.readfame("synthetic.db", "?", class_="series", freq="monthly")

report = bridge.readfame_report("synthetic.db", raw_fallback=True)
report.workspace, report.failures, report.raw, report.complete
```

`writefame` takes workspaces, mappings and multivariate series (also one
tuple of them, as the reference does) and, for a path, an explicit `mode`:
the reference defaults a path to overwrite, this package asks. A path is
posted on success and always closed; a `FameDatabase` handle is never
posted here. `readfame` reads explicit names and wildcards, filtered by the
`listdb` keywords, and shapes the keys with `prefix`/`glue`, `collect` and
`namecase` (lower case by default); a transformation that would make two
objects share a key is refused before anything is read. The strict
functions raise at the first failure, where the reference logs and skips
the object; `bridge.readfame_report` and `bridge.writefame_report` contain
failures per object and return the partial result with every failure
listed. Multivariate series are written as one series per column and read
back as separate series. See [contracts](contracts.md) for the missing,
empty and text policies and the deliberate differences from the reference.

## Retiring a database into DataEcon

```python
from famepy import migration
from tsecon.dataecon import open_dataecon

plan = migration.plan_migration("synthetic.db")  # read-only, nothing written
print(plan.summary())  # refusals, skips, losses
report = migration.migrate("synthetic.db", "archive.daec", plan=plan)
print(report.summary())  # per object; omissions listed
with open_dataecon("archive.daec") as db:
    migration.migration_status(db)  # layout, complete/incomplete, counts
    migration.read_migrated(db, "ts")  # back in bridge terms
```

The defaults refuse every representable loss: an existing destination (a
migration only ever creates a new file), an unsupported object, a name
collision, an invalid first date and a stale plan stop it before anything
is created, and an unrepresentable value marks the archive `incomplete`;
missing categories travel in sidecar masks, and reading back verifies the
layout structurally. Metadata the bound calls cannot retrieve is listed
as omitted.
See the [migration guide](migration.md) for the fidelity matrix, the
policies and what does not travel (aliases, BASIS/OBSERVED, descriptions,
timestamps, formulas). `examples/retire_synthetic.py` runs the workflow on
synthetic data.

## Benchmarks

`python -m famepy.benchmarks --native --scratch <new-dir> --report bench.json`
times conversion, native writes, posting, reads and the workspace forms on
synthetic data, one worker process per measurement, verifying every read
back and recording native call counts and memory from a separate
instrumented pass; see [benchmarks](benchmarks.md). Without a real library
the report says the numbers are not vendor timings; a failed measurement
gives exit status 1.
