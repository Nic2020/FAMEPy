# API and data contracts

The public names are FAME.jl's: `init_chli`, `close_chli`, `version`,
`check_status`, `HLIError`, `FameDatabase`, `opendb`, `workdb`, `postdb`,
`closedb`, `FameObject`, `quick_info`, `listdb`, `do_read`, `do_write`,
`fame`, `refame`, `unfame`, `readfame` and `writefame`. Python forces three
adaptations: the `!` of `closedb!` and `do_read!` is dropped, the `class`
filter keyword is `class_`, and do-block forms are context managers.
Everything else named here is an extension without a reference spelling.

## Runtime and diagnostics

Import never loads CHLI. `diagnose()` returns a dictionary with a schema
version, Python/dependency versions, platform, candidate ABI layout and
discovery status. `diagnose(probe=True)` loads the trusted library in a timed
subprocess and checks symbol presence without calling CHLI. Schema version 3
adds the `presence_only` map (empty since every inventoried symbol is
declared) and `trusted_root_known`. Exit zero means only `library_found` or
`symbols_found`; neither means the ABI or a database operation was validated.
See [native validation](native-validation.md).

Path precedence is an explicit argument, then `FAMEPY_LIBRARY`, then `FAME`.
Discovery through `FAME` records the installation root as a trusted directory;
an explicit library can name one with `root=` (`--root` on the command line).
The root travels with the library path to the probe child and to the
validation runner, so every loader registers the same directories. On
Windows both the library directory and the root are registered as DLL search
directories for the process lifetime; `PATH` is never modified.
Initialization requires the `FAME` environment variable because the library
needs it for licensing.

The runtime is one-shot per process. CHLI initializes once, and finalization
is the last native call the process makes; a spawned process is the supported
fresh-runtime boundary. `init_chli()` returns the process default session
and is idempotent while active; unlike the reference's `init_chli`, it
never restarts the library. Exactly one session may initialize per
process, and once any session has attempted `cfmini` no other session, no
new wrapper over the same library and no other library candidate can
initialize in that process: every such attempt raises `RuntimeStateError`
before any native call. The native library is fixed for the process
lifetime once loaded: no unload is attempted, and choosing a different
library or root after a load raises `RuntimeStateError`.

States are `created` -> `loaded` -> `initialized` -> `finalized`, plus two
other terminal states: `failed` when `cfmini` failed or was interrupted (the
package does not assume a failed native initialization can be retried), and
`broken` when `cfmfin` failed or was interrupted. Python-level checks that run before
`cfmini` (the licensing environment, ownership) leave the session `loaded`
and retryable because nothing native was consumed. When setup fails after a
successful `cfmini` (for example the sentinel globals cannot be read), one
`cfmfin` is attempted as the terminal call, its status is recorded on
`finalize_status` (or remains `None` if no status was returned), and the original
setup error is what propagates; the
session ends `finalized` or `broken`, never reusable. `cfmfin` is issued at
most once per process: a failed finalization is not retried, and calling
`close_chli()` in any terminal or never-initialized state is a harmless
Python-level no-op. `close_chli()` closes tracked databases first (recording
any close statuses on `last_cleanup_statuses`) and invalidates every handle;
a handle used after finalization raises `StaleHandleError`. `generation` is 0
before the single initialization and 1 after it.

`reset()` (module-level and `Session.reset`) is not supported: it raises
`UnsupportedOperationError` before finalizing or touching anything, whatever
the state. Any use of a session after `fork`, including creating a new
session in the child, raises `InheritedRuntimeError`.

Every operation holds one process-wide reentrant lock for its whole duration,
and every operation on a database handle validates the handle (open, runtime
still initialized) inside that lock, immediately before the native call. A read
obtains metadata and data in one locked operation, so a concurrent close, key
reuse or object replacement cannot slip between validation and the call. Work
databases, ITEM options, wildcard cursors, output redirection and the extended
error state are process-global inside the library, so there is no
parallel-thread throughput promise and no public arbitrary-native-call API.

