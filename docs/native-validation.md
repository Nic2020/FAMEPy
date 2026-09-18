# Native validation

There are three separate kinds of evidence: Python tests, an independent C shim,
and actual FAME execution. Only the first two can run without a licensed FAME
installation. Passing them is not evidence that vendor calls are correct.

## Discovery available now

After installing the package, run from outside the source tree:

```sh
python -m famepy
python -m famepy --probe
```

Configure `FAME` or an absolute `FAMEPY_LIBRARY` path first. Discovery is read-only
and does not load CHLI; `--probe` loads it in a subprocess and locates symbols.
Neither calls initialization, version, date conversion or any database operation.
The timeout defaults to 15 seconds. `--library` overrides environment discovery.
Missing FAME produces JSON and exit code one, never a successful integration test.

Do not paste private paths into shared commands. Reports omit paths and raw loader
output. Keep all vendor headers, libraries and help files in their installation.

## Interpreting probe failures

The public CLI still exits one on failure; `probe_exit_code` is the separate
child exit code. Schema version 2 adds these `probe_failure_kind` values when
status is `probe_failed`:

| Child exit | Failure kind | Meaning |
|---|---|---|
| 20 | package_import_failed | The child bootstrap could not import the package |
| 21 | invalid_request | Invalid internal request data |
| 22 | discovery_failed | The child's discovery could not locate a supported library |
| 23 | library_load_failed | The operating system rejected native loading |
| 24 | child_exception | Another exception during symbol probing |
| 25 | child_setup_failed | Child error-mode setup failed before loading CHLI |
| Negative POSIX exit | signal_exit | A signal terminated the child |
| Windows exception code | windows_exception_exit | A Windows exception-style exit code |
| Other nonzero exit | unclassified_exit | Cause unknown; retain the numeric code |

Native load failures also report `load_errno`, `load_winerror` (integer or null)
and `load_error_class`. The classes are `library_or_dependency_not_found`,
`bad_image`, `initialization_failed`, `access_denied`, or `other`. These are
conservative numeric hints, not proof of one root cause: a missing library and
a missing dependency can share an error code; `bad_image` need not mean only
wrong architecture. `initialization_failed` refers to OS library initialization,
not a call to CHLI initialization. POSIX loader errors often have no numeric
code, so they may correctly remain `other`. No filename/message parsing is used.

Timeout, process-start failure and invalid child output retain their existing
`probe_timeout`, `probe_start_failed` and `probe_invalid_output` statuses.
If native output corrupts the JSON, numeric details may be unavailable; do not
paste raw stdout/stderr to compensate. Record the exact artifact and failure kind.

The Windows child requests suppression of system error dialogs before loading
CHLI and preserves inherited error-mode bits. The parent process is unchanged.
This does not control dialogs deliberately displayed by vendor code. See
[Windows error-mode documentation](https://learn.microsoft.com/en-us/windows/win32/api/errhandlingapi/nf-errhandlingapi-seterrormode).

## ABI review before database tests

The candidate signatures in `src/famepy/_abi.py` come from FAME.jl revision
`30586743f1c1bed549841e0309410da0134f3014`. They have not been checked against
installed vendor headers. Review every function used by a test before calling it:

- Calling convention and status convention: `cfm*` returns void and takes a
  leading status pointer; `fame_*` returns status. Confirm against local headers.
- Integer and Boolean width, 64-bit date/index width, signedness, and native
  range struct size, alignment and field offsets on each platform.
- `fame_year_period_to_index` output: the reference declares it inconsistently;
  the candidate uses the 64-bit declaration from its bridge. This is unresolved.
- Input strings versus writable output buffers, arrays of string pointers,
  lengths, terminators and allocation ownership.
- Missing globals (including whether each string global is a pointer or array),
  error codes such as truncation, and version-specific exported symbols.
- The truncation status value and whether extended error status 513 still
  requires `cfmferr`; that function's output-buffer size and accepted encoding.
- Object-name and path/command encodings, maximum wildcard name byte length,
  Linux transitive-library search requirements and additional Windows DLL paths.
- Whether CHLI initialization requires `FAME` even with an explicit library path.

Record permitted technical conclusions, not copies of headers or help text.
Symbol presence alone cannot establish any of the above. The shim verifies the
Python calling mechanism against its own declarations, not a vendor ABI.
The synthetic shim exports a double, a string pointer and a string array; tests
verify only address discovery for all three, never reinterpret a string array
as a pointer. Seeing a symbol does not establish its type or calling convention.

## Subsequent integration gate

Use disposable synthetic databases, subprocess timeouts, deterministic values,
and the exact built artifact. Record OS/architecture, Python/dependency/FAME
versions, source revision, artifact SHA256, commands, exit codes and skips.
Test close/reopen and cross-process persistence, failures, missing values, all
supported frequency anchors, and Julia/Python interchange in both directions.
Mark unavailable comparisons outstanding. No such database tests ship yet.

The installation has no compiler or vendor-help build step. If offline installs
are needed, collect compatible dependency wheels in an approved environment and
install from that wheelhouse; do not copy a virtual environment between OSes.
