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

Modes: integers 1-7, names (`readonly`, `create`, `overwrite`, `update`,
`shared`, `write`, `direct_write`) or `famepy.AccessMode` members. Remote
connection strings are passed through unchanged and never appear in `repr`
or error messages.

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
`namelist` (scalar bytes). Scalar reads return exact-width NumPy scalars
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

Listing leaves `ITEM CLASS`, `ITEM TYPE`, `ITEM FREQUENCY` and `ITEM ALIAS`
set to ON afterwards (a documented normalization, not a restoration). The
`frequencies` filter takes exact frequency names or codes (`"monthly"`, `129`,
`["monthly", "case"]`) and is enforced on the listed metadata; `"quarterly"`
alone is refused. A failed command raises `CommandError` whose `stage` says
whether the output redirection, the command itself or the restoration
returned the status; partial output stays on `error.output`.

Extended error text is opt-in. With `session.extended_error_retrieval`
configured from the installed header's declarations, a failure captures the
text immediately and exposes it as `error.extended_text` and
`session.extended_error_text()`, never in the exception message.

## TimeSeriesEconPy bridge (monthly precision)

```python
from tsecon import TSeries, mm
from famepy import bridge

ts = TSeries(mm(2020, 1), [1.0, float("nan"), 3.0])
bridge.write_tseries("synthetic.db", "ts", ts, mode="update")  # posts and closes
back = bridge.read_tseries("synthetic.db", "ts")  # NaN for NC/NA/ND
strict = bridge.read_tseries(db, "ts", missing="strict")  # raises on missing
```

See [contracts](contracts.md) for the missing and empty-series conventions.