`check_status(0)` returns normally; other signed 32-bit integers raise
`HLIError` with the original code. Default errors never include native text.
Extended error text is opt-in: `session.enable_extended_errors()` (or an
explicit `ExtendedErrorRetrieval` on `session.extended_error_retrieval`)
turns it on. The session then reads the text at the failure itself, under
the same lock and before any other native call (for commands, before the
output redirection is restored): the declared length call sizes an owned
buffer (bounded at 64 KiB; a larger reported length is refused without
allocating) which the declared fetch call fills and the library truncates
to. The text is attached as `extended_text` on the raised `HLIError` and
`session.extended_error_text()` returns the text captured by the most
recent failure; it never enters an exception message, because it can carry
private command text or identifiers. A retrieval that fails never masks the
status; its class name is kept on `extended_error_capture_failure`. With
retrieval off (the default) `extended_error_text()` raises
`UnsupportedOperationError`. The length call's declaration is the older
convention recorded on both inspected installations; the `extended_errors`
validation group is its native gate.

## Text

No vendor encoding has been established. The native layer exchanges bytes.
Text arguments that the library documents as both input and output (the
database name of the local open, option names and values, object names of
the older calling convention, namelist text) may be trimmed or upper-cased
in place by the library, so the binding never hands them the caller's
immutable bytes: each such argument is an owned NUL-terminated copy that
lives for the call and is discarded afterwards. The caller's bytes, and any
dictionary keyed by them, are never modified. Input-only text (commands,
the newer functions' `const` names) is passed as given.
`str` input must be ASCII and is rejected otherwise; `bytes` pass through
without NUL bytes. Returned names are bytes with an ASCII `name_text` view
that raises on non-ASCII content. This boundary applies to object names,
namelist members, database and connection strings, option values and
commands, and no value policy widens it.

String *values* (string scalars and string series observations) are bytes
in the raw layer and are converted only by the bridge, under an explicit
value text policy: `ascii` (the default) and `bytes` keep the boundary
above, and `utf-8` encodes `str` input strictly as UTF-8 and decodes
stored bytes strictly as UTF-8. Every policy refuses an embedded NUL, a
lone surrogate and stored bytes that are not the selected encoding
(`TextEncodingError`); nothing is replaced, escaped or guessed, and a
policy never makes a claim about how the library interprets the bytes.
The library's string missing sentinels are not ASCII text (two bytes each
on both inspected installations); a missing observation is classified by
its sentinel bytes before any decoding, so a sentinel is never decoded
under any policy. The validation runner exports string evidence only for
values it constructed itself (as printable ASCII or bounded hex); bytes
that differ from a fixture are reported by length.

## Databases

`opendb(dbname, mode="readonly")` returns a `FameDatabase`. It accepts the seven reference modes as
integers, names or `AccessMode` members, and opens the five local ones:
read-only, create, overwrite, update and shared. `write` and `direct_write`
are modes of a database opened on a named server connection through a
different open function (with write-server prerequisites of its own) that
neither this package nor the reference binds; the local open rejects them
with the bad-mode status, which is what every campaign observed. They are
therefore refused with `UnsupportedOperationError` before any native call,
never remapped to another mode. The constants remain for parity with the
reference table. Closing never posts (the package issues no post
on close; what the library does with unposted updates on close is recorded
by the campaign as an observation); `postdb(db)` is explicit, and
`closedb(db)` returns the handle as the reference's `closedb!` does.
Closing twice is a no-op. If the native close fails, the handle stays open and
tracked: the status propagates, `closedb` can be retried, and `close_chli()`
still attempts the close and records its status. `writefame`, `readfame`
and `listdb` given a path open the database themselves, post after a
successful write and always close. No rollback is promised: a failure after a replacement leaves the old
object deleted. Mode persistence behavior is recorded by the validation
campaign, not assumed. The work database is opened once per session and
reopened after close. Handles never store or print the name or connection
string.

## Objects and values

