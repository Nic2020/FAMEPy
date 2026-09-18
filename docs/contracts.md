# API and data contracts

## Runtime and diagnostics

Import never loads CHLI. `diagnose()` returns a dictionary with a schema
version, Python/dependency versions, platform, candidate ABI layout and
discovery status. `diagnose(probe=True)` loads the trusted library in a timed
subprocess and checks symbol presence, including presence-only symbols such as
`cfmlerr`, without calling CHLI. Schema version 3 adds the `presence_only` map
and `trusted_root_known`. Exit zero means only `library_found` or
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

`initialize()` returns the process default session and is idempotent. Exactly
one session may be initialized per process. The native library is fixed for
the process lifetime once loaded: no unload is attempted, and choosing a
different library or root after a load raises `RuntimeStateError`; use a new
process. Each initialization increments a generation; database handles from
an earlier generation raise `StaleHandleError`. `finalize()` closes tracked
databases (recording any close statuses on `last_cleanup_statuses`), then
finalizes. Ownership is released only by a successful `cfmfin`: a failed
finalization, including the cleanup after a failed startup, leaves the session
`broken` and still owning the process, so no other session (and no new
wrapper over the same library) can initialize until `finalize()` is retried
successfully. A failed initialization before `cfmini` leaves the session
retryable. Any use of a session after `fork`, including creating a new session
in the child, raises `InheritedRuntimeError`.

Every operation holds one process-wide reentrant lock for its whole duration,
and every operation on a database handle validates the handle (open, same
generation) inside that lock, immediately before the native call. A read
obtains metadata and data in one locked operation, so a concurrent close, key
reuse or object replacement cannot slip between validation and the call. Work
databases, ITEM options, wildcard cursors, output redirection and the extended
error state are process-global inside the library, so there is no
parallel-thread throughput promise and no public arbitrary-native-call API.

`check_status(0)` returns normally; other signed 32-bit integers raise
`FameError` with the original code. Default errors never include native text.
Extended error text is opt-in: configure `session.extended_error_retrieval`
with an `ExtendedErrorRetrieval` whose declarations come from the installed
header. The session then reads the text at the failure itself, under the same
lock and before any other native call (for commands, before the output
redirection is restored), and attaches it as `extended_text` on the raised
`FameError`; `session.extended_error_text()` returns the text captured by the
most recent failure. The text never enters an exception message. A retrieval
that fails (for example a length outside the bound) never masks the status;
its class name is kept on `extended_error_capture_failure`. The package ships
no vendor declaration, so `extended_error_text()` raises
`UnsupportedOperationError` until one is configured.

## Text

No vendor encoding has been established. The native layer exchanges bytes.
`str` input must be ASCII and is rejected otherwise; `bytes` pass through
without NUL bytes. Returned names are bytes with an ASCII `name_text` view
that raises on non-ASCII content. This is an initial validation boundary.

## Databases

`open_database(name, mode="readonly")` accepts the seven modes as integers,
names or `AccessMode` members. Closing never posts; `post()` is explicit.
Closing twice is a no-op. If the native close fails, the handle stays open and
tracked: the status propagates, `close()` can be retried, and `finalize()`
still attempts the close and records its status. Bridge functions that
receive a path open the database themselves, post after success and always
close. No rollback is promised: a failure after a replacement leaves the old
object deleted. Mode persistence behavior is recorded by the validation
campaign, not assumed. The work database is opened once per session and
reopened after close. Handles never store or print the name or connection
string.

## Values

`RawScalar` and `RawSeries` preserve native type, frequency, range and the
NC/NA/ND encodings. Series values are exact-dtype one-dimensional arrays
(float64, float32, int32, int64) or lists of bytes; a zero-length series is
truly empty (NC endpoints). Scalar reads return exact-width NumPy scalars
(`numpy.float32` for numeric objects, `numpy.float64`, `numpy.int32`,
`numpy.int64`) so that every bit pattern, including NaN payloads, survives a
round trip; missing-value globals for float32 are kept as `numpy.float32` and
classified from their bits, never through a double. A Python float written as
`numeric` is rounded to float32 by NumPy; pass `numpy.float32` to control the
encoding.

Every Python-side check happens before the first native call: name, kind,
frequency, attributes, value encoding (a Boolean code must fit int32, a date
index int64, a float must be encodable, strings and namelists are NUL-free
bytes) and buffers (dtype, byte order, contiguity, length and 64-bit range
arithmetic). Buffers are validated again immediately before the write, so a
buffer or list mutated after construction is refused rather than passed on.
An invalid input therefore makes no mutating native call and leaves existing
objects and files unchanged. The caller's buffer is never converted or
modified. Reads allocate owning buffers. Allocation is bounded (2**31-1
observations, 2**28 string bytes).

`classify_by_sentinel` compares bit patterns with the globals read after
initialization; `missing_type` asks the library per value. The campaign checks
their agreement before the bitwise form is trusted for vendor data. Boolean
missing codes are never coerced to True.

`write_object(..., replace=True)` deletes an existing object first, as the
reference does; without it the library's own status for an existing name is
raised. The default `observed` attribute is `summed` for floating data and
`undefined` otherwise, matching the reference; `basis` defaults to daily.

## Listing

`list_objects` sets the ITEM options it needs inside its locked operation and
then *normalizes* the four options it uses (`ITEM CLASS`, `ITEM TYPE`,
`ITEM FREQUENCY`, `ITEM ALIAS`) to ON. It does not restore a prior state:
there is no declared call to read the options back, so a selection made by an
earlier command is not preserved across a listing. Commands that depend on
those options must set them again afterwards. Cleanup frees the cursor and
attempts every option reset even after a failure; the first failure is what
propagates.

## Bridge (monthly precision)

NaN writes as NC. NC, NA and ND read as NaN by default (`missing="nan"`), which
is lossy; `missing="strict"` raises `MissingValueError`; any other policy
string raises `ValueError`. Integer arrays and integer scalars are converted
only when every value is exactly representable in float64 (2**60 is accepted,
2**53+1 is refused); float32 and Boolean series are refused in this release.
Other frequencies raise `UnsupportedFrequencyError`. With a path target, all
of these checks and the object-name check run before the database is opened,
so an invalid input never creates, truncates or opens a file.

Empty series: `empty="preserve"` (default) writes an empty TSeries as a truly
empty FAME series, which stores no first date, so reading it back needs
`empty_firstdate`. `empty="reference"` follows the reference: an empty TSeries
writes one NA observation at its first date, and on read a single missing
observation collapses to an empty TSeries. That encoding cannot distinguish an
empty series from a one-observation missing series; it is opt-in for that reason.

## Commands

`run_command` redirects output to a temporary file with a literal
`output file("...!")`, executes, restores `output terminal` and removes the
file, whether or not the command fails. `CommandError` carries the status and
any partial output on its `output` attribute, never in its message, plus the
opt-in `extended_text` captured before the restoration. INPUT statements are
expanded before execution: a statement starts at the beginning of the text or
after a `;` or newline and ends before the next one, so consecutive INPUT
statements are all expanded; literal `FILE("name")` and bare names, `.inp`
appended when no suffix and no trailing `!`, relative names resolved against
`base_dir` (the working directory by default), computed FILE() arguments
refused, cycles/depth/size limits enforced, and every file-system error
(missing, unreadable, not a regular file) reported as `IncludeError` without
a file name. Commands are limited to 2**20 bytes (reference-derived).

## Performance

Bulk native reads/writes and contiguous NumPy buffers are the baseline. No
compiled accelerator and no speed claims exist in this release.
