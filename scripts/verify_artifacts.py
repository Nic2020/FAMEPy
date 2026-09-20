# SPDX-License-Identifier: MIT
"""Verify distributions outside the source directory.

Two modes share the same checks:

* Default (no arguments): build an sdist and a wheel into a fresh scratch
  directory, verify the wheel, test it installed, rebuild a wheel from the
  sdist and test that too. This is the ordinary CI check.
* Release (``--wheel`` and ``--sdist``): verify exactly the supplied files.
  The supplied wheel is inspected and tested installed; the supplied sdist is
  extracted and a wheel is rebuilt from it, whose package content must be
  byte-identical to the supplied wheel's, and that rebuilt wheel is tested
  installed as well. Nothing is built as a substitute for the inputs. A
  machine-readable record (``--record``) links the exact filenames, hashes,
  version and source revision of what was verified; it is written only when
  every check passed. Release mode requires the independent C shim and the
  DataEcon extension in the test runs, so a skipped requirement fails.

Every check is fail-closed: an unexpected member, a member whose bytes differ
from the source tree, a hash mismatch, a version that disagrees between the
filename, the metadata and the package, or an input that does not exist,
raises ``VerificationError`` and the process exits with status 1.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

RECORD_SCHEMA = 1
DISTRIBUTION = "FAMEPy"
PACKAGE = "famepy"
WHEEL_NAME = re.compile(r"^famepy-(?P<version>[0-9A-Za-z.!+]+)-py3-none-any\.whl$")
SDIST_NAME = re.compile(r"^famepy-(?P<version>[0-9A-Za-z.!+]+)\.tar\.gz$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_SUFFIXES = (".dll", ".so", ".pyd", ".db", ".log", ".sqlite", ".env")


class VerificationError(Exception):
    """A distribution failed a fail-closed check."""


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(list(args), cwd=cwd, check=True, env=env)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


# ----------------------------------------------------------------- inspection


def source_package_files(root: Path) -> dict[str, bytes]:
    """The package's ``.py`` files in the source tree, keyed by wheel member path."""
    package = root / "src" / PACKAGE
    files: dict[str, bytes] = {}
    for file in sorted(package.rglob("*.py")):
        if "__pycache__" in file.parts:
            continue
        files[PACKAGE + "/" + file.relative_to(package).as_posix()] = file.read_bytes()
    require(bool(files), "no package sources found under src/")
    marker = package / "py.typed"
    require(marker.is_file(), "source py.typed missing")
    files[PACKAGE + "/py.typed"] = marker.read_bytes()
    return files