A `FameObject(name, class_, type, freq, first_index=None, last_index=None,
data=None)` carries what the reference's does: the name (bytes; ASCII `str`
is encoded), the class (`series` or `scalar`), the type (a value kind or,
for date values, the frequency of the dates), the index frequency
(`undefined` for scalars), the range and the data. Codes are validated when
assigned (names, codes and enumeration members are accepted; the case
frequency is refused as a type); the data is validated against the class,
type and range when the object is written or converted, before any native
call. `quick_info` and `listdb` return objects without data and keep codes
the library reported even when they are outside the package tables (a
listing never fails on them; the derived views raise). `do_read(obj, db)`
re-queries the metadata inside the locked read, refuses an object whose
stored class, type or frequency no longer match (`DataValidationError`),
reads the object's range (`None` or the NC index meaning the stored
endpoint; an explicit endpoint must lie inside the stored range, which is
how a subrange is read), then sets the range and an owning data buffer on
the object and returns it; a failed read leaves the object untouched.
`do_write(obj, db)` deletes an existing object first, as the reference does.
Pass `replace=False` to refuse an existing name with the library's status. `unfame` refuses an object without data.

The data preserves native type, frequency, range and the
NC/NA/ND encodings *inside* a series. What the library persists for missing
observations at the start or end of a written range is the library's
rule, not the package's: the package adds no padding and trims nothing, a
read returns the range the library reports, and the campaign asserts that
whatever was retained equals what was written there while recording the
retained range. On the two inspected installations every leading and
trailing ND was dropped, an all-ND range read back empty, and leading and
trailing NC and NA were kept; that is measured behavior of those
installations, not a promise for every version.

A namelist value is the list text the library returns (members within
braces, separated by commas). The library documents that layout only to
that extent and may return the same list spelled differently from what was
written; the object's `data` keeps the returned bytes untouched, and
`namelist_members(data)` parses them into the ordered members under a
strict grammar (optional blanks around members and inside an empty list;
no empty members, no blanks inside a member, nothing outside the braces),
raising `DataValidationError` for anything else. Members are returned as
spelled, without case change or de-duplication. Plain string values are
compared byte for byte; only namelists have this structural reading.
Series values are exact-dtype one-dimensional arrays
(float64, float32, int32, int64) or lists of bytes; a zero-length series is
truly empty (NC endpoints). Scalar reads return exact-width NumPy scalars
(`numpy.float32` for numeric objects, `numpy.float64`, `numpy.int32`,
`numpy.int64`) so that every bit pattern, including NaN payloads, survives a
round trip; missing-value globals for float32 are kept as `numpy.float32` and
classified from their bits, never through a double. A Python float written as
`numeric` is rounded to float32 by NumPy; pass `numpy.float32` to control the
encoding.

Every Python-side check happens before the first native call: name, kind,
frequency (a date value frequency must be a calendar frequency, never
case), attributes, value encoding (a Boolean code must fit int32, a date
index int64, a float must be encodable, strings and namelists are NUL-free
bytes) and buffers (dtype, byte order, contiguity, length and 64-bit range
arithmetic). Object names are checked for their byte capacity only; whether
a name is legal (the library reserves a number of words, such as the names
of its data types and missing-value codes, and date-like names) is the
library's decision, reported as `HLIError` with status 25 by the object
creation and never guessed by a shipped word list. Buffers are validated again immediately before the write, so a
buffer or list mutated after construction is refused rather than passed on.
An invalid input therefore makes no mutating native call and leaves existing
objects and files unchanged. The caller's buffer is never converted or
modified. Reads allocate owning buffers. Allocation is bounded (2**31-1
observations, 2**28 string bytes).

`classify_by_sentinel` compares bit patterns with the globals read after
initialization; `missing_type` asks the library per value. The campaign checks
their agreement before the bitwise form is trusted for vendor data. Nothing
assumes that floating missing values are NaNs: the sentinels are whatever the
library exports (the first campaign observed distinct non-NaN precision
values), and the offline tests run a synthetic finite-sentinel profile as
well as the NaN-payload profile. Boolean missing codes are never coerced to True.

