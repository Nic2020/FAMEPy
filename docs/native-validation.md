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
including presence-only symbols (`cfmlerr`). Neither calls initialization.
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
3. `database`, `raw_matrix`, `discovery`, `commands`, `bridge`: each in its
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
indices, never from what was read back. Which endpoint rule the library
applies remains an open vendor question; these checks establish
preservation, not the rule. Within `database`, the
`write` and `direct_write` modes use their own fixtures: an existing database
(open, write, post, reopen and list; required) and a path that does not
exist yet (recorded as an observation). Within `discovery`, the frequency
filter is checked as exact sets over mixed frequencies and scalars, invalid
input is refused, and the count the native wildcard yields under the
`ITEM FREQUENCY` selection alone is recorded as an observation. Within
`commands`, a failing case names the stage that returned the status.

Compare every row of the [per-function checklist](abi-checklist.md) with the
installed header before the first run, record the conclusions per row in a
private record and pass that record's SHA-256 as `--abi-attestation`; the
report carries both the attestation and the identity of the declaration table
it applies to. Symbol presence does not validate a signature.

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
corrupted endpoint reads, refused write modes, refused redirections,
failed restorations, a lost endpoint neighbour) and with adversarial child
payloads and results; each must yield `FAIL` or `BLOCKED`, and no synthetic
private marker may reach the final report. Backends that print forged
results and diagnostics to the C-level streams, or that apply a different
endpoint rule, must still pass with only their observations changed.

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

When `--julia` is given, the bridge group writes a script that reads the
Python-written database with FAME.jl and reports values as IEEE bit patterns,
then writes a database for Python to read back; its result travels through
the worker result-file protocol. The FAME.jl tree identity is
compared with the pinned reference; a different tree qualifies the comparison
(the case is reported `unsupported`, the value comparisons carry a note) so
that a mismatch is visible and never a silent pass.

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
`cfmlerr` signature is the shim's own choice for testing the retrieval
mechanics and is not a vendor fact.
