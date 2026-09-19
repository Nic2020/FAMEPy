# Usage

Everything below runs against a separately installed, licensed FAME. Set the
`FAME` environment variable to the installation (the library requires it for
licensing) or pass an absolute library path to `initialize`. Names, paths and
commands must be ASCII `str` values or NUL-free `bytes` in this release.

## Runtime

```python
import famepy

session = famepy.initialize()  # one initialized owner per process, once
print(famepy.version())  # library version as a float
famepy.finalize()  # terminal for this process; handles become stale
```

`initialize` is idempotent while active. CHLI initializes once per process
and finalization is terminal: after `finalize()` nothing in the same process
can initialize again (not another session, not a new wrapper, not another
library), and `famepy.reset()` raises `UnsupportedOperationError` without
touching the runtime. Start a spawned process for a fresh runtime; a runtime
inherited across `fork` is refused. A failed native initialization is
terminal too and keeps the numeric status on the `FameError`; only checks
that happen before the native call (such as a missing `FAME` environment
variable) leave the session retryable. The library is fixed for the process
once loaded.

## Databases

```python
with famepy.open_database("synthetic.db", "create") as db:  # read-only is the default mode
    famepy.write_object(db, "x", famepy.scalar("precision", 1.5))
    db.post()  # closing never posts

with famepy.open_database("synthetic.db") as db:
    info = famepy.quick_info(db, "x")
    print(info.kind, info.frequency_label, famepy.read_object(db, "x").value)

work = famepy.work_database()  # the process work database, opened once
```

Modes: integers 1-5, names (`readonly`, `create`, `overwrite`, `update`,
`shared`) or `famepy.AccessMode` members. `write` and `direct_write` (6 and
7) exist for parity with the reference but belong to a database opened on a
named server connection, which this release does not bind; they raise
`UnsupportedOperationError` before any native call. Remote connection
strings are passed through unchanged and never appear in `repr` or error
messages.

## Raw objects

```python
import numpy as np

values = np.array([1.0, 2.0, np.nan])
raw = famepy.series("precision", "monthly", first_index, values)
famepy.write_object(db, "s", raw, replace=True)

back = famepy.read_object(db, "s")  # RawSeries with an owning float64 array
codes = famepy.classify_by_sentinel(back.values, "precision", db.session.sentinels)
part = famepy.read_object(db, "s", first_index=first_index + 1, last_index=first_index + 2)
```

Kinds: `precision` (float64), `numeric` (float32), `boolean` (int32 codes),
`date` (int64 indices plus the value frequency), `string` (bytes) and
`namelist` (scalar bytes; `famepy.namelist_members(value)` gives the ordered
members, since the library may lay the list text out differently from what
was written). Scalar reads return exact-width NumPy scalars
(for example `numpy.float32` for numeric objects). Missing NC/NA/ND
encodings are preserved; buffers must already have the exact dtype, native
byte order and contiguity. The caller's array is never modified. Every
input check runs before the first native call, so an invalid value never
deletes or creates anything. `replace=True` deletes an existing object
first; that deletion is not undone if the following native write fails.

## Listing and commands

```python
infos = famepy.list_objects(db, "sales?", classes="series", frequencies="monthly")
output = famepy.run_command("display 2+2")  # bytes of captured output
famepy.run_command("input setup", base_dir=".", quiet=True)  # recursive INPUT expansion
```

Listing leaves `ITEM CLASS`, `ITEM TYPE`, `ITEM FREQUENCY`, `ITEM INDEX` and
`ITEM ALIAS` set to ON afterwards (a documented normalization, not a
restoration). The `frequencies` filter takes exact frequency names or codes
(`"monthly"`, `129`, `["monthly", "case"]`) and is enforced on the listed
metadata; `"quarterly"` alone is refused. The library's family and index
words narrow the native listing only where they cannot exclude a requested
object. A failed command raises `CommandError` whose `stage` says
whether the output redirection, the command itself or the restoration
returned the status; partial output stays on `error.output`.

Extended error text is opt-in. With `session.extended_error_retrieval`
configured from the installed header's declarations, a failure captures the
text immediately and exposes it as `error.extended_text` and
`session.extended_error_text()`, never in the exception message.

## TimeSeriesEconPy bridge

```python
from tsecon import TSeries, Workspace, mm, qq
from famepy import bridge

ts = TSeries(qq(2020, 1), [1.0, float("nan"), 3.0])
bridge.write_tseries("synthetic.db", "ts", ts, mode="update")  # posts and closes
back = bridge.read_tseries("synthetic.db", "ts")  # NaN for NC/NA/ND
strict = bridge.read_tseries(db, "ts", missing="strict")  # raises on missing

bridge.write_value(db, "when", qq(2021, 3))  # a date scalar
bridge.write_value(db, "names", bridge.NameList(["a", "b"]))
bridge.write_value(db, "text", bridge.Text("{not a list}"))
bridge.write_value(db, "labels", ["x", "y"])  # a case string series
bridge.read_value(db, "when")  # -> MIT
```

Every reference frequency anchor is supported as an index (case, daily,
business, the seven weekly endings, monthly, three quarterly, six half-yearly
and twelve annual anchors); other library frequencies raise
`UnsupportedFrequencyError`. Date *values* carry a calendar frequency: a case
moment is refused as a date scalar or `DateSeries` observation
(`DataValidationError`), because the library does not type objects by the
case frequency; write numeric data for case numbers. Object names must be
legal for the library: a reserved word (for example `NAMELIST`) is refused
by the library with status 25.
Values of every kind convert as listed in [contracts](contracts.md): dates
become `MIT`, date series `bridge.DateSeries`, string series
`bridge.StringSeries`, name-lists `bridge.NameList`.

## Workspaces

```python
w = Workspace(a=1.0, b=ts, c=Workspace(alpha=0.1, n=Workspace(s="Hello")))
bridge.write_workspace("synthetic.db", w, mode="overwrite")  # A, B, C_ALPHA, C_N_S

everything = bridge.read_workspace("synthetic.db")  # keys a, b, c_alpha, c_n_s
nested = bridge.read_workspace("synthetic.db", collect=[("c", ["n"])])  # c.alpha, c.n.s
some = bridge.read_workspace("synthetic.db", "a", "c?", prefix="c")  # a, alpha, n_s

report = bridge.read_workspace_report("synthetic.db", raw_fallback=True)
report.workspace, report.failures, report.raw, report.complete
```

Names are transformed by `prefix`/`glue`, `collect` and `namecase` (lower
case by default); a transformation that would make two objects share a key
is refused before anything is read. The strict functions raise at the first
failure; the `*_report` variants contain failures per object and return the
partial result with every failure listed. Multivariate series are written as
one series per column and read back as separate series. See [contracts](contracts.md)
for the missing, empty and text policies and the deliberate differences from
the reference.
