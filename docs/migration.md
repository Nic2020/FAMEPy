# Retiring a FAME database into DataEcon

`famepy.migration` copies the scalar and series objects of a FAME database
into a new DataEcon file (the TimeSeriesEconPy interchange format) through
FAMEPy's own readers and the public `tsecon.dataecon` writers. It is a
*data* migration with explicit loss policies: what travels, what is refused
and what is left behind is decided from a plan before the destination is
created, reported per object afterwards, and verifiable from the archive
alone.

```python
import famepy
from famepy import migration
from tsecon.dataecon import open_dataecon

famepy.initialize()
plan = migration.plan_migration("source.db")  # source opened read-only
print(plan.summary())  # refusals, skips, losses
report = migration.migrate("source.db", "archive.daec", plan=plan)
print(report.summary())
assert report.complete

with open_dataecon("archive.daec") as db:
    print(migration.migration_status(db))  # layout, status, counts
    for name in migration.list_migrated(db):
        back = migration.read_migrated(db, name)  # bridge terms, layout verified
```

`examples/retire_synthetic.py` runs the whole workflow on a synthetic
database and verifies the archive against the values it wrote.

## Plan first: a metadata preview

`plan_migration(source, patterns=("?",), options=None)` lists the objects
(without aliases) and decides from metadata alone, for each one, whether it
can be stored under the options, which DataEcon name it gets and which
losses the policies may cause. Nothing is written and no value is read:
the plan is a preview of the *decisions*, not a validation of the data.
Values are read and converted during the run, so a conversion that fails
on the data itself (for example a string with non-ASCII bytes) is a
contained per-object failure of the run, not a refusal of the plan. The
plan refuses:

- formulas, global objects and any other class the structured reader does
  not support;
- series whose index frequency, or date objects whose value frequency, is
  outside the bridge set (intraday, biweekly, ten-day, ... are raw codes
  only);
- destination names that are invalid for DataEcon or equal the mask catalog
  name, and two objects that land on one destination name after
  `namecase` (both are refused);
- an explicit first date supplied for a nonempty object, or one whose
  frequency is not the empty series' frequency;
- an empty series when `empty="refuse"` and no explicit first date was
  supplied.

With `unsupported="skip"` the first two become *skips*: they are recorded in
the plan and in the report, and the archive is marked `incomplete`. Nothing
is discarded silently. The plan records the patterns it was built from; a
plan handed to `migrate` is checked again against the source's current
metadata and refused when the source changed since (objects added, removed
or changed), so a stale plan is never taken as permission.

## Run: one new file per migration

`migrate(source, destination, patterns=..., options=..., plan=...)` checks,
in this order, before anything is created:

1. the destination must not exist (there is no append mode: a migration
   only ever creates a new file) and must not be the source database;
2. the plan (built here when not given, otherwise re-checked against the
   source) has no refused entry, otherwise `MigrationRefused` names them.

Only then is the destination path *claimed* (created exclusively; a file
that appeared in the meantime is refused, never opened) and the archive
written to a partial file next to it, `<destination>.<id>.partial`. The
partial file's catalog receives the layout version, a `started` mark and
the planned count; the source is opened read-only; each object is read,
converted and stored in its own contained step (a failure is recorded with
its error class and status and the others continue); the catalog is marked
`complete` only when every planned object was stored and nothing was
skipped, `incomplete` otherwise; and the finished file is moved onto the
claim. The mark and the counts are readable with `migration_status`, so a
partial archive is never mistaken for a complete one. A run that stops
before the move (an interrupted process, an error outside the per-object
containment) leaves the partial file with its mark and releases the claim;
the destination then does not exist and the partial file is the evidence.

Because an archive is produced by exactly one run, a later run can neither
add to it, replace objects in it, nor turn an `incomplete` archive into a
`complete` one: any existing file at the destination path is refused and
left byte-identical, whatever it contains. To migrate more objects, build
another archive. The source is never posted or modified.

## Fidelity matrix