`do_write(obj, db)` defaults to `replace=True`: it deletes an existing
object first, matching the reference. With `replace=False`, an existing
name raises the library's own status and its stored data is preserved. The default `observed` attribute is `summed` for floating data and
`undefined` otherwise, matching the reference; `basis` defaults to daily.
Both accept enumeration members, their codes or their names and are
validated by one function (`attribute_codes`) before any native call, in
every writer, so an invalid attribute never deletes or creates anything.

## Listing

`listdb(db, wildcard="?", alias=True, class_="", type="", freq="")` (a path
opens read-only and closes) sets the ITEM options it needs inside its
locked operation and then *normalizes* the five options it uses
(`ITEM CLASS`, `ITEM TYPE`, `ITEM FREQUENCY`, `ITEM INDEX`, `ITEM ALIAS`)
to ON. The filters take the reference's comma-separated strings or Python
sequences (`""` and `None` mean no filter). The `freq`
filter accepts exact frequency names or codes from the frequency table (a
family word such as `quarterly` is refused with `ValueError`) and is
enforced on the metadata of the listed objects: an object is returned only
when its frequency code is one of those requested, so scalars (undefined
frequency) appear only when `undefined` is requested. The library's own
selectors are coarser than the package filter: `ITEM FREQUENCY <family>`
words (`MONTHLY`, `QUARTERLY`, `WEEKLY`, ...) select date-indexed series by
family, and `ITEM INDEX CASE` / `ITEM INDEX DATE` select series by index
kind; there is no per-anchor word and no frequency word for case series.
The listing narrows the native selection with those documented words only
when that cannot exclude a requested object (families when every requested
frequency is date-indexed; the case index when only `case` is requested)
and leaves the selection broad otherwise, so the result never depends on
the narrowing; any option error surfaces. It does not restore a prior
state:
there is no declared call to read the options back, so a selection made by an
earlier command is not preserved across a listing. Commands that depend on
those options must set them again afterwards. Cleanup frees the cursor and
attempts every option reset even after a failure; the first failure is what
propagates.

## Bridge

`refame(name, value)` converts a Python/TimeSeriesEconPy value into a
`FameObject` ready for `do_write`; `unfame(obj)` converts an object read
with `do_read` back into a value; `readfame` and `writefame` do the same
for whole workspaces, with `bridge.readfame_report` and
`bridge.writefame_report` as the per-object contained variants. A single
object is read as `unfame(do_read(quick_info(db, name), db))` and written
as `do_write(refame(name, value), db)`, as in the reference. The bridge
uses TimeSeriesEconPy's public API only.

### Representation

