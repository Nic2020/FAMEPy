# FAMEPy

Python binding foundations for the FAME CHLI library, with planned integration for
[TimeSeriesEconPy](https://github.com/Nic2020/TimeSeriesEconPy).
The behavioral reference is
[FAME.jl](https://github.com/bankofcanada/FAME.jl).

**Status: pre-alpha foundation. Installation, discovery, isolated symbol probing
and status errors are implemented. Database access and time-series conversion
are not implemented yet. No real FAME compatibility has been verified.**

The planned scope includes database access, reading and writing FAME objects,
running FAME commands, and conversion to and from time-series workspaces.
Windows and Linux are the intended runtime platforms. A separately installed,
licensed FAME runtime will be required for FAME operations; it will not be
distributed with this project.

FAMEPy depends on TimeSeriesEconPy. TimeSeriesEconPy and its DataEcon support
will remain independent of FAMEPy and FAME.

The intended distribution name is `FAMEPy`, with Python import `famepy`.
PyPI publication and name availability have not been established.

## Try the foundation

From a clone, with Python 3.11 or newer:

```sh
python -m pip install .
python -m famepy
```

The package builds without FAME, vendor headers or a C compiler. It does not
bundle FAME. If no library is configured, the diagnostic returns JSON with
`library_unavailable` and exits with code one. Importing `famepy` remains usable.
Configure `FAME` or an absolute `FAMEPY_LIBRARY` path to locate an installation.
An optional `python -m famepy --probe` checks symbols in a child process without
calling CHLI functions. Load only trusted native libraries.

See [contracts](docs/contracts.md), [capability status](docs/capabilities.md),
[native validation](docs/native-validation.md), [contributing](CONTRIBUTING.md)
and [security/privacy](SECURITY.md). Diagnostic output excludes paths and raw
loader messages; review it before sharing.

## Repository conventions

Text files use UTF-8 and LF on Windows and Linux, except Windows batch scripts.
Git attributes preserve database and native-library files as binary.
Local databases, FAME binaries, environments and test reports are ignored.
Do not commit licensed runtime files or private database contents.

## Licensing

Original code uses the MIT license in `LICENSE`. Candidate ABI declarations and
status conventions adapted from FAME.jl retain its BSD 3-Clause notices in
[licenses/FAME.jl.txt](licenses/FAME.jl.txt). Both licenses ship with the package.
FAME itself is a separate proprietary product.
