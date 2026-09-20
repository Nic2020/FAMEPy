# Installation

FAMEPy is a pure-Python package (`py3-none-any` wheel) that depends on
TimeSeriesEconPy (`>=0.0.1.dev3`) and NumPy (`>=1.26`). It needs no
compiler and bundles no FAME component; a separately installed, licensed
FAME runtime is required only for the operations that call CHLI. Python
3.11, 3.12 and 3.13 on Windows and Linux x86-64 are the supported
combinations: TimeSeriesEconPy publishes binary wheels for exactly those
interpreters on `win_amd64` and `manylinux_2_28_x86_64`, so a fresh
installation with `--only-binary=:all:` resolves without building
anything on those six targets. TimeSeriesEconPy also ships macOS arm64
wheels, but FAMEPy is not supported on macOS: it is untested there and no
FAME runtime was available to test with. Other platforms and Python
versions are likewise untested and not advertised. The native campaigns
ran on one Windows x86-64 installation (CHLI release 11.8 file metadata,
library version 11.83) and one Linux x86-64 installation (Red Hat
Enterprise Linux 8, `libchli.so.2`); see [capability
status](capabilities.md).

## From an index

Releases are published as `FAMEPy` by the procedure in
[releasing](releasing.md). Once a version is on PyPI:

```sh
python -m pip install --only-binary=:all: "famepy==<version>"
python -m famepy            # discovery report; exits 1 without FAME
```

Release candidates (`0.1.0rc1`, ...) are installed only when named
explicitly or with `--pre`. Pin the version and record the wheel hash in
any environment that must be reproducible.

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
test suite against each; given `--wheel` and `--sdist` it verifies exactly
those files instead (see [releasing](releasing.md)). The wheel hash printed
there is the identity the validation runner compares with the installed
package.

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