| Python value | FAME object | read back as |
|---|---|---|
| `float`, `numpy.float64` | precision scalar | `float` |
| `numpy.float32` | numeric scalar | `numpy.float32` |
| `int` (exactly representable in float64) | precision scalar | `float` |
| `bool`, `numpy.bool_` | Boolean scalar | `bool` |
| `MIT` of a calendar frequency | date scalar with the moment's frequency | `MIT` |
| `str` not shaped `{...}` | string scalar | `str` |
| `str` shaped `{...}` | namelist (the reference's detection) | `NameList` |
| `bridge.Text("{literal}")` | string scalar, always | `str` |
| `bridge.NameList([...])` | namelist (members upper-cased, as the reference writes) | `NameList` |
| `bytes` | string scalar, always literal | `str` (`bytes` with `text="bytes"`) |
| `list`/`tuple` of `str` | case string series from index 1 | `StringSeries` at `MIT(Unit(), 1)` |
| `TSeries` float64 or exact integers | precision series | `TSeries` float64 |
| `TSeries` float32 | numeric series | `TSeries` float32 |
| `TSeries` bool | Boolean series | `TSeries` bool |
| `bridge.DateSeries` (calendar value frequency) | date series | `DateSeries` |
| `bridge.StringSeries` | string series | `StringSeries` |
| `Workspace`, mapping, `MVTSeries` | one object per flattened name | separate members (no reconstruction) |

Deliberate differences from the reference, each covered by tests:

- An `int` becomes a precision scalar when it is exactly representable in
  float64 and is refused otherwise; the reference rounds integers to a
  float32 numeric scalar.
- A NaN scalar writes as NC, like NaN observations; the reference's scalar
  NaN test never matches, so it stores a plain NaN.
- A name-list reads back as a `NameList` of ordered members; the reference
  returns the library's list text, whose layout the library may change.
- A string series keeps its first date in a `StringSeries`; the reference
  returns a bare vector.
- A missing Boolean observation raises `MissingValueError`; the reference
  reads it as `True`. A missing date reads as `None`; it is never an integer.
- Existing objects are replaced by default by `writefame` (as the
  reference does); `do_write` also defaults to `replace=True`; `replace=False` is the opt-out.
- Wildcard matches are ordered by name bytes and the same object matched
  twice is read once; the reference follows the library's cursor order and
  reads duplicates twice.
- `writefame` with a path requires an explicit `mode`; the reference
  defaults to overwrite.
- `readfame` and `writefame` raise at the first object that cannot be
  read, converted or written; the reference logs the failure and skips the
  object. `bridge.readfame_report` and `bridge.writefame_report` contain
  failures per object instead.
- A case moment (`MIT` of `Unit`) is refused as a date *value* with
  `DataValidationError`: as a scalar, as a `DateSeries` observation and as
  the `value_frequency` of an empty or all-missing `DateSeries`. The
  library indexes series by the case frequency but does not accept it as
  an object type, so such an object cannot be created; the reference maps
  the frequency anyway and leaves the refusal to the library. Nothing is
  remapped to a calendar or to a number in its place (write numeric data
  for case numbers explicitly). Case-indexed series of calendar dates, case
  string series and the case-index conversions are unaffected.

### Frequencies

Supported index frequencies: `Unit` (case), `Daily`, `BDaily` (business,
Monday to Friday), `Weekly(end_day)` for all seven endings, `Monthly`,
`Quarterly(1..3)` (library anchors october/november/december),
`HalfYearly(1..6)` (july..december) and `Yearly(1..12)` (january..december).
Supported date *value* frequencies are the same set without `Unit`: the
case frequency is never a value type (see the differences above), and the
object model refuses it in the same way (a `FameObject` typed by the case
frequency, `type_code("case")`), before any native call.
The library names quarterly and half-yearly frequencies by one of their
equivalent ending months; the maps are the reference's. Ten-day, biweekly,
twice-monthly, bimonthly, ypp, ppy, intraday, weekly-pattern and undefined
frequencies raise `UnsupportedFrequencyError` on either side and are never
mapped to an ordinary calendar; raw reads of such objects still work.

Indices go through the library's year/period functions. The year/period
conventions are the reference's, computed from public TimeSeriesEconPy
calendar functions: year-period moments decompose directly; daily and
business moments use the year and the day (business day) number within the
year; weekly moments use the year of the week's ending day and the week
number `ceil(day_of_year / 7)` of that day, so a year has a week 53 exactly
when a week ends on its last day (or last two days in a leap year). A
library answer outside those conventions (a day number beyond the year, a
week that does not end in the stated year) raises `DataValidationError`
rather than being normalized. Case moments never touch the library: their
index is the moment's integer value, any signed 64-bit integer; the type
and range checks run before that fast path, so an index outside the
signed 64-bit range is refused for every frequency. Years
outside Python's calendar (1..9999) are refused for the day-based
frequencies; the library's own year range applies natively.

### Missing, empty and text policies

`missing="nan"` (default) reads NC, NA and ND as NaN for precision and
numeric values and as `None` for date and string values, which is lossy
between the three categories; a missing Boolean has no in-band value and
raises `MissingValueError` under either policy; `missing="strict"` raises
for every missing observation. Writes encode NaN and `None` as NC. Any
other policy string raises `ValueError`.

`empty="preserve"` (default) writes an empty series as a truly empty FAME
series, which stores no first date, so reading it back needs
`empty_firstdate` (`EmptySeriesError` otherwise). `empty="reference"`
follows the reference: an empty TSeries or DateSeries writes one NA
observation at its first date, and on read a single missing observation
collapses to an empty series (precision, numeric, Boolean and date series;
string series are never collapsed and have no reference encoding, so they
are written truly empty under both policies). That encoding cannot
distinguish an empty series from a one-observation missing series, which is
why it is opt-in.

`text="ascii"` (default) encodes `str` string values as ASCII and decodes
stored values strictly as ASCII, raising `TextEncodingError` otherwise;
`text="bytes"` encodes `str` as ASCII and returns stored values as their
bytes; `text="utf-8"` encodes `str` strictly as UTF-8 (a lone surrogate is
refused) and decodes strictly as UTF-8 (bytes that are not UTF-8 are
refused, never replaced). `bytes` input is written as given under every
policy, an embedded NUL is refused under every policy, and the policy is
validated with the other policies before any path is opened. The policy
applies to string scalars, the `Text` carrier, `StringSeries` observations
and plain string vectors on `refame`, `unfame`, `readfame` and `writefame`,
including the `*_report` variants, where a value the policy cannot encode is one
contained failure. Object names and namelist members are not values and
stay ASCII under every policy, so a `str` shaped `{...}` with a non-ASCII
member is still a namelist with an invalid member, while `Text` of the same
text is a UTF-8 string scalar. The whole stored byte sequence is decoded: the reference
wrapper slices its read buffer by the native byte length on a character
index, which fails when the last character is multibyte (a wrapper
limitation recorded by the `text` validation group); that behavior is
deliberately not reproduced. Byte capacities apply to the encoded bytes,
never to character counts. The migration reads string values under the
default policy and does not take a text option: a value with non-ASCII
bytes is a contained per-object failure of the run (see the
[migration guide](migration.md)).

With a path target, every check that does not need the database (types,
frequencies, dtypes, exact integer conversion, policies, object names,
flattening, collisions, cycles) and the calendar conversions run before the
database is opened, so an invalid input never creates, truncates or opens a
file. Path targets post after success and always close; database targets
never post.

### Workspaces

Reading: positional names are explicit names or wildcard patterns (`?`
any run, `^` one character); with none, everything (`?`) is read. Wildcards
are expanded with `listdb` and the `alias`, `class_`, `type` and `freq`
filters; explicit names are looked up with `quick_info` whatever their
class or frequency, and an absent explicit name raises the library's
status. Each resolved object is then read with `do_read` and converted
with `unfame`. Names
are transformed in order: the `prefix` (joined by `glue`, compared
upper-cased) is stripped from the start when present; `collect` entries (a
name, a `(name, nested)` pair whose second element is a list, tuple or
mapping, a mapping, or a list of those; `"?"`/`"*"` collects by the first
glue-separated part) nest matching names into sub-workspaces; finally
`namecase` (`str.lower` by default; any `str -> str` callable returning a
non-empty string) produces the key. Collect matching is done on the
library's upper-cased name against the whole prefix followed by the glue
(a prefix may itself contain the glue, such as `"a_b"` for `A_B_C` giving
`a_b.c`); on a match the whole prefix and glue are removed and the output
key is produced separately (the prefix as given, or `namecase` of the
first part for `"?"`/`"*"`, so `A_B` with `namecase=lambda s: "x_" + s.lower()`
gives `x_a.x_b`). The reference helper removes one split part and reuses
the transformed key for matching; neither quirk is reproduced. A name with
nothing after the prefix and glue stays a member; `collect` with an empty
`glue` and a self-referencing collect specification are refused.
Every destination is computed before any object is read; two different
objects that would land on the same key, or a key that would be both a
value and a nested workspace, raise `NameCollisionError` and nothing is
read. Explicit names keep their argument order; wildcard matches are ordered
by name bytes.

`readfame` raises at the first failure. `bridge.readfame_report`
contains per-object read and conversion failures and returns a `ReadReport`
with the partial `workspace`, the `failures` (`ObjectFailure`: FAME name,
key path, the exception, its class name and numeric status; never library
text) and `complete`. With `raw_fallback=True` an object the bridge cannot
represent (unsupported frequency, missing Boolean, truly empty series,
non-ASCII text, malformed name-list) is stored as its `FameObject`, data
read but not converted, and listed in `raw`; native read failures are
never replaced by an object. Name resolution and collisions stay strict in both variants,
and the `text` policy of a read applies to every string value it converts.

Writing: workspaces, mappings and multivariate series (each argument, any
number of them, or one tuple of them as the reference accepts) are flattened recursively by joining names with `glue`; an
optional `prefix` is prepended to every top-level name (`prefix=""` still
adds the glue, `prefix=None` adds nothing). A cycle raises
`WorkspaceCycleError`; a non-string key `TypeError`; an invalid name
`ValueError`; two names equal under the library's case-insensitive naming
`NameCollisionError`. All of that, the `mode`, `empty`, `basis` and
`observed` options, every value's validation and every calendar conversion
happen before the destination is opened and before the first create,
replace or delete, so an invalid input never creates, truncates or opens a
file and never mutates an open handle. With nothing to write (empty
inputs) the destination is not opened at all: `writefame` returns
`()` and the report variant an empty, complete, unposted report; creating
an empty database is `opendb`'s job. `writefame` returns the
FAME names written and raises at the first native failure (a path target
is then closed without posting). `bridge.writefame_report` keeps
flattening, names, collisions, cycles and the options strict, contains an
invalid value, a failed conversion or a native failure per object, opens
the destination only when at least one object converted (when every
object failed the failures are returned and nothing is created, truncated
or opened), writes the others, posts a path target when at least one
object was written, and returns a `WriteReport` (`written`, `failures`,
`posted`, `complete`). No rollback is promised: a failure after some objects were
replaced leaves them replaced, and what the library keeps of unposted
changes is the library's rule. Multivariate series are written as one
series per column and are not reconstructed on read.

## Commands

`fame(command)` redirects output to a temporary file with a literal
`output file("...!")`, executes, restores `output terminal` and removes the
file, whether or not the command fails, and returns the captured bytes
(the reference prints them; `output=` takes a binary stream as its
`fame(io, command)` form does, and there is no string-macro form). The file name is fresh inside a
private directory created for the call and the file itself is created by
the library, never pre-created by the package. `CommandError` carries the
status, the failing `stage` (`redirect`, `command` or `restore`) and any
partial output on its `output` attribute, never in its message, plus the
opt-in `extended_text` captured before the restoration. When the payload
fails and the restoration fails too, the payload error propagates and the
restoration status is kept on `restore_status`. INPUT statements are
expanded before execution: a statement starts at the beginning of the text or
after a `;` or newline and ends before the next one, so consecutive INPUT
statements are all expanded; literal `FILE("name")` and bare names, `.inp`
appended when no suffix and no trailing `!`, relative names resolved against
`base_dir` (the working directory by default), computed FILE() arguments
refused, cycles/depth/size limits enforced, and every file-system error
(missing, unreadable, not a regular file) reported as `IncludeError` without
a file name. Commands are limited to 2**20 bytes (reference-derived).

## Migration

`famepy.migration` retires a FAME database into a *new* DataEcon file under
a documented layout with explicit loss policies: the plan is a metadata
preview re-checked at run time, an existing destination is refused and
never opened, the archive is written to a partial file and moved into
place when the run finishes, and reading back verifies the layout
structurally. See the [migration guide](migration.md) for the
plan/run/verify contract, the fidelity matrix, the options and what a data
migration does not carry.

## Performance

Bulk native reads/writes and contiguous NumPy buffers are the baseline. No
compiled accelerator and no speed claims exist in this release.
