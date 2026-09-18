# Per-function ABI checklist

This table lists every native call the package and its validation runner use,
with the candidate declaration the package binds. Before the first native
campaign on a host, compare each row with the installed header and record
match or difference per row (conclusions only, never header text). A mismatch
blocks the operations that use that row, not unrelated groups.

Conventions: `cfm*` functions return `void` and take a leading `int *status`
(the binding initializes it to -1 and treats any nonzero result as failure);
`fame_*` functions return `int` status. `int` is 32-bit signed, `index` is the
64-bit signed `fame_index`, `range` is the 24-byte structure `{int frequency;
index start; index end}` with offsets 0/8/16 and alignment 8. Text is passed
as NUL-terminated `char *`; the package sends NUL-free bytes and accepts only
ASCII `str` values in this release. "Owned" means the package allocates the
buffer and keeps it alive for the call. Every text argument carries its
direction: `in/out text` is text the older calling convention lets the
library rewrite in place (trimmed, upper-cased), so the binding passes an
owned NUL-terminated copy (`char *`), never the caller's bytes; `input text`
is documented as input only (`const char *` in the newer convention) and is
passed as given. Pointer widths are the same either way; the distinction
is ownership, and it must be confirmed against the installed header per
host like every other row rather than inferred from a passing width check.

| Function | Candidate arguments (after status where applicable) | Direction and ownership | Used by |
|---|---|---|---|
| cfmini | none | initializes the library; requires the FAME environment variable for licensing | lifecycle |
| cfmfin | none | finalizes; all handles become invalid | lifecycle |
| cfmver | `float *version` | output, owned 4-byte float | lifecycle |
| cfmfame | `char *command` | input text, NUL-terminated, at most 2**20 bytes (reference-derived bound) | commands |
| cfmopwk | `int *key` | output database key | database |
| cfmopdb | `int *key, char *name, int mode` | output key; in/out text (owned copy; the library trims it); mode 1-5 for this local open (6 and 7 are modes of the open on a named server connection, which is not bound, and are refused before the call; the local open is documented to return the bad-mode status for them) | database |
| cfmpodb | `int key` | posts updates | database |
| cfmcldb | `int key` | closes without posting | database |
| cfmsopt | `char *option, char *value` | in/out texts (owned copies; the library trims and upper-cases both), for example `ITEM CLASS` / `ON`; frequency selections use the documented family words (`ITEM FREQUENCY MONTHLY`, ...) and index selections `ITEM INDEX CASE` / `DATE` | discovery |
| cfmnlen | `int key, char *name, int item(-1), int *length` | in/out name (owned copy); output length excluding terminator | raw data |
| cfmgtnl | `int key, char *name, int item(-1), char *buffer, int capacity, int *length` | in/out name (owned copy); owned writable buffer of capacity+1 bytes; the whole list comes back as members within braces separated by commas, in the library's own layout | raw data |
| cfmwtnl | `int key, char *name, int item(-1), char *text` | in/out name and in/out list text (owned copies; the library trims and upper-cases them) | raw data |
| cfmdlob | `int key, char *name` | in/out name (owned copy); deletes; status 13 when absent | raw data |
| cfmnwob | `int key, char *name, int class, int frequency, int type, int basis, int observed` | in/out name (owned copy); creates an empty object | raw data |
| cfmispm | `double value, int *type` | classification output 0-4 | raw data |
| cfmisnm | `float value, int *type` | classification output | raw data |
| cfmisbm | `int value, int *type` | classification output | raw data |
| cfmissm | `char *value, int *type` | input text; classification output | raw data |
| fame_index_to_year_period | `int frequency, index value, int *year, int *period` | outputs owned | bridge |
| fame_year_period_to_index | `int frequency, index *out, int year, int period` | 64-bit output (the reference declares this inconsistently; the 64-bit form is used) | bridge |
| fame_quick_info | `int key, const char *name, int *class, int *type, int *frequency, index *first, index *last` | outputs owned | all |
| fame_init_wildcard | `int key, int *cursor, const char *pattern, int 0, const char *NULL` | output cursor key | discovery |
| fame_get_next_wildcard | `int cursor, char *name, int *class, int *type, int *frequency, index *first, index *last, int capacity, int *length` | owned name buffer of capacity+1 bytes; 242-byte capacity; status 18 when longer, with the full length in `length` | discovery |
| fame_free_wildcard | `int cursor` | releases the cursor; always called in `finally` | discovery |
| fame_get_precisions | `int key, const char *name, range *range_or_NULL, double *values` | owned float64 buffer of range length (1 for scalars) | raw data |
| fame_get_numerics | same with `float *values` | owned float32 buffer | raw data |
| fame_get_booleans | same with `int *values` | owned int32 buffer | raw data |
| fame_get_dates | same with `index *values` | owned int64 buffer | raw data |
| fame_len_strings | `int key, const char *name, range *, int *lengths` | owned int32 buffer of range length | raw data |
| fame_get_strings | `int key, const char *name, range *, char **values, const int *inlen, int *outlen(NULL)` | owned array of owned buffers sized from `fame_len_strings` plus terminators; `inlen` carries those capacities; the optional `outlen` output lengths are not requested (NULL) | raw data |
| fame_write_precisions | `int key, const char *name, range *, const double *values` | caller's validated float64 buffer, never converted | raw data |
| fame_write_numerics | same with `const float *` | caller's float32 buffer | raw data |
| fame_write_booleans | same with `const int *` | caller's int32 buffer | raw data |
| fame_write_dates | `int key, const char *name, range *, int type, const index *values` | caller's int64 buffer; `type` is the frequency code of the dates | raw data |
| fame_write_strings | `int key, const char *name, range *, char **values` | owned array of pointers to NUL-terminated byte strings | raw data |
| fame_date_missing_type | `index value, int *type` | classification output | raw data |
| cfmlerr | not declared | needed to size extended-error text; presence probed only, no call until the installed declaration (return convention, argument types and directions) is recorded | none |

