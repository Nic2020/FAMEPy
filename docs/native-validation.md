# Native validation

Three kinds of evidence exist: Python tests with the in-memory fake backend,
the independent C shim, and actual FAME execution. Only the first two run
without a licensed installation, and passing them is not evidence that vendor
calls are correct. The fake and the shim implement the package's own
contracts, not FAME behavior.

## Discovery

After installing the package, run from outside the source tree:

```sh
python -m famepy
python -m famepy --probe
```

Configure `FAME` or an absolute `FAMEPY_LIBRARY` path first (`--root` names
the trusted installation root for an explicit library). Discovery is
read-only; `--probe` loads the library in a subprocess and locates symbols,
including any presence-only symbols (none at present). Neither calls initialization.
Missing FAME produces JSON and exit code one, never a successful test.

## Interpreting probe failures

`probe_failure_kind` values: `package_import_failed` (20), `invalid_request`
(21), `discovery_failed` (22), `library_load_failed` (23), `child_exception`
(24), `child_setup_failed` (25), `signal_exit`, `windows_exception_exit` or
`unclassified_exit`. Load failures also report `load_errno`, `load_winerror`
and a conservative `load_error_class`. Timeout, start failure and invalid
child output keep their own statuses. No filename or message parsing is used.

## The consolidated campaign

One entry point runs the whole native campaign and writes one sanitized JSON
report. Build a wheel from the reviewed revision, hash it, install it in an
isolated environment and run from a directory outside the checkout:

Windows (PowerShell):

```powershell
python -m pip wheel --no-deps -w .\wheelhouse <path-to-checkout>
Get-FileHash .\wheelhouse\famepy-*.whl -Algorithm SHA256
python -m pip install .\wheelhouse\famepy-*.whl
python -m famepy.validation --native --scratch .\famepy-scratch --report .\famepy-report.json `
  --wheel .\wheelhouse\famepy-<version>-py3-none-any.whl --source-sha <revision> `
  --abi-attestation <sha256-of-the-checklist-review-record>
```

Linux (bash):

```sh
python -m pip wheel --no-deps -w ./wheelhouse <path-to-checkout>
sha256sum ./wheelhouse/famepy-*.whl
python -m pip install ./wheelhouse/famepy-*.whl
python -m famepy.validation --native --scratch ./famepy-scratch --report ./famepy-report.json \
  --wheel ./wheelhouse/famepy-<version>-py3-none-any.whl --source-sha <revision> \
  --abi-attestation <sha256-of-the-checklist-review-record> \
  --julia /path/to/julia --julia-project /path/to/project-with-FAME.jl
```

Options: `--groups lifecycle,database,...` selects groups (`--list` prints
them), `--timeout` bounds each operation (children get four times it, and a
timed-out child is terminated together with its descendants), `--library`
and `--root` override discovery, `--julia`/`--julia-project` enable the
differential checks. Without `--native` every group is reported as blocked; a
missing or unloadable library blocks them too. Native groups never silently
pass without a library.

The scratch directory must be new or empty. The runner reserves a fresh
`run-<id>` directory inside it and every group works in its own subdirectory
of that run; nothing pre-existing is written, replaced or removed, and a
nonempty directory, a file or a symbolic link makes the scratch unusable and
launches no child. A second campaign needs a new (or emptied) scratch.

Execution order and gates:

1. `preflight` (parent, read-only apart from the reserved run directory):
   environment, dependency versions, package identity (source hash, whether
   the import came from site-packages or the working directory, the wheel
   hash and whether the wheel's shipped sources match the imported package),
   the ABI table identity, the operator's attestation, scratch reservation,
   discovery and symbol probe including the presence-only row.
2. `lifecycle` (child): initialize, idempotence, version, sentinel facts,
   refusal of `reset()`, one terminal finalize, then rejection of every
   reinitialization route (same session, new wrapper, operations) before any
   native call, and finally a spawned fresh-process child that initializes,
   reads the version and finalizes on its own. Failure blocks the remaining
   groups.
3. `database`, `raw_matrix`, `discovery`, `commands`, `bridge`,
   `frequencies`, `workspace`, `extended_errors`, `migration`: each in its
   own child with its own one-shot runtime and scratch subdirectory; every
   group finalizes exactly once at its end (the database group finalizes
   while a handle is still open to prove the stale-handle contract). Calendar indices
   used by the groups come from the library's own year/period conversion.
   Persistence is verified by a further child that reopens the database
   read-only and compares class, type, frequency, range and exact value bits
   against a manifest (string values travel as hex, so non-ASCII sentinel
   bytes and empty strings are compared exactly). The Julia differential
   runs inside `bridge` when configured and is otherwise reported as
   unsupported.

Within `raw_matrix`, every object is built and validated before the database
is created, then written, read and verified as its own case inside its own
exception boundary (`verify_object:<name>`); one object that cannot be
created, or whose verification raises, fails its own cases, blocks the cases
that depend on it (`prerequisite case did not pass`) and leaves the others
untouched. Replacement and deletion use fixtures of their own. Interior
missing values (NC/NA/ND between normal endpoints) are asserted for every
type; endpoint fixtures (leading and trailing ND, NC and NA, and all ND, for
each type) record the persisted range and classification codes as
observations and assert that whatever was retained lies inside the written
range and equals, index by index, what was written there (an invented value,
a changed missing code or a shifted range fails), that the normal value keeps
its position, and that an explicit read of the stored range agrees. Their
cross-process manifest is built from the written values over the retained
indices, never from what was read back. The endpoint rule is the library's
and is recorded per installation; these checks establish preservation, not
the rule. A namelist is asserted by its ordered members (a reordered,
missing or malformed member fails, in the group and across processes) while
its length and which documented layout the library used are recorded as
observations; the returned bytes themselves never enter the report. Within
`database`, the `write` and `direct_write` modes use their own fixtures: an
existing database and a path that does not exist yet. The package must
refuse both modes before any native call, the library's local open must
return the bad-mode status for both (an unexpected success is closed again
and fails the case), the existing database must be unchanged and the new
path must stay absent; all of these are required. Within `discovery`, every
listing call is its own case, so one failure cannot hide the later class,
type, alias, name-length and truncation cases; the frequency filter is
checked as exact sets over mixed frequencies and scalars, invalid input is
refused, the options are shown normalized afterwards, and the counts the
native wildcard yields under the documented family and index words alone
are recorded as isolated observations (an option error there is recorded,
never propagated). Within `commands`, a failing case names the stage that
returned the status.

Within `bridge`, after the monthly precision cases, every value kind of the
reference is written through the bridge and read back (numeric, integer,
Boolean, date, string, literal brace string, name-list, string vector,
numeric/Boolean/date/string series; the objects are written under a prefix
because bare kind labels can be names the library reserves, and the
library's status for a reserved word used as an object name, 25, is
asserted natively), each missing category of each kind is
written raw and read through the bridge (floating values as NaN, dates and
strings as `None`, a missing Boolean refused, strict mode refused), and the
reference's empty-series cases are written and read under the reference
convention. Within `frequencies`, every calendar frequency anchor of the
reference (daily, business, seven weekly endings, monthly, three quarterly,
six half-yearly and twelve annual anchors) is checked against structural
facts of the library's own index space rather than an assumed epoch: the
inverse conversion of a fixed moment, adjacency of consecutive periods
(across the 2020 leap day and a weekend), continuity across the 2020/2021
boundary and the number of periods in 2020 (366 days, 262 business days,
52 or 53 weeks per ending, the periods per year otherwise); the library's
year/period reading of the last week of 2020 is recorded as an observation
because the week-53 numbering is reference-derived. One precision series
and one date scalar per anchor, plus representative date-value/index
frequency combinations (including a case-indexed series of calendar
dates), are written, read back through the bridge and, separately, as raw
objects whose stored bits and missing categories are asserted against the
fixture and the explicitly loaded NC sentinel, then verified across
processes against manifests built from the fixtures and the verified
calendar conversion, never from what was read back (a backend that stores
NA in place of NC fails both); an unsupported library frequency must be
refused by the bridge and case indices must pass through untouched. The
case frequency as a date *value* is required to be refused before any
native call (scalar, series, empty and all-missing carriers, raw scalar and
series), the destination must be unchanged (listing and file bytes) after
every such attempt including the contained write, and the library's own
status for an object typed by the case frequency (16) is asserted at the
native layer rather than inferred. Within
`workspace`, the reference's workspace test (a scalar, a quarterly series, a
two-column monthly multivariate series and two nesting levels) is written,
listed, read whole (its quarterly series carries one missing observation
whose raw category is asserted), by explicit names, by wildcard, with
prefix stripping and with one- and two-level collection; collisions after transformation and
an absent explicit name must be refused; the report variants must contain
an unconvertible object and a missing Boolean, and fall back to raw carriers
on request; a contained write must report completeness and posting; and
four objects are verified across processes.

