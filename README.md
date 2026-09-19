# FAMEPy

Python bindings for the FAME CHLI library with integration for
[TimeSeriesEconPy](https://github.com/Nic2020/TimeSeriesEconPy).
The behavioral reference is
[FAME.jl](https://github.com/bankofcanada/FAME.jl).

**Status: pre-alpha. The operational core (runtime lifecycle, local
databases, raw object I/O, listing, commands and the monthly precision
bridge) passed the consolidated validation campaign on one Windows and one
Linux host with an installed FAME. The full TimeSeriesEconPy bridge (every
reference frequency anchor, every value kind, workspace reads and writes) is
implemented and tested offline against an in-memory fake backend and an
independent C shim; its first native run passed the workspace group and the
calendar checks and failed on two invalid fixtures, corrected in this
revision, so it awaits a new native run
(see [capability status](docs/capabilities.md)).**

Implemented: runtime lifecycle, databases (the five local access modes, work
database, explicit posting), raw scalar and series I/O for precision,
numeric, Boolean, date, string and namelist objects with preserved missing
categories, wildcard listing with filters, command execution with recursive
INPUT expansion, and the TimeSeriesEconPy bridge: values of every reference
kind, all reference frequency anchors, workspace/mapping/multivariate
writes and workspace reads with name transformation and per-object
reporting.
Not implemented: extended native error text (blocked on a vendor
declaration), server-connection writes, multivariate reconstruction on read
(not in the reference either).

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
from famepy import bridge
from tsecon import TSeries, mm

famepy.initialize()  # once per process
bridge.write_tseries("synthetic.db", "ts", TSeries(mm(2020, 1), [1.0, 2.0]), mode="create")
print(bridge.read_tseries("synthetic.db", "ts"))
famepy.finalize()  # terminal; use a new process for another runtime
```

See [usage](docs/usage.md), [contracts](docs/contracts.md),
[capability status](docs/capabilities.md), [native validation](docs/native-validation.md),
the [ABI checklist](docs/abi-checklist.md), [contributing](CONTRIBUTING.md)
and [security/privacy](SECURITY.md).

## Validating against an installed FAME

`python -m famepy.validation --native --scratch <new-dir> --report <file>` runs
the consolidated campaign (lifecycle, databases, raw types, discovery,
commands, bridge, frequencies, workspace) in isolated subprocesses inside a fresh run directory of a
new or empty scratch, and writes one schema-validated report without paths
or native text. A group passes only when every required case passed.
See [native validation](docs/native-validation.md).

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