## Native globals

| Symbol | Candidate type | Use |
|---|---|---|
| FAME_INDEX_NC, FAME_INDEX_NA, FAME_INDEX_ND | signed 64-bit | date/index missing sentinels; NC marks empty series ranges |
| FPRCNC, FPRCNA, FPRCND | double | precision sentinels |
| FNUMNC, FNUMNA, FNUMND | float | numeric sentinels |
| FBOONC, FBOONA, FBOOND | int | Boolean sentinels |
| FSTRNC, FSTRNA, FSTRND | `char[3]` arrays (reported on both inspected installations) | string sentinels; read as bounded arrays, never as pointers |

The package reads the globals once after initialization and compares data
against them by bit pattern. The validation campaign checks that classifier
against the library's own `cfmis*m`/`fame_date_missing_type` results before
the bitwise form is relied on.

## Constants

| Constant | Value used | Source |
|---|---|---|
| HSUCC | 0 | reference |
| HBMODE | 5 | vendor status help (bad or unauthorized access mode, or database not open for the requested access); returned by the local open for modes 6 and 7 on both inspected installations |
| HNOOBJ | 13 | reference; header-confirmed |
| HTRUNC | 18 | header-confirmed on both inspected installations |
| HBOPT | 67 | reference; header-confirmed |
| HFMENV / HLICFL | 97 / 98 | vendor status help (licensing environment / license file) |
| HFAMER | 513 | header-confirmed; extended text needs the opt-in path |
| HNAMLEN | 242 (buffer 243) | header-confirmed v4 name capacity |
| HNLALL | -1 | reference namelist selector |
| class, type, frequency, basis, observed codes | as in `famepy._constants` | reference tables |

Unknowns to record per host: the `cfmlerr` declaration, the text encoding the
library expects for names, paths and commands, and whether initialization has
root-level dependency requirements beyond the library directory. Both hosts
confirmed that initialization happens once per process and that
finalization is the last native call. Per host, the campaign records rather
than assumes: the rule for missing observations at the ends of a written
range per value type (both inspected installations dropped leading and
trailing ND, emptied all-ND ranges and kept NC and NA endpoints); the
layout the library uses for a whole namelist (nine bytes for a three-member
list on both); and the counts the documented family and index words select
on their own. Whether a given installation's header matches these
candidate directions, and whether its release honors the documented family
and index words, is confirmed per host by the checklist review and the
campaign, never assumed from documentation.