The `bridge` kind objects and every object of the `frequencies` fixture are
written with per-object containment (the report variant of the workspace
write) and read as separate cases: a refused or failed object is recorded
as its own failing `write:<name>` (or `write_kind:<kind>`) case carrying the
primary error and status, only the cases that depend on that object are
`blocked` (`object not written`), and every other object is still written,
read, checked raw and verified across processes. Blocked and missing cases
never count as passes: every per-object case is in the required list. The
strict batch behavior of `write_workspace` (first failure raises, nothing
posted) is covered by the `workspace` group and the unit tests and is not
weakened by this containment. The offline backends model two library
boundaries so that an invalid fixture cannot pass offline and fail natively:
a small sample of reserved words is refused as an object name with status 25
(no vendor word list is shipped and no name validator is invented) and the
case frequency is refused as an object type with status 16.

Within `extended_errors`, the opt-in retrieval of extended error text is
the native gate of the `cfmlerr` binding: with retrieval off the text is
unavailable (`UnsupportedOperationError`), after `enable_extended_errors`
a failing command must yield a `CommandError` carrying captured bytes sized
by the library's own length call, the text must not appear in the error
message, the capture must record no failure, the same bytes must be
retrievable afterwards and a later command must still run. Only the length
and whether the text is ASCII are recorded (as observations); the text
itself never leaves the child. Whether the library also reports text for a
failing local open is recorded, not assumed. A backend whose length call
exceeds the package bound cannot pass: the retrieval refuses to allocate
and the capture failure is recorded.

Within `migration`, a fixture of every FAME kind as scalar and series, raw
missing categories (NC, NA and ND), a case-indexed series, a weekly index,
a business-daily series with a missing observation, an empty series and
date series with and without missing observations is written to a
synthetic database, migrated into a DataEcon file with the default (strict,
mask) policies and read back: in the same process, and in a nested child
that opens only the archive, every object's description must equal the
description built from the fixture itself (exact bits, moment codes,
categories, frequency labels, representation), and the archive's status
mark must read `complete`. The negative cases are required too: an
existing destination file must be refused and left byte-identical, a
refused plan (an unsupported index frequency in the source), a name
collision, an explicit first date of the wrong frequency and a plan
built before the source changed must create no file, the lossy policy
must refuse a Boolean series with missing observations and mark its
archive `incomplete` while reporting the floating loss per object, a
contained per-object failure (a non-ASCII string) must be reported with
the archive marked `incomplete` and its counts, a later run onto that
incomplete archive and onto the finished archive must be refused with
both files byte-identical and the `incomplete` mark kept, and four
structural corruptions applied through the public writers with the
original attributes kept (replaced values, a mask shifted to another
start, a carrier re-indexed by another frequency, a payload inside an
empty carrier) must be detected in the same process and in a fresh
process that reads only the archive. When the DataEcon native
extension cannot load, the group records that as one blocked case and
blocks every required case: the environment block is distinct from a
failure and never a pass, and no fake write is substituted. See the
[migration guide](migration.md).