Every object is stored under its own name with attributes whose names start
with `famepy.migration.` (layout version 1). Missing observations keep their
category (NC, NA or ND) in a sidecar `int8` series of the same axis inside
the catalog `famepy_migration_masks`; a missing scalar keeps its category in
the `missing` attribute. The default policy (`missing="mask"`) preserves
every supported value and category; the explicit `missing="nan"` policy
collapses floating missing observations to NaN without a mask (a documented
loss reported per object) and refuses Boolean, date and string objects with
missing observations. Missing-value *bit patterns* (the library's sentinels)
are not kept in either policy; the categories are. The metadata a data
migration cannot carry (see below) is lost under every policy, defaults
included, and is listed in every plan and report.

| FAME object | DataEcon object | attributes / sidecars | read back as |
|---|---|---|---|
| precision scalar | Float64 scalar (NaN when missing) | `missing` = NC, NA or ND | `float` or `None` |
| numeric scalar | Float32 scalar | `missing` | `numpy.float32` or `None` |
| Boolean scalar | Int8 scalar (0 when missing) | `missing` | `bool` or `None` |
| date scalar | MIT scalar; Int64 zero when missing | `value_frequency`, `missing` | `MIT` or `None` |
| string scalar | string scalar (empty when missing) | `missing` | `str` or `None` |
| namelist scalar | text vector of the ordered members | `representation` = `members` | `NameList` |
| precision / numeric series | float64 / float32 `TSeries` (NaN at missing) | `frequency`; mask when any missing | `TSeries` |
| Boolean series | bool `TSeries` (False at missing) | `frequency`; mask when any missing | `TSeries` |
| date series, no missing | `StoredSeries` of `MIT` elements | `frequency`, `value_frequency` | `DateSeries` |
| date series with missing | int64 `TSeries` of moment codes (0 at missing) | `representation` = `codes`, mask | `DateSeries` |
| string series | text vector (`""` at missing) | `frequency`, `firstdate`, mask | `StringSeries` |
| empty series, any kind | zero-length array of the kind's dtype | `frequency`, `empty` = `unknown_firstdate` | `None` (empty) |
| empty series, explicit first date | typed empty series | `empty` = `explicit_firstdate` | empty carrier |

Index frequencies: every bridge frequency, including the case frequency
(`Unit`, stored as a DataEcon Unit axis) and every weekly, quarterly,
half-yearly and annual anchor. Moment codes stored as values or attributes
are TimeSeriesEconPy's own `MIT` integers, never FAME indices; frequency
labels are FAME frequency names (`monthly`, `weekly_sunday`, ...). Normal
floating values keep their exact bits (float64 and float32 are stored at
their own width). Integer-valued FAME objects do not exist; nothing is
widened or narrowed.

An empty FAME series has no stored first date. The layout keeps it unknown
unless the caller supplies one per object (`empty_firstdates={"NAME":
mit}`, checked against the series' frequency in the plan); no date is ever
synthesized. A date series with missing observations cannot be a
`StoredSeries` of moments without inventing a moment for the gaps, so it is
stored as moment codes plus the mask and reads back as the same
`DateSeries` with `None` at the gaps.

Text crosses the default (ASCII) value text policy of the bridge; the
migration takes no `text` option, so a string with non-ASCII bytes, whether
it was written under the bridge's opt-in `utf-8` policy or by another
writer, fails as a contained per-object `TextEncodingError` and the archive
is marked `incomplete`. Retiring such values is not offered until the
archive layout records their encoding; the raw bridge reads remain
available for them.

## Names, metadata and scope

- Names: FAME names are upper-case; the default `namecase=str.lower` gives
  lower-case DataEcon names. DataEcon names are case-sensitive, so a
  different `namecase` is checked for collisions in the plan. The
  destination `catalog` is an absolute path without empty components or a
  trailing slash and without the reserved mask catalog name; it is created
  inside the new file.
- Aliases: the source is listed with aliases off. An alias is a second name
  for the same object and is not migrated as a second object; the report
  says so.
- BASIS and OBSERVED, descriptions, documentation, user attributes,
  creation and modification timestamps: not retrievable through the calls
  this release binds, so they do not travel. The report's `omissions` list
  states this; it is not silent. This is not a full FAME archive.
- Formulas, global objects and interpreter state: not part of a data
  migration; they remain reachable through FAME commands only.
- Frequencies outside the bridge set: refused or skipped, never remapped.

The migration report (`MigrationReport.summary()`) is meant for the person
running the migration and names objects, reasons and error classes. The
validation runner's own migration group exports only fixture-derived
descriptions, never a user's object names or values.

## Verify: reading is structural checking

`read_migrated(db, name)` returns a `MigratedObject` in bridge terms (kind,
value, per-observation categories, frequency labels, representation) and
verifies the layout while doing so. The catalog must carry this layout
version and a defined status; the object's attributes must be exactly the
ones the layout defines for its representation, with defined values
(an unknown attribute, missing tag, empty mode, mask value or
representation is an error, never ordinary data); and the stored object
must agree with them: the DataEcon class (scalar, series, array), the
dtype, the length (an empty carrier is genuinely zero-length), the index
frequency of the carrier, the element frequency of stored dates, the
placeholders at tagged-missing positions (NaN, `False`, `0`, `""`), and
the mask's dtype, frequency, first date, length and codes. Any
disagreement is a `LayoutError` naming it. A `MigratedObject` cannot even
be constructed with a label its carrier contradicts, so `describe()` never
repeats metadata the stored object does not have.

`describe()` reduces a `MigratedObject` to a canonical, JSON-safe
description with exact bits and codes. `expected_object(name, value, ...)`
builds the object an exact migration produces from a bridge value you hold
independently of FAME, so `describe(read_migrated(...)) ==
describe(expected_object(...))` is the verification. A successful open
proves nothing by itself: compare descriptions object by object, and check
`migration_status` for `complete`.

The validation runner's `migration` group does this natively: it writes a
fixture of every kind and category to a synthetic FAME database, migrates
it, reads the archive back in the same process and in a fresh process, and
asserts the negative cases (existing destinations untouched, refused plan,
collision, invalid first date and stale plan create nothing, lossy policy
refusals, contained failure marked incomplete, an incomplete archive never
completed by a later run, and four structural corruptions detected in-process
and cross-process). When the DataEcon native extension is unavailable the
group is *blocked*, not passed. See [native validation](native-validation.md).

## Options

| Option | Values | Default | Effect |
|---|---|---|---|
| `namecase` | callable | `str.lower` | DataEcon name of a FAME name; collisions are refused |
| `missing` | `mask`, `nan` | `mask` | categories in sidecars (lossless) or NaN collapse (lossy, floating kinds only) |
| `unsupported` | `refuse`, `skip` | `refuse` | stop before creating anything, or record and mark incomplete |
| `empty` | `carrier`, `refuse` | `carrier` | zero-length carrier without a first date, or refuse |
| `empty_firstdates` | mapping | `{}` | explicit first dates per FAME name for empty series (frequency checked in the plan) |
| `catalog` | path | `/` | destination catalog inside the new file (validated before anything is created) |

No production database is migrated by the tests or the example; every
scratch output is synthetic.
