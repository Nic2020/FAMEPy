# SPDX-License-Identifier: MIT
"""Check that a downloaded release artifact is the one an operator approved.

The publish workflow downloads the artifact of one earlier ``release``
(rehearsal) run and calls this script before anything is uploaded. It
takes the run and artifact metadata the workflow fetched from the GitHub
API, the directory the artifact was extracted into, and the identities the
operator typed into the dispatch (run id, wheel and sdist SHA-256), and
refuses unless every one of the following holds:

* the run belongs to this repository, executed the approved workflow file,
  completed successfully, and was started from the commit being released;
* exactly one artifact of the expected name exists for that run, is not
  expired, and was produced by that same run and commit;
* the downloaded directory holds exactly the wheel, the sdist, the
  verification record and the checksum file of that run, the checksums
  agree with the bytes, and the operator-supplied digests agree too;
* the verification record says the checks passed with both required
  extensions, names those exact files and digests, and carries the
  released version and commit; the wheel's own metadata carries the same
  version.

On success the two distributions (and nothing else) are copied into the
upload directory and a summary is written; on any failure nothing is
copied and the exit status is 1. Only standard-library modules are used so
the publish job needs no project environment.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[1-9][0-9]{0,18}$")
VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$")
RECORD_NAME = "verification.json"
SUMS_NAME = "SHA256SUMS"


class PromotionError(Exception):
    """The artifact or its provenance does not match what was approved."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PromotionError(message)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PromotionError(f"cannot read {path.name}: {error.__class__.__name__}") from None


def check_run(run: Any, *, run_id: int, repository: str, workflow_path: str, head_sha: str) -> None:
    require(isinstance(run, dict), "run metadata is not an object")
    require(run.get("id") == run_id, "run id differs from the approved run")
    require(
        run.get("repository", {}).get("full_name") == repository,
        "run belongs to another repository",
    )
    require(run.get("path") == workflow_path, "run did not execute the approved workflow file")
    require(run.get("status") == "completed", "run is not completed")
    require(run.get("conclusion") == "success", "run did not succeed")
    require(run.get("head_sha") == head_sha, "run was not started from the released commit")
    require(run.get("event") == "workflow_dispatch", "run was not an operator dispatch")


def check_artifacts(
    listing: Any, *, run_id: int, head_sha: str, artifact_name: str
) -> dict[str, Any]:
    require(
        isinstance(listing, dict) and isinstance(listing.get("artifacts"), list),
        "artifact listing malformed",
    )
    matches = [
        a for a in listing["artifacts"] if isinstance(a, dict) and a.get("name") == artifact_name
    ]
    require(
        len(matches) == 1,
        f"expected exactly one artifact named {artifact_name!r}, found {len(matches)}",
    )
    artifact = matches[0]
    require(artifact.get("expired") is False, "artifact has expired")
    run = artifact.get("workflow_run") or {}
    require(run.get("id") == run_id, "artifact was produced by another run")
    require(run.get("head_sha") == head_sha, "artifact was produced from another commit")
    return {
        "id": artifact.get("id"),
        "size_in_bytes": artifact.get("size_in_bytes"),
        "digest": artifact.get("digest"),
    }