def package_content_digest(members: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(members):
        digest.update(name.encode() + b"\0" + members[name] + b"\0")
    return digest.hexdigest()


def read_record(archive: zipfile.ZipFile, dist_info: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for line in archive.read(f"{dist_info}/RECORD").decode().splitlines():
        if not line:
            continue
        name, digest, size = line.rsplit(",", 2)
        entries[name] = (digest, size)
    return entries


def inspect_wheel(
    wheel: Path, sources: dict[str, bytes], *, expect_version: str | None
) -> dict[str, Any]:
    """Fail-closed inventory, metadata, RECORD and content checks of one wheel."""
    match = WHEEL_NAME.fullmatch(wheel.name)
    require(match is not None, f"unexpected wheel filename {wheel.name!r}")
    assert match is not None
    version = match.group("version")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate wheel members")
        dist_infos = sorted(
            {n.split("/")[0] for n in names if n.split("/")[0].endswith(".dist-info")}
        )
        require(dist_infos == [f"famepy-{version}.dist-info"], f"unexpected dist-info {dist_infos}")
        dist_info = dist_infos[0]
        for name in names:
            require(
                not name.startswith("/") and ".." not in name.split("/"), f"unsafe member {name!r}"
            )
            require(not name.lower().endswith(FORBIDDEN_SUFFIXES), f"forbidden member {name!r}")
            require(
                "__pycache__" not in name and not name.endswith(".pyc"), f"bytecode member {name!r}"
            )
            require(
                name.startswith(PACKAGE + "/") or name.startswith(dist_info + "/"),
                f"member outside the package {name!r}",
            )
        allowed_metadata = {
            "METADATA",
            "WHEEL",
            "RECORD",
            "entry_points.txt",
            "licenses/LICENSE",
            "licenses/licenses/FAME.jl.txt",
        }
        require(
            all(
                n.split("/", 1)[1] in allowed_metadata
                for n in names
                if n.startswith(dist_info + "/")
            ),
            "unexpected dist-info member",
        )
        # Package content: exactly the source files, byte for byte, plus py.typed.
        package_members = {n: archive.read(n) for n in names if n.startswith(PACKAGE + "/")}
        expected = dict(sources)
        require(PACKAGE + "/py.typed" in package_members, "py.typed missing from the wheel")
        missing = sorted(set(expected) - set(package_members))
        extra = sorted(set(package_members) - set(expected))
        require(not missing, f"wheel lacks source members {missing}")
        require(not extra, f"wheel has members not in the source tree {extra}")
        changed = sorted(n for n in sources if package_members[n] != sources[n])
        require(not changed, f"wheel members differ from the source tree {changed}")
        install_metadata = {
            name.split("/", 1)[1]: archive.read(name)
            for name in names
            if name in (f"{dist_info}/METADATA", f"{dist_info}/entry_points.txt")
        }
        # Metadata.
        metadata = email.parser.Parser().parsestr(archive.read(f"{dist_info}/METADATA").decode())
        require(
            metadata["Name"] == DISTRIBUTION, f"unexpected distribution name {metadata['Name']!r}"
        )
        require(metadata["Version"] == version, "METADATA version differs from the filename")
        require(
            metadata["License-Expression"] == "MIT AND BSD-3-Clause",
            "unexpected license expression",
        )
        licenses = {n.split("/", 1)[1] for n in names if n.startswith(f"{dist_info}/licenses/")}
        require(
            licenses == {"licenses/LICENSE", "licenses/licenses/FAME.jl.txt"},
            f"unexpected license files {sorted(licenses)}",
        )
        wheel_meta = archive.read(f"{dist_info}/WHEEL").decode()
        require(
            "Tag: py3-none-any" in wheel_meta and "Root-Is-Purelib: true" in wheel_meta,
            "not a pure py3-none-any wheel",
        )
        # RECORD covers every member with the right digest and size.
        record = read_record(archive, dist_info)
        require(set(record) == set(names), "RECORD does not list exactly the wheel members")
        for name in names:
            if name == f"{dist_info}/RECORD":
                require(record[name] == ("", ""), "RECORD must not hash itself")
                continue
            data = archive.read(name)
            digest, size = record[name]
            require(size == str(len(data)), f"RECORD size mismatch for {name!r}")
            algorithm, _, value = digest.partition("=")
            require(algorithm == "sha256", f"RECORD digest algorithm for {name!r} is {algorithm!r}")
            expected_digest = (
                base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            )
            require(value == expected_digest, f"RECORD digest mismatch for {name!r}")
        # The version inside the package agrees with the metadata.
        init = package_members[PACKAGE + "/__init__.py"].decode()
        found = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)
        require(
            found is not None and found.group(1) == version,
            "__version__ differs from the wheel version",
        )
    if expect_version is not None:
        require(
            version == expect_version,
            f"wheel version {version} is not the expected {expect_version}",
        )
    return {
        "filename": wheel.name,
        "sha256": sha256_file(wheel),
        "size": wheel.stat().st_size,
        "version": version,
        "members": len(names),
        "package_content_sha256": package_content_digest(package_members),
        "install_metadata_sha256": package_content_digest(install_metadata),
    }


def extract_sdist(sdist: Path, destination: Path) -> Path:
    """Extract an sdist safely and return the single top-level directory."""
    destination.mkdir()
    with tarfile.open(sdist) as archive:
        # Reject traversal and links even for our own freshly built artifact.
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            require(
                target.is_relative_to(destination.resolve()), f"unsafe sdist member {member.name!r}"
            )
            require(member.isfile() or member.isdir(), f"non-regular sdist member {member.name!r}")
        # All members were checked above; also supports the original Python 3.11 API.
        if hasattr(tarfile, "data_filter"):
            archive.extractall(destination, filter="data")
        else:
            archive.extractall(destination)
    entries = list(destination.iterdir())
    require(len(entries) == 1 and entries[0].is_dir(), "sdist must contain one top-level directory")
    return entries[0]


def inspect_sdist(
    sdist: Path,
    sources: dict[str, bytes],
    *,
    expect_version: str | None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    match = SDIST_NAME.fullmatch(sdist.name)
    require(match is not None, f"unexpected sdist filename {sdist.name!r}")
    assert match is not None
    version = match.group("version")
    if expect_version is not None:
        require(
            version == expect_version,
            f"sdist version {version} is not the expected {expect_version}",
        )
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        require(len(names) == len(set(names)), "duplicate sdist members")
        prefix = f"famepy-{version}/"
        require(
            all(n == prefix.rstrip("/") or n.startswith(prefix) for n in names),
            "sdist members outside the versioned directory",
        )
        for name in names:
            require(
                ".." not in name.split("/") and not name.startswith("/"),
                f"unsafe sdist member {name!r}",
            )
            require(
                not name.lower().endswith(FORBIDDEN_SUFFIXES), f"forbidden sdist member {name!r}"
            )
            require(
                "__pycache__" not in name and "/build/" not in name,
                f"build product in sdist {name!r}",
            )
        files = {m.name: m for m in members if m.isfile()}
        require(all(m.isfile() or m.isdir() for m in members), "non-regular sdist member")

        def read(name: str) -> bytes:
            handle = archive.extractfile(files[prefix + name])
            assert handle is not None
            return handle.read()

        require(prefix + "PKG-INFO" in files, "PKG-INFO missing")
        info = email.parser.Parser().parsestr(read("PKG-INFO").decode())
        require(
            info["Name"] == DISTRIBUTION and info["Version"] == version,
            "PKG-INFO name/version mismatch",
        )
        pyproject = read("pyproject.toml").decode()
        require(
            f'version = "{version}"' in pyproject,
            "pyproject.toml version differs from the sdist version",
        )
        for required in ("LICENSE", "licenses/FAME.jl.txt", "README.md", "CHANGELOG.md", "uv.lock"):
            require(prefix + required in files, f"{required} missing from the sdist")
        if source_root is not None:
            for name in files:
                relative = name[len(prefix) :]
                if relative == "PKG-INFO":
                    continue  # Generated distribution metadata, checked above.
                original = (source_root / relative).resolve()
                require(
                    original.is_relative_to(source_root.resolve()) and original.is_file(),
                    f"sdist member absent from source tree: {relative}",
                )
                require(
                    read(relative) == original.read_bytes(),
                    f"sdist sources differ from source tree: {relative}",
                )
        source_members = {n: read("src/" + n) for n in sources if prefix + "src/" + n in files}
        missing = sorted(set(sources) - set(source_members))
        require(not missing, f"sdist lacks source members {missing}")
        extra = sorted(
            n[len(prefix) + 4 :]
            for n in files
            if n.startswith(prefix + "src/" + PACKAGE + "/")
            and n.endswith(".py")
            and n[len(prefix) + 4 :] not in sources
        )
        require(not extra, f"sdist has package sources not in the tree {extra}")
        changed = sorted(n for n in sources if source_members[n] != sources[n])
        require(not changed, f"sdist sources differ from the source tree {changed}")
    if expect_version is not None:
        require(
            version == expect_version,
            f"sdist version {version} is not the expected {expect_version}",
        )
    return {
        "filename": sdist.name,
        "sha256": sha256_file(sdist),
        "size": sdist.stat().st_size,
        "version": version,
        "members": len(names),
    }


# ------------------------------------------------------------------- testing


def test_installed(wheel: Path, root: Path, scratch: Path, *, require_extensions: bool) -> None:
    """Install ``wheel`` in an isolated target directory and run the suite against it."""
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
    if require_extensions:
        require(
            bool(environment.get("FAMEPY_TEST_SHIM")),
            "FAMEPY_TEST_SHIM must point at the built shim for release checks",
        )
        environment["FAMEPY_REQUIRE_SHIM"] = "1"
        environment["FAMEPY_REQUIRE_DATAECON"] = "1"
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


def rebuild_from_sdist(sdist: Path, scratch: Path) -> Path:
    extracted = extract_sdist(sdist, scratch / "source")
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
    wheels = list(rebuilt.glob("*.whl"))
    require(len(wheels) == 1, "the sdist must rebuild into exactly one wheel")
    return wheels[0]


Tester = Callable[[Path, Path], None]


def verify_release(
    wheel: Path,
    sdist: Path,
    root: Path,
    scratch: Path,
    *,
    source_sha: str | None,
    expect_version: str | None,
    tester: Tester,
) -> dict[str, Any]:
    """Verify the supplied wheel and sdist and return the verification record."""
    require(wheel.is_file(), f"wheel not found: {wheel.name}")
    require(sdist.is_file(), f"sdist not found: {sdist.name}")
    require(
        source_sha is None or re.fullmatch(r"[0-9a-f]{40}", source_sha) is not None,
        "source SHA must be 40 hex digits",
    )
    sources = source_package_files(root)
    wheel_info = inspect_wheel(wheel, sources, expect_version=expect_version)
    sdist_info = inspect_sdist(sdist, sources, expect_version=expect_version, source_root=root)
    require(wheel_info["version"] == sdist_info["version"], "wheel and sdist versions differ")
    scratch = scratch.resolve()
    scratch.mkdir(parents=True)
    tester(wheel, scratch / "wheel-check")
    rebuilt = rebuild_from_sdist(sdist, scratch)
    rebuilt_info = inspect_wheel(rebuilt, sources, expect_version=wheel_info["version"])
    require(
        rebuilt_info["package_content_sha256"] == wheel_info["package_content_sha256"],
        "the wheel rebuilt from the sdist does not contain the supplied wheel's package files",
    )
    require(
        rebuilt_info["install_metadata_sha256"] == wheel_info["install_metadata_sha256"],
        "rebuilt wheel installation metadata differs from the supplied wheel",
    )
    tester(rebuilt, scratch / "sdist-check")
    # Inputs unchanged by the checks.
    require(
        sha256_file(wheel) == wheel_info["sha256"] and sha256_file(sdist) == sdist_info["sha256"],
        "inputs changed during verification",
    )
    return {
        "schema_version": RECORD_SCHEMA,
        "result": "passed",
        "distribution": DISTRIBUTION,
        "version": wheel_info["version"],
        "source_sha": source_sha,
        "wheel": wheel_info,
        "sdist": sdist_info,
        "rebuilt_wheel": {
            "sha256": rebuilt_info["sha256"],
            "package_content_sha256": rebuilt_info["package_content_sha256"],
            "install_metadata_sha256": rebuilt_info["install_metadata_sha256"],
        },
        "tests": {"installed_wheel": "passed", "sdist_rebuilt_wheel": "passed"},
        "required_extensions": ["shim", "dataecon"],
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }


def default_mode(root: Path) -> None:
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
    sdist = next(distributions.glob("*.tar.gz"))
    require_extensions = os.environ.get("FAMEPY_REQUIRE_DATAECON") == "1"

    def tester(candidate: Path, check_dir: Path) -> None:
        test_installed(candidate, root, check_dir, require_extensions=require_extensions)

    record = verify_release(
        wheel, sdist, root, scratch / "verify", source_sha=None, expect_version=None, tester=tester
    )
    print(
        json.dumps(
            {
                "wheel": record["wheel"]["sha256"],
                "sdist": record["sdist"]["sha256"],
                "scratch": scratch.name,
            }
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify distributions outside the source directory."
    )
    parser.add_argument("--wheel", type=Path, help="the exact wheel to verify (release mode)")
    parser.add_argument("--sdist", type=Path, help="the exact sdist to verify (release mode)")
    parser.add_argument("--source-sha", help="the 40-hex source revision to record")
    parser.add_argument("--expect-version", help="the version the distributions must carry")
    parser.add_argument(
        "--record", type=Path, help="where to write the verification record (release mode)"
    )
    parser.add_argument("--scratch", type=Path, help="a new scratch directory (release mode)")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.wheel is None and args.sdist is None:
            require(
                not (args.record or args.scratch or args.expect_version or args.source_sha),
                "those options need --wheel and --sdist",
            )
            default_mode(root)
            return 0
        require(
            args.wheel is not None and args.sdist is not None,
            "release mode needs both --wheel and --sdist",
        )
        assert args.wheel is not None and args.sdist is not None
        scratch = (args.scratch or root / "build" / ("release-" + uuid.uuid4().hex)).resolve()
        require(not scratch.exists(), "the scratch directory must not exist")

        def tester(candidate: Path, check_dir: Path) -> None:
            test_installed(candidate, root, check_dir, require_extensions=True)

        record = verify_release(
            args.wheel.resolve(),
            args.sdist.resolve(),
            root,
            scratch,
            source_sha=args.source_sha,
            expect_version=args.expect_version,
            tester=tester,
        )
    except VerificationError as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    if args.record is not None:
        args.record.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "result": record["result"],
                "version": record["version"],
                "wheel": record["wheel"]["sha256"],
                "sdist": record["sdist"]["sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
