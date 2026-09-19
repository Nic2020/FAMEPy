# Installation

FAMEPy is a pure-Python package (`py3-none-any` wheel) that depends on
TimeSeriesEconPy and NumPy. It needs no compiler and bundles no FAME
component; a separately installed, licensed FAME runtime is required only
for the operations that call CHLI. Python 3.11 or newer on Windows or Linux
x86-64 is the intended platform set; other platforms and Python versions are
untested and not advertised.

## From a checkout

```sh
python -m pip install .
python -m famepy            # discovery report; exits 1 without FAME
python -m famepy --probe    # symbol presence in a subprocess, no CHLI calls
```

Set `FAME` to the installation root (the runtime needs it for licensing) or
`FAMEPY_LIBRARY` to an absolute trusted library path; see
[contracts](contracts.md) for the precedence and the trusted-root rules.
Load only trusted native libraries.

## Building the artifacts

```sh
python -m pip install build
python -m build             # dist/famepy-<version>.tar.gz and the wheel
```

`scripts/verify_artifacts.py` installs the built wheel and a wheel rebuilt
from the sdist in isolated environments outside the checkout and runs the
test suite against each; the wheel hash printed there is the identity the
validation runner compares with the installed package.

## Offline wheelhouse

Protected hosts usually have no index access. Build the wheelhouse on a
connected machine for the target Python version and platform, transfer the
directory, and install from it only:

Connected machine (match the target interpreter's version and platform):

```sh
python -m pip download --dest ./wheelhouse --only-binary=:all: \
    --python-version 3.11 --platform win_amd64 --implementation cp \
    "TimeSeriesEconPy>=0.0.1.dev3" "numpy>=1.26"
python -m pip wheel --no-deps --wheel-dir ./wheelhouse <path-to-FAMEPy-checkout>
```

Use `--platform manylinux_2_28_x86_64` for the Linux host and repeat per
Python version. TimeSeriesEconPy ships compiled extensions, so its wheel
must match the target interpreter exactly; the FAMEPy wheel is the same on
every platform.

Target host:

```sh
python -m pip install --no-index --find-links ./wheelhouse famepy
python -m famepy --probe
```

Record the wheel hashes (`sha256sum` or `Get-FileHash`) with the
installation; the validation runner takes the FAMEPy wheel path with
`--wheel` and refuses to run native groups when the installed sources do
not match it. Nothing in the wheelhouse contains vendor files; the FAME
runtime stays where the vendor installer put it.

## Optional dependencies

- The DataEcon native extension used by the migration workflow ships inside
  the TimeSeriesEconPy wheels; when it cannot load, the migration tests skip
  with that reason and the runner's `migration` group is blocked.
- Julia with FAME.jl and TimeSeriesEcon is needed only for the differential
  checks and the same-host benchmark comparison; both are opt-in flags.
- Building the independent C test shim (`scripts/build_test_shim.py`) needs
  a C compiler and is for development and CI only.
