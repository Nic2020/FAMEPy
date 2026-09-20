# FAMEPy

Python bindings for the FAME CHLI library with integration for
[TimeSeriesEconPy](https://github.com/Nic2020/TimeSeriesEconPy).
The behavioral reference is
[FAME.jl](https://github.com/bankofcanada/FAME.jl).

**Status: release candidate. The public API is spelled as FAME.jl spells
it (`opendb`, `readfame`, `writefame`, `refame`, `do_read`, ...); the
second candidate (0.1.0rc2, unreleased) renames the first candidate's
Python-descriptive names to those spellings, see the
[changelog](CHANGELOG.md). At revision 889d219 the operational core,
the full TimeSeriesEconPy bridge (runtime lifecycle, local databases, raw
object I/O, listing, commands, every reference frequency anchor, every
value kind, workspace reads and writes, the string value text policies),
the opt-in extended error text and the FAME-to-DataEcon migration
workflow passed the consolidated eleven-group validation campaign on one
Windows and one Linux host with an installed FAME, with the FAME.jl
comparisons configured on both; the benchmark harness completed its
scenarios on both; and a bounded read-only comparison of one small
approved remote selection was equal through the reference and this
package on both hosts. See [capability status](docs/capabilities.md),
the [parity ledger](docs/parity.md) and the [changelog](CHANGELOG.md).
These are statements about the inspected installations, not blanket
version coverage.**

Implemented: runtime lifecycle (`init_chli`, `close_chli`), databases
(`opendb` with the five local access modes, `workdb`, `postdb`, `closedb`),
named objects (`FameObject`, `quick_info`, `listdb`, `do_read`, `do_write`)
for precision, numeric, Boolean, date, string and namelist scalars and
series with preserved missing categories, command execution (`fame`) with
recursive INPUT expansion, and the TimeSeriesEconPy bridge (`refame`,
`unfame`, `readfame`, `writefame`): values of every reference kind, all
reference frequency anchors, workspace/mapping/multivariate writes and
workspace reads with name transformation and per-object reporting, with
string values as ASCII, raw bytes or strict UTF-8.
Also: opt-in extended error text, a
[FAME-to-DataEcon migration workflow](docs/migration.md) and a
[benchmark harness](docs/benchmarks.md).
Not implemented: server-connection writes (the reference's remote route is
read-only and neither wrapper binds a named-connection write API),
multivariate reconstruction on read (not in the reference either).

Windows and Linux x86-64 are the intended runtime platforms. A separately
installed, licensed FAME runtime is required for FAME operations; it is not
distributed with this project. FAMEPy depends on TimeSeriesEconPy;
TimeSeriesEconPy remains independent of FAMEPy and FAME.

## Install and try

From a clone, with Python 3.11 or newer:

```sh
python -m pip install .
python -m famepy            # discovery report, exits 1 without FAME
python -m famepy --probe    # symbol presence in a subprocess, no CHLI calls
```

The package builds without FAME, vendor headers or a C compiler and does not
bundle FAME. Set `FAME` to the installation (required for licensing) or
`FAMEPY_LIBRARY` to an absolute trusted library path. Load only trusted
native libraries.

```python
import famepy
from tsecon import TSeries, Workspace, mm

famepy.init_chli()  # once per process
famepy.writefame("synthetic.db", Workspace(ts=TSeries(mm(2020, 1), [1.0, 2.0])), mode="create")
print(famepy.readfame("synthetic.db"))
with famepy.opendb("synthetic.db") as db:
    obj = famepy.do_read(famepy.quick_info(db, "ts"), db)  # the named-object form
    print(famepy.unfame(obj))
famepy.close_chli()  # terminal; use a new process for another runtime
```

The names are FAME.jl's; Python only drops the `!` of `closedb!` and
`do_read!`, spells the `class` filter keyword `class_`, and replaces
do-block forms with context managers. See [usage](docs/usage.md).

See [installation](docs/installation.md) (including offline wheelhouses),
[usage](docs/usage.md), [contracts](docs/contracts.md),
[capability status](docs/capabilities.md), the [parity ledger](docs/parity.md),
[migration](docs/migration.md), [benchmarks](docs/benchmarks.md),
[native validation](docs/native-validation.md),
the [ABI checklist](docs/abi-checklist.md), [releasing](docs/releasing.md),
the [changelog](CHANGELOG.md), [contributing](CONTRIBUTING.md)
and [security/privacy](SECURITY.md).

## Validating against an installed FAME

`python -m famepy.validation --native --scratch <new-dir> --report <file>` runs
the consolidated campaign (lifecycle, databases, raw types, discovery,
commands, bridge, frequencies, workspace, extended errors, migration, text) in
isolated subprocesses inside a fresh run directory of a new or empty
scratch, and writes one schema-validated report without paths or native
text. A group passes only when every required case passed.
`python -m famepy.benchmarks --native ...` produces the separate benchmark
report. See [native validation](docs/native-validation.md).

## Repository conventions

Text files use UTF-8 and LF on Windows and Linux, except Windows batch scripts.
Git attributes preserve database and native-library files as binary.
Local databases, FAME binaries, environments and test reports are ignored.
Do not commit licensed runtime files or private database contents.

## Licensing

Original code uses the MIT license in `LICENSE`. Code tables, ABI declarations
and behavior adapted from FAME.jl retain its BSD 3-Clause notices in
[licenses/FAME.jl.txt](licenses/FAME.jl.txt). Both licenses ship with the package.
FAME itself is a separate proprietary product.