def check_distributions(
    dist: Path, *, version: str, source_sha: str, wheel_sha256: str, sdist_sha256: str
) -> dict[str, Any]:
    require(dist.is_dir(), "download directory missing")
    entries = sorted(p.name for p in dist.iterdir())
    require(all((dist / n).is_file() for n in entries), "unexpected directory inside the artifact")
    wheel_name = f"famepy-{version}-py3-none-any.whl"
    sdist_name = f"famepy-{version}.tar.gz"
    require(
        entries == sorted([wheel_name, sdist_name, RECORD_NAME, SUMS_NAME]),
        f"artifact members are {entries}, not the expected four",
    )
    wheel, sdist = dist / wheel_name, dist / sdist_name
    actual = {wheel_name: sha256_file(wheel), sdist_name: sha256_file(sdist)}
    # Checksum file written by the rehearsal run.
    sums: dict[str, str] = {}
    for line in (dist / SUMS_NAME).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split()
        require(len(parts) == 2, "malformed checksum line")
        sums[parts[1].lstrip("*")] = parts[0]
    require(sums == actual, "SHA256SUMS does not match the artifact bytes")
    # Operator-approved digests.
    require(actual[wheel_name] == wheel_sha256, "wheel digest differs from the approved digest")
    require(actual[sdist_name] == sdist_sha256, "sdist digest differs from the approved digest")
    # Verification record from the rehearsal run.
    record = load_json(dist / RECORD_NAME)
    require(
        isinstance(record, dict) and record.get("schema_version") == 1,
        "unknown verification record schema",
    )
    require(record.get("result") == "passed", "verification record does not say passed")
    require(
        record.get("distribution") == "FAMEPy", "verification record is for another distribution"
    )
    require(
        record.get("version") == version,
        "verification record version differs from the release version",
    )
    require(
        record.get("source_sha") == source_sha, "verification record was made from another commit"
    )
    for key, name in (("wheel", wheel_name), ("sdist", sdist_name)):
        entry = record.get(key) or {}
        require(entry.get("filename") == name, f"verification record names another {key}")
        require(
            entry.get("sha256") == actual[name],
            f"verification record {key} digest differs from the bytes",
        )
        require(entry.get("version") == version, f"verification record {key} version differs")
    tests = record.get("tests") or {}
    require(
        tests.get("installed_wheel") == "passed" and tests.get("sdist_rebuilt_wheel") == "passed",
        "installed tests not recorded as passed",
    )
    require(
        set(record.get("required_extensions") or []) == {"shim", "dataecon"},
        "both extensions were not required",
    )
    rebuilt = record.get("rebuilt_wheel") or {}
    for key in ("package_content_sha256", "install_metadata_sha256"):
        expected_digest = (record.get("wheel") or {}).get(key)
        require(
            isinstance(expected_digest, str) and SHA256.fullmatch(expected_digest) is not None,
            f"verification record lacks a valid {key}",
        )
        require(rebuilt.get(key) == expected_digest, "rebuilt wheel content was not identical")
    # The wheel's own metadata.
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate wheel members")
        infos = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        require(len(infos) == 1, "wheel has no single METADATA")
        dist_info = infos[0].split("/", 1)[0]
        inventories = {
            "package_content_sha256": {
                n: archive.read(n) for n in names if n.startswith("famepy/")
            },
            "install_metadata_sha256": {
                n.split("/", 1)[1]: archive.read(n)
                for n in names
                if n in (f"{dist_info}/METADATA", f"{dist_info}/entry_points.txt")
            },
        }
        for key, members in inventories.items():
            digest = hashlib.sha256()
            for name in sorted(members):
                digest.update(name.encode() + b"\0" + members[name] + b"\0")
            require(
                digest.hexdigest() == record["wheel"][key],
                f"verification record {key} differs from actual wheel content",
            )
        metadata = email.parser.Parser().parsestr(archive.read(infos[0]).decode())
        require(
            metadata["Name"] == "FAMEPy" and metadata["Version"] == version,
            "wheel metadata name/version mismatch",
        )
    return {
        "wheel": {"filename": wheel_name, "sha256": actual[wheel_name]},
        "sdist": {"filename": sdist_name, "sha256": actual[sdist_name]},
    }


def check_promotion(
    *,
    run_json: Path,
    artifacts_json: Path,
    dist: Path,
    upload: Path,
    run_id: str,
    repository: str,
    workflow_path: str,
    head_sha: str,
    version: str,
    wheel_sha256: str,
    sdist_sha256: str,
    artifact_name: str,
) -> dict[str, Any]:
    require(RUN_ID.fullmatch(run_id) is not None, "run id must be a positive integer")
    require(COMMIT.fullmatch(head_sha) is not None, "commit must be 40 hex digits")
    require(VERSION.fullmatch(version) is not None, "version is not a plain PEP 440 public version")
    require(SHA256.fullmatch(wheel_sha256) is not None, "wheel digest must be 64 hex digits")
    require(SHA256.fullmatch(sdist_sha256) is not None, "sdist digest must be 64 hex digits")
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None,
        "repository must be owner/name",
    )
    check_run(
        load_json(run_json),
        run_id=int(run_id),
        repository=repository,
        workflow_path=workflow_path,
        head_sha=head_sha,
    )
    artifact = check_artifacts(
        load_json(artifacts_json),
        run_id=int(run_id),
        head_sha=head_sha,
        artifact_name=artifact_name,
    )
    files = check_distributions(
        dist,
        version=version,
        source_sha=head_sha,
        wheel_sha256=wheel_sha256,
        sdist_sha256=sdist_sha256,
    )
    require(not upload.exists(), "upload directory must not exist yet")
    upload.mkdir(parents=True)
    for entry in files.values():
        shutil.copyfile(dist / entry["filename"], upload / entry["filename"])
        require(
            sha256_file(upload / entry["filename"]) == entry["sha256"], "copy changed the bytes"
        )
    return {
        "run_id": int(run_id),
        "repository": repository,
        "head_sha": head_sha,
        "version": version,
        "artifact": artifact,
        **files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--artifacts-json", type=Path, required=True)
    parser.add_argument(
        "--dist", type=Path, required=True, help="directory the artifact was downloaded into"
    )
    parser.add_argument(
        "--upload",
        type=Path,
        required=True,
        help="new directory that receives only the distributions",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--sdist-sha256", required=True)
    parser.add_argument("--artifact-name", default="distributions")
    parser.add_argument("--summary", type=Path, help="where to write the promotion summary")
    args = parser.parse_args(argv)
    try:
        summary = check_promotion(
            run_json=args.run_json,
            artifacts_json=args.artifacts_json,
            dist=args.dist,
            upload=args.upload,
            run_id=args.run_id,
            repository=args.repository,
            workflow_path=args.workflow_path,
            head_sha=args.head_sha,
            version=args.version,
            wheel_sha256=args.wheel_sha256,
            sdist_sha256=args.sdist_sha256,
            artifact_name=args.artifact_name,
        )
    except PromotionError as error:
        print(f"promotion refused: {error}", file=sys.stderr)
        return 1
    text = json.dumps(summary, indent=2, sort_keys=True)
    if args.summary is not None:
        args.summary.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
