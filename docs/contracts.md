# API and data contracts

Only `diagnose`, `check_status`, error types and the command-line diagnostic are
implemented in this foundation release. Database and conversion APIs below are
design contracts for subsequent implementation, not callable features yet.

## Runtime and diagnostics

Import never loads CHLI. `diagnose()` returns a dictionary with a schema version,
Python/dependency versions, platform, candidate ABI layout and discovery status.
`diagnose(probe=True)` loads the trusted library in a timed subprocess and checks
symbol presence without calling CHLI. The CLI outputs the same report as JSON.
It exits zero for `library_found` (discovery only) or `symbols_found`; neither
means the ABI or a database operation was validated. Other statuses exit one.

Diagnostic schema version 2 retains these statuses and adds `probe_failure_kind`
for failed children. Its allowlisted values distinguish package import, request,
discovery, native loading, child setup and unexpected child exceptions. Signal
or Windows exception exits retain their numeric code; unclassified exits are
not guessed to be a particular crash. See [probe reports](native-validation.md).
The child protocol uses explicit UTF-8 decoding with replacement for invalid
output; raw text is never forwarded. This is not a choice of CHLI text encoding.

Path precedence is an explicit argument, then `FAMEPY_LIBRARY`, then `FAME`.
An invalid explicit choice fails without silently falling back. Windows/Linux
x86-64 are candidate native targets. An unsupported host can still import the
package and obtain a diagnostic. Runtime loading does not modify PATH.

`check_status(0)` returns normally; other signed 32-bit integers raise FameError
with the original code. Unknown codes are retained. Boolean/noninteger and
out-of-range values are rejected. Default errors do not include raw native text;
retrieval of FAME's extended command error text is deferred to command support
and must be opt-in because it may contain user commands or database identifiers.

`LibraryLoadError` is a subclass of `LibraryNotFoundError`, retaining optional
integer `errno` and `winerror` attributes without the original message. A missing
declared function raises `SymbolNotFoundError`, exposing only its known symbol
name. Both are exported from `famepy`.

## Database operations (planned)

Use an owning database context manager, read-only by default, with explicit
`post()` and `close()`. Closing will not implicitly post. High-level writes that
own their database will post after success. Failure does not imply rollback;
mode-specific persistence must be tested and documented. Seven reference access
modes and remote connection strings remain in scope. Representations and errors
must not echo connection strings. Reset invalidates outstanding handles.

Serialize whole CHLI operations using one process-wide reentrant lock because
work databases, item options and command output are shared state. Processes
must use spawn rather than reuse a runtime inherited across fork. There is no
parallel-thread throughput promise or public arbitrary-native-call API.

## Values and conversion (planned)

The raw object layer preserves native type, frequency, range and missing-value
categories. NC, NA and ND remain distinct there. Missing Boolean values must
never silently become True. A single missing observation remains one observation
by default; a truly empty raw series remains empty.

The convenience bridge may collapse numeric missing categories to NaN only with
documented interpretation. An explicit Julia-compatible empty convention will
support the reference's one-missing-observation encoding, without claiming that
this ambiguous representation distinguishes every empty and nonempty series.
Unrepresentable date/string series remain owning FAME carriers until a supported
public tsecon representation is selected; no silent coercion into numeric arrays.

Writes validate dtype, range, names and flattened-name collisions before changing
objects. Input NumPy arrays are never mutated to substitute sentinels. Integer
values that cannot survive the requested floating conversion exactly are refused
unless a future explicit lossy policy is selected. Reads return owning data.

Workspace operations default to strict errors. An explicit reporting mode will
return successes and structured failures rather than silently skipping members.
Prefix, glue, case conversion, nested collection and multivariate-column flattening
will retain reference capability. Metadata or frequency conversion losses must
be reported; unsupported FAME features must not look like successful migration.

## Performance

Bulk native reads/writes and contiguous NumPy buffers are the baseline. Optimize
measured conversion hotspots with equivalence tests. A compiled accelerator is
optional until measurements justify the packaging and maintenance cost.
