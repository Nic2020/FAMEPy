# SPDX-License-Identifier: MIT
"""Build wheel/sdist and test installed wheels outside the source directory."""

import json
import os
import subprocess
import sys
import tarfile
import uuid
import zipfile
from pathlib import Path


def run(*args: str, cwd: Path) -> None:
    subprocess.run(list(args), cwd=cwd, check=True)


def verify_wheel(wheel: Path, root: Path, scratch: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        assert any(name.endswith("/licenses/licenses/FAME.jl.txt") for name in names)
        assert "famepy/py.typed" in names
        assert not any(name.endswith((".dll", ".so", ".pyd", ".db")) for name in names)
    scratch.mkdir()
    # uv is the project environment manager; use the locked existing dependency
    # environment as the base, but install FAMEPy in an isolated target directory.
    target = scratch / "installed"
    run(
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--no-deps",
        "--no-index",
        "--target",
        str(target),
        str(wheel),
        cwd=scratch,
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(target)
    environment["FAMEPY_EXPECTED_ROOT"] = str(target)
    command = (
        "import famepy, pathlib; "
        f"assert pathlib.Path(famepy.__file__).is_relative_to(pathlib.Path({str(target)!r})); "
        "from importlib.metadata import version; "
        "assert famepy.__version__ == version('FAMEPy')"
    )
    subprocess.run([sys.executable, "-c", command], cwd=scratch, env=environment, check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(root / "tests"),
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(scratch / "pytest"),
        ],
        cwd=scratch,
        env=environment,
        check=True,
    )
    print(json.dumps({"artifact": wheel.name, "installed_tests": "passed"}))


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    scratch = root / "build" / ("artifacts-" + uuid.uuid4().hex)
    scratch.mkdir(parents=True)
    distributions = scratch / "dist"
    run(
        sys.executable,
        "-m",
        "build",
        "--no-isolation",
        "--sdist",
        "--wheel",
        "--outdir",
        str(distributions),
        str(root),
        cwd=scratch,
    )
    wheel = next(distributions.glob("*.whl"))
    assert wheel.name.endswith("py3-none-any.whl")
    verify_wheel(wheel, root, scratch / "wheel-check")
    source = scratch / "source"
    source.mkdir()
    with tarfile.open(next(distributions.glob("*.tar.gz"))) as archive:
        # Reject traversal and links even for our own freshly built artifact.
        for member in archive.getmembers():
            destination = (source / member.name).resolve()
            if not destination.is_relative_to(source) or not (member.isfile() or member.isdir()):
                raise ValueError("Unexpected source archive member")
        # All members were checked above; also supports the original Python 3.11 API.
        if hasattr(tarfile, "data_filter"):
            archive.extractall(source, filter="data")
        else:
            archive.extractall(source)
    extracted = next(source.iterdir())
    rebuilt = scratch / "rebuilt"
    run(
        sys.executable,
        "-m",
        "build",
        "--no-isolation",
        "--wheel",
        "--outdir",
        str(rebuilt),
        str(extracted),
        cwd=scratch,
    )
    verify_wheel(next(rebuilt.glob("*.whl")), root, scratch / "sdist-check")


if __name__ == "__main__":
    main()