Compare every row of the [per-function checklist](abi-checklist.md) with the
installed header before the first run, record the conclusions per row in a
private record and pass that record's SHA-256 as `--abi-attestation`; the
report carries both the attestation and the identity of the declaration table
it applies to. Symbol presence does not validate a signature. A new binding
changes the declaration table identity and needs a new per-host review before
its group can run: `cfmlerr` (the extended-error length call) joined the
table in this revision, so the checklist row must be re-confirmed per host
and the `extended_errors` group is its gate.

## One combined session per host

The release candidate is qualified in one session per platform: the
regression campaign (all groups), the migration qualification (part of it),
and the bounded benchmarks, from the same installed wheel and revision.
Benchmark data is kept apart from the PASS/FAIL report and never enters it.

Windows (PowerShell), after the wheel build, hash and install shown above:

```powershell
python -m famepy.validation --native --scratch .\famepy-scratch --report .\famepy-report.json `
  --wheel .\wheelhouse\famepy-<version>-py3-none-any.whl --source-sha <revision> `
  --abi-attestation <sha256-of-the-checklist-review-record>
python -m famepy.benchmarks --native --scale standard --scratch .\famepy-bench `
  --report .\famepy-bench.json `
  --wheel .\wheelhouse\famepy-<version>-py3-none-any.whl --source-sha <revision>
```

Linux (bash), with the Julia differential and the Julia benchmark:

```sh
python -m famepy.validation --native --scratch ./famepy-scratch --report ./famepy-report.json \
  --wheel ./wheelhouse/famepy-<version>-py3-none-any.whl --source-sha <revision> \
  --abi-attestation <sha256-of-the-checklist-review-record> \
  --julia /path/to/julia --julia-project /path/to/project-with-FAME.jl
python -m famepy.benchmarks --native --scale standard --scratch ./famepy-bench \
  --report ./famepy-bench.json \
  --wheel ./wheelhouse/famepy-<version>-py3-none-any.whl --source-sha <revision> \
  --julia /path/to/julia --julia-project /path/to/project-with-FAME.jl
```

Retain both reports with the same identity record (revision, wheel hash,
library version, OS and architecture, dependency versions, commands). The
benchmark report is described in [benchmarks](benchmarks.md); its exit
status is 1 and its `result` is `incomplete` when any requested
measurement failed, timed out or was rejected, and the benchmark numbers
never enter the validation PASS/FAIL.

For actual native groups, preflight requires an installed package, a source
revision, a valid wheel whose shipped sources match the installed package, and
an ABI-review attestation digest. Missing or mismatched provenance blocks the
groups. Injected offline backends are exempt from this native-only gate.

## How a group passes

The parent never reads a child's standard streams for results. Each launch
reserves a result file and a token inside the group's scratch directory;
the child redirects its stdout and stderr descriptors to a local log before
anything native runs (so text the library prints stays there), writes its
result atomically and echoes the token. A missing, unreadable, malformed,
partial, stale (wrong token) or wrong-group result fails the group with a
matching `exit_kind`; the size of any stray stream output is reported as
`stray_output_bytes`, never its content. Nested verification children and
the Julia differential use the same protocol (fresh token and result path
per launch, atomic completion, expected group, size bound, process-tree
timeout), and a group child validates every nested case against the same
schema before adopting it, keeping the observation flag so that an
observation can never satisfy a required assertion. A group passes only when its child
exited cleanly, every reported case is well formed, every required case for
that group is present with status `pass`, and no case failed or was blocked.
Duplicate case identifiers, required cases marked only as observations,
missing required cases, an empty case list, an unknown status, a malformed
value, an exception outside the recorder or a timeout make the group fail; a
required case reported blocked makes the group blocked. The campaign result
is `PASS` only when every selected group passed. Predicates are asserted
explicitly (a callable that merely returns is not a passing assertion), scalar
and array values are compared by dtype and bit pattern, and observations that
are not assertions are marked `observation`.

The self-test in the package's own test suite drives the runner with
intentionally faulty backends (wrong version, NaN payloads lost, posts
discarded, private markers in native output, a hanging initialization, one
object that cannot be created, a classifier that fails inside one object,
corrupted endpoint reads, a local open that accepts the connection modes or
leaves a file behind when refusing them, rejected family or index option
words, reordered or dropped namelist members, refused redirections,
failed restorations, a lost endpoint neighbour, a calendar that reports
every index one period late, a daily calendar without leap days, missing
Boolean observations read as true, an object missing from wildcard
listings, NC stored as NA outside the monthly calendar, one bridge kind and
one frequency anchor object refused at creation) and with adversarial child
payloads and results; each must yield `FAIL` or `BLOCKED`, and no synthetic
private marker may reach the final report. Backends that print forged
results and diagnostics to the C-level streams, that apply a different
endpoint rule, or that lay a namelist out differently, must still pass with
only their observations changed.

## Report contents

The report holds versions, platform, package identity, symbol presence,
per-group status with pass/fail/blocked/unsupported counts, exit codes and
timeouts, and per-case records limited to identifiers, statuses, error class
names, numeric CHLI statuses, OS error numbers, and synthetic expected/actual
values. Every case is validated against a field and value schema in the
parent: strings are short and free of path separators, floats that are not
finite appear only as bit patterns, and bytes are rendered losslessly
(printable ASCII, bounded hex of at most 64 bytes, or a length and digest
beyond that) only when the runner constructed them itself: the expected side
of an assertion and the sentinels a group registers. Any other bytes, which
is what the library returns when it disagrees with a fixture, are reduced to
their length. Anything else is replaced by a `malformed case record` failure. Command
output never enters the report; command cases record predicates only.
Review the report before transferring it anywhere.

Retain with the report: the exact source revision and any working-tree diff,
the wheel hash, library version and OS/architecture, dependency versions and
the command used. On a retry after a fix, record the changed identity.

## Julia differential

When `--julia` is given, the bridge group first writes one precision series
per calendar frequency anchor and one value of each kind into its database,
then runs a generated script that reads them with FAME.jl and reports
floating values as IEEE bit patterns and moments as their integer values
(the two libraries share the moment encoding), and writes a database of
independently constructed moments (one series per anchor) and kinds for
Python to read back and compare with its own fixtures; its result travels
through the worker result-file protocol. Julia reports, per anchor, the
moment integer, the library frequency name of the series' frequency, the
element type and the value bits, so anchor identity is checked in both
directions. The FAME.jl tree identity is
compared with the pinned reference; a different tree qualifies the comparison
(the case is reported `unsupported`, the value comparisons carry a note) so
that a mismatch is visible and never a silent pass. When `--julia` is
configured, every differential comparison is a required case of the
`bridge` group (a Julia that fails to run or a missing comparison fails the
group); without it the single `julia_differential` case is `unsupported`.

## Offline checks

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -q
uv run python scripts/build_test_shim.py    # then set FAMEPY_TEST_SHIM and rerun pytest
uv run python scripts/verify_artifacts.py   # installed wheel and sdist-rebuilt wheel
```

The shim implements the candidate declarations over an in-memory toy database
with fault injection, so pointer, buffer, lifetime, wildcard, command and
extended-error mechanics are exercised end to end without vendor code. Its
`cfmlerr` follows the declaration the package now binds (a leading status
pointer and one output length); that the installed library agrees is what
the per-host checklist review and the `extended_errors` group establish.

The migration tests and the migration group need the DataEcon native
extension that TimeSeriesEconPy ships in its wheels; when it cannot load,
the tests skip with that reason (set `FAMEPY_REQUIRE_DATAECON=1` to make
that a failure) and the runner group is blocked. `python -m famepy.benchmarks`
with an injected backend exercises the harness only and says so in its
report.
