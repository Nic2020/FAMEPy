"""Guards of the release artifact verification and promotion scripts.

The scripts are exercised on small synthetic distributions that mirror the
real layout (a wheel with RECORD, METADATA, WHEEL and licenses; an sdist with
PKG-INFO, pyproject and the package sources) against a synthetic source tree,
so tampering, substitution and provenance failures are decided without
building or installing anything. The installed-test step is replaced by a
recording stub.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = load("verify_artifacts")
promotion = load("check_promotion")

VERSION = "9.9.9rc1"
SOURCES = {
    "famepy/py.typed": b"",
    "famepy/__init__.py": f'"""pkg"""\n__version__ = "{VERSION}"\n'.encode(),
    "famepy/_core.py": b"X = 1\n",
    "famepy/sub/__init__.py": b"",
}


def project_files(version: str, pkg_version: str | None = None) -> dict[str, bytes]:
    return {
        "PKG-INFO": (
            f"Metadata-Version: 2.4\nName: FAMEPy\nVersion: {pkg_version or version}\n"
        ).encode(),
        "pyproject.toml": f'[project]\nname = "FAMEPy"\nversion = "{version}"\n'.encode(),
        "LICENSE": b"MIT\n",
        "licenses/FAME.jl.txt": b"BSD\n",
        "README.md": b"# x\n",
        "CHANGELOG.md": b"# c\n",
        "uv.lock": b"",
    }


def make_tree(root: Path, sources: dict[str, bytes] = SOURCES) -> None:
    for name, data in sources.items():
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name, data in project_files(VERSION).items():
        if name != "PKG-INFO":
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def make_wheel(
    path: Path,
    *,
    version: str = VERSION,
    sources: dict[str, bytes] = SOURCES,
    extra: dict[str, bytes] | None = None,
    drop: str | None = None,
    bad_record: bool = False,
    metadata_version: str | None = None,
    license_expression: str = "MIT AND BSD-3-Clause",
) -> Path:
    dist_info = f"famepy-{version}.dist-info"
    members = dict(sources)
    members["famepy/py.typed"] = b""
    if extra:
        members.update(extra)
    if drop:
        members.pop(drop)
    members[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.4\nName: FAMEPy\n"
        f"Version: {metadata_version or version}\n"
        f"License-Expression: {license_expression}\n\nreadme\n"
    ).encode()
    members[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    members[f"{dist_info}/licenses/LICENSE"] = b"MIT\n"
    members[f"{dist_info}/licenses/licenses/FAME.jl.txt"] = b"BSD\n"
    lines = [_record_line(n, d) for n, d in members.items()]
    if bad_record:
        lines[0] = lines[0].replace(",sha256=", ",sha256=A")
    lines.append(f"{dist_info}/RECORD,,")
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr(f"{dist_info}/RECORD", "\n".join(lines) + "\n")
    return path


def make_sdist(
    path: Path,
    *,
    version: str = VERSION,
    sources: dict[str, bytes] = SOURCES,
    extra: dict[str, bytes] | None = None,
    omit: tuple[str, ...] = (),
    pkg_version: str | None = None,
) -> Path:
    prefix = f"famepy-{version}/"
    files = project_files(version, pkg_version)
    files.update({"src/" + n: d for n, d in sources.items()})
    if extra:
        files.update(extra)
    for name in omit:
        files.pop(name)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


class Stub:
    """Records the wheels it was asked to test instead of installing them."""

    def __init__(self) -> None:
        self.tested: list[str] = []

    def __call__(self, wheel: Path, scratch: Path) -> None:
        self.tested.append(wheel.name)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    make_tree(root)
    return root


@pytest.fixture
def good(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = make_wheel(dist / f"famepy-{VERSION}-py3-none-any.whl")
    sdist = make_sdist(dist / f"famepy-{VERSION}.tar.gz")

    # The rebuild step is replaced by copying the supplied wheel, so the
    # content comparison is exercised without a build backend.
    def rebuild(sdist_path: Path, scratch: Path) -> Path:
        verify.extract_sdist(sdist_path, scratch / "source")
        rebuilt = scratch / "rebuilt"
        rebuilt.mkdir()
        target = rebuilt / wheel.name
        target.write_bytes(wheel.read_bytes())
        return target

    monkeypatch.setattr(verify, "rebuild_from_sdist", rebuild)
    return {"wheel": wheel, "sdist": sdist, "scratch": tmp_path / "scratch"}


def run_verify(good: dict[str, Path], tree: Path, **overrides: object) -> dict[str, object]:
    stub = Stub()
    kwargs: dict[str, object] = {
        "source_sha": "a" * 40,
        "expect_version": VERSION,
        "tester": stub,
    }
    kwargs.update(overrides)
    record = verify.verify_release(good["wheel"], good["sdist"], tree, good["scratch"], **kwargs)
    record["_tested"] = stub.tested
    return record


def test_good_release_inputs_pass_and_record_identities(good, tree):  # type: ignore[no-untyped-def]
    record = run_verify(good, tree)
    assert record["result"] == "passed" and record["version"] == VERSION
    assert record["source_sha"] == "a" * 40
    assert record["wheel"]["sha256"] == hashlib.sha256(good["wheel"].read_bytes()).hexdigest()
    assert record["sdist"]["sha256"] == hashlib.sha256(good["sdist"].read_bytes()).hexdigest()
    assert record["wheel"]["filename"] == good["wheel"].name
    assert (
        record["rebuilt_wheel"]["package_content_sha256"]
        == record["wheel"]["package_content_sha256"]
    )
    assert record["required_extensions"] == ["shim", "dataecon"]
    # Both the supplied wheel and the rebuilt wheel were tested, in that order.
    assert record["_tested"] == [good["wheel"].name, good["wheel"].name]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda g, t: make_wheel(g["wheel"], sources={**SOURCES, "famepy/_core.py": b"X = 2\n"}),
            "differ from the source tree",
        ),
        (
            lambda g, t: make_wheel(g["wheel"], extra={"famepy/_extra.py": b""}),
            "not in the source tree",
        ),
        (lambda g, t: make_wheel(g["wheel"], drop="famepy/_core.py"), "lacks source members"),
        (
            lambda g, t: make_wheel(g["wheel"], extra={"famepy/native.so": b"\0"}),
            "forbidden member",
        ),
        (lambda g, t: make_wheel(g["wheel"], bad_record=True), "RECORD digest mismatch"),
        (lambda g, t: make_wheel(g["wheel"], metadata_version="9.9.9rc2"), "METADATA version"),
        (lambda g, t: make_wheel(g["wheel"], license_expression="MIT"), "license expression"),
        (
            lambda g, t: make_sdist(g["sdist"], sources={**SOURCES, "famepy/_core.py": b"X = 3\n"}),
            "sdist sources differ",
        ),
        (
            lambda g, t: make_sdist(g["sdist"], extra={"src/famepy/_smuggled.py": b""}),
            "absent from source tree",
        ),
        (lambda g, t: make_sdist(g["sdist"], omit=("CHANGELOG.md",)), "CHANGELOG.md missing"),
        (lambda g, t: make_sdist(g["sdist"], pkg_version="9.9.9"), "PKG-INFO"),
        (lambda g, t: make_sdist(g["sdist"], extra={"build/x.log": b""}), "forbidden sdist member"),
        (lambda g, t: (t / "src" / "famepy" / "_new.py").write_bytes(b""), "lacks source members"),
        (
            lambda g, t: (t / "src" / "famepy" / "_core.py").write_bytes(b"X = 9\n"),
            "differ from the source tree",
        ),
    ],
)
def test_tampered_or_mismatched_inputs_are_refused(good, tree, mutate, message):  # type: ignore[no-untyped-def]
    mutate(good, tree)
    with pytest.raises(verify.VerificationError, match=message):
        run_verify(good, tree)


def test_version_and_revision_arguments_are_checked(good, tree, tmp_path):  # type: ignore[no-untyped-def]
    with pytest.raises(verify.VerificationError, match="not the expected"):
        run_verify(good, tree, expect_version="9.9.9")
    with pytest.raises(verify.VerificationError, match="40 hex"):
        run_verify(good, tree, source_sha="abc")
    other = make_sdist(tmp_path / "famepy-9.9.9.tar.gz", version="9.9.9")
    with pytest.raises(verify.VerificationError, match="not the expected"):
        verify.verify_release(
            good["wheel"],
            other,
            tree,
            tmp_path / "s2",
            source_sha=None,
            expect_version=VERSION,
            tester=Stub(),
        )
    renamed = tmp_path / "famepy-9.9.9-py3-none-any.whl"
    renamed.write_bytes(good["wheel"].read_bytes())
    with pytest.raises(verify.VerificationError, match="dist-info"):
        verify.verify_release(
            renamed,
            good["sdist"],
            tree,
            tmp_path / "s3",
            source_sha=None,
            expect_version=None,
            tester=Stub(),
        )


def test_missing_inputs_and_wrong_names_are_refused(good, tree, tmp_path):  # type: ignore[no-untyped-def]
    with pytest.raises(verify.VerificationError, match="wheel not found"):
        verify.verify_release(
            tmp_path / "none.whl",
            good["sdist"],
            tree,
            tmp_path / "s",
            source_sha=None,
            expect_version=None,
            tester=Stub(),
        )
    other = tmp_path / "other-1.0-py3-none-any.whl"
    other.write_bytes(good["wheel"].read_bytes())
    with pytest.raises(verify.VerificationError, match="unexpected wheel filename"):
        verify.verify_release(
            other,
            good["sdist"],
            tree,
            tmp_path / "s",
            source_sha=None,
            expect_version=None,
            tester=Stub(),
        )


def test_rebuilt_wheel_with_other_content_is_refused(good, tree, monkeypatch):  # type: ignore[no-untyped-def]
    def rebuild(sdist_path: Path, scratch: Path) -> Path:
        rebuilt = scratch / "rebuilt"
        rebuilt.mkdir(parents=True)
        return make_wheel(
            rebuilt / good["wheel"].name, sources={**SOURCES, "famepy/_core.py": b"X = 2\n"}
        )

    monkeypatch.setattr(verify, "rebuild_from_sdist", rebuild)
    with pytest.raises(verify.VerificationError, match="differ from the source tree"):
        run_verify(good, tree)


def test_cli_refuses_bad_option_combinations(tmp_path, capsys):  # type: ignore[no-untyped-def]
    assert verify.main(["--wheel", str(tmp_path / "x.whl")]) == 1
    assert "both --wheel and --sdist" in capsys.readouterr().err
    assert verify.main(["--record", str(tmp_path / "r.json")]) == 1
    assert "need --wheel and --sdist" in capsys.readouterr().err
    assert (
        verify.main(
            [
                "--wheel",
                str(tmp_path / "x.whl"),
                "--sdist",
                str(tmp_path / "y.tar.gz"),
                "--scratch",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert "must not exist" in capsys.readouterr().err


# ---------------------------------------------------------------- promotion

RUN_ID = "123456789"
HEAD = "b" * 40
REPO = "owner/FAMEPy"
WORKFLOW = ".github/workflows/release.yml"


def run_payload(**overrides: object) -> dict[str, object]:
    run: dict[str, object] = {
        "id": int(RUN_ID),
        "repository": {"full_name": REPO},
        "path": WORKFLOW,
        "status": "completed",
        "conclusion": "success",
        "head_sha": HEAD,
        "event": "workflow_dispatch",
    }
    run.update(overrides)
    return run


def artifacts_payload(**overrides: object) -> dict[str, object]:
    artifact: dict[str, object] = {
        "id": 77,
        "name": "distributions",
        "expired": False,
        "size_in_bytes": 10,
        "digest": "sha256:" + "c" * 64,
        "workflow_run": {"id": int(RUN_ID), "head_sha": HEAD},
    }
    artifact.update(overrides)
    return {"total_count": 1, "artifacts": [artifact]}


@pytest.fixture
def promo(tmp_path: Path) -> dict[str, object]:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = make_wheel(dist / f"famepy-{VERSION}-py3-none-any.whl")
    sdist = make_sdist(dist / f"famepy-{VERSION}.tar.gz")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    sdist_sha = hashlib.sha256(sdist.read_bytes()).hexdigest()
    inspection = verify.inspect_wheel(wheel, SOURCES, expect_version=VERSION)
    content = inspection["package_content_sha256"]
    install = inspection["install_metadata_sha256"]
    record = {
        "schema_version": 1,
        "result": "passed",
        "distribution": "FAMEPy",
        "version": VERSION,
        "source_sha": HEAD,
        "wheel": {
            "filename": wheel.name,
            "sha256": wheel_sha,
            "version": VERSION,
            "package_content_sha256": content,
            "install_metadata_sha256": install,
        },
        "sdist": {"filename": sdist.name, "sha256": sdist_sha, "version": VERSION},
        "rebuilt_wheel": {
            "sha256": "0" * 64,
            "package_content_sha256": content,
            "install_metadata_sha256": install,
        },
        "tests": {"installed_wheel": "passed", "sdist_rebuilt_wheel": "passed"},
        "required_extensions": ["shim", "dataecon"],
    }
    (dist / "verification.json").write_text(json.dumps(record), encoding="utf-8")
    (dist / "SHA256SUMS").write_text(
        f"{wheel_sha}  {wheel.name}\n{sdist_sha}  {sdist.name}\n", encoding="utf-8"
    )
    (tmp_path / "run.json").write_text(json.dumps(run_payload()), encoding="utf-8")
    (tmp_path / "artifacts.json").write_text(json.dumps(artifacts_payload()), encoding="utf-8")
    return {
        "dist": dist,
        "tmp": tmp_path,
        "wheel_sha": wheel_sha,
        "sdist_sha": sdist_sha,
        "record": record,
    }


def run_promotion(promo: dict[str, object], **overrides: object) -> dict[str, object]:
    tmp = promo["tmp"]
    assert isinstance(tmp, Path)
    kwargs: dict[str, object] = {
        "run_json": tmp / "run.json",
        "artifacts_json": tmp / "artifacts.json",
        "dist": promo["dist"],
        "upload": tmp / "upload",
        "run_id": RUN_ID,
        "repository": REPO,
        "workflow_path": WORKFLOW,
        "head_sha": HEAD,
        "version": VERSION,
        "wheel_sha256": promo["wheel_sha"],
        "sdist_sha256": promo["sdist_sha"],
        "artifact_name": "distributions",
    }
    kwargs.update(overrides)
    return promotion.check_promotion(**kwargs)


def test_good_promotion_copies_only_the_distributions(promo):  # type: ignore[no-untyped-def]
    summary = run_promotion(promo)
    upload = promo["tmp"] / "upload"
    assert sorted(p.name for p in upload.iterdir()) == [
        f"famepy-{VERSION}-py3-none-any.whl",
        f"famepy-{VERSION}.tar.gz",
    ]
    assert summary["wheel"]["sha256"] == promo["wheel_sha"] and summary["artifact"]["id"] == 77
    assert summary["run_id"] == int(RUN_ID) and summary["head_sha"] == HEAD


def rewrite(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "prepare, message",
    [
        (
            lambda p: rewrite(p["tmp"] / "run.json", run_payload(conclusion="failure")),
            "did not succeed",
        ),
        (
            lambda p: rewrite(
                p["tmp"] / "run.json", run_payload(status="in_progress", conclusion=None)
            ),
            "not completed",
        ),
        (lambda p: rewrite(p["tmp"] / "run.json", run_payload(id=5)), "run id differs"),
        (
            lambda p: rewrite(p["tmp"] / "run.json", run_payload(head_sha="c" * 40)),
            "released commit",
        ),
        (
            lambda p: rewrite(
                p["tmp"] / "run.json", run_payload(path=".github/workflows/checks.yml")
            ),
            "approved workflow",
        ),
        (
            lambda p: rewrite(p["tmp"] / "run.json", run_payload(repository={"full_name": "x/y"})),
            "another repository",
        ),
        (lambda p: rewrite(p["tmp"] / "run.json", run_payload(event="push")), "operator dispatch"),
        (
            lambda p: rewrite(p["tmp"] / "artifacts.json", artifacts_payload(expired=True)),
            "expired",
        ),
        (
            lambda p: rewrite(p["tmp"] / "artifacts.json", artifacts_payload(name="other")),
            "exactly one artifact",
        ),
        (
            lambda p: rewrite(
                p["tmp"] / "artifacts.json",
                artifacts_payload(workflow_run={"id": 1, "head_sha": HEAD}),
            ),
            "another run",
        ),
        (lambda p: rewrite(p["tmp"] / "artifacts.json", {"artifacts": []}), "exactly one artifact"),
        (lambda p: (p["dist"] / "extra.txt").write_text("x"), "not the expected four"),
        (lambda p: (p["dist"] / "SHA256SUMS").unlink(), "not the expected four"),
        (
            lambda p: (p["dist"] / f"famepy-{VERSION}.tar.gz").write_bytes(b"tampered"),
            "SHA256SUMS does not match",
        ),
        (
            lambda p: (p["dist"] / "verification.json").write_text(
                json.dumps({**p["record"], "result": "failed"})
            ),
            "does not say passed",
        ),
        (
            lambda p: (p["dist"] / "verification.json").write_text(
                json.dumps({**p["record"], "source_sha": "d" * 40})
            ),
            "another commit",
        ),
        (
            lambda p: (p["dist"] / "verification.json").write_text(
                json.dumps({**p["record"], "version": "9.9.9"})
            ),
            "record version",
        ),
        (
            lambda p: (p["dist"] / "verification.json").write_text(
                json.dumps({**p["record"], "required_extensions": ["shim"]})
            ),
            "both extensions",
        ),
        (
            lambda p: (p["dist"] / "verification.json").write_text(
                json.dumps({**p["record"], "wheel": {**p["record"]["wheel"], "sha256": "e" * 64}})
            ),
            "digest differs from the bytes",
        ),
        (lambda p: (p["dist"] / "verification.json").write_text("{not json"), "cannot read"),
    ],
)
def test_wrong_run_artifact_or_record_is_refused(promo, prepare, message):  # type: ignore[no-untyped-def]
    prepare(promo)
    with pytest.raises(promotion.PromotionError, match=message):
        run_promotion(promo)
    assert not (promo["tmp"] / "upload").exists()


def test_operator_inputs_are_validated_and_compared(promo):  # type: ignore[no-untyped-def]
    with pytest.raises(promotion.PromotionError, match="positive integer"):
        run_promotion(promo, run_id="12; rm -rf /")
    with pytest.raises(promotion.PromotionError, match="64 hex"):
        run_promotion(promo, wheel_sha256="abc")
    with pytest.raises(promotion.PromotionError, match="approved digest"):
        run_promotion(promo, wheel_sha256="f" * 64)
    with pytest.raises(promotion.PromotionError, match="approved digest"):
        run_promotion(promo, sdist_sha256="f" * 64)
    with pytest.raises(promotion.PromotionError, match="40 hex"):
        run_promotion(promo, head_sha="zz")
    with pytest.raises(promotion.PromotionError, match="not the expected four"):
        run_promotion(promo, version="9.9.9")
    with pytest.raises(promotion.PromotionError, match="PEP 440"):
        run_promotion(promo, version="v9.9.9")


def test_promotion_cli_reports_refusal(promo, capsys):  # type: ignore[no-untyped-def]
    tmp = promo["tmp"]
    args = [
        "--run-json",
        str(tmp / "run.json"),
        "--artifacts-json",
        str(tmp / "artifacts.json"),
        "--dist",
        str(promo["dist"]),
        "--upload",
        str(tmp / "up"),
        "--run-id",
        RUN_ID,
        "--repository",
        REPO,
        "--workflow-path",
        WORKFLOW,
        "--head-sha",
        HEAD,
        "--version",
        VERSION,
        "--wheel-sha256",
        promo["wheel_sha"],
        "--sdist-sha256",
        "0" * 64,
        "--summary",
        str(tmp / "summary.json"),
    ]
    assert promotion.main(args) == 1
    assert "sdist digest differs" in capsys.readouterr().err
    assert not (tmp / "summary.json").exists() and not (tmp / "up").exists()
    args[args.index("0" * 64)] = promo["sdist_sha"]
    assert promotion.main(args) == 0
    assert json.loads((tmp / "summary.json").read_text())["version"] == VERSION


@pytest.mark.parametrize("key", ["package_content_sha256", "install_metadata_sha256"])
@pytest.mark.parametrize("bad", [None, "invalid", "0" * 64])
def test_missing_or_forged_content_evidence_refuses_promotion(promo, key, bad):
    record = promo["record"]
    for target in ("wheel", "rebuilt_wheel"):
        if bad is None:
            record[target].pop(key)
        else:
            record[target][key] = bad
    rewrite(promo["dist"] / "verification.json", record)
    with pytest.raises(promotion.PromotionError):
        run_promotion(promo)
    assert not (promo["tmp"] / "upload").exists()


@pytest.mark.parametrize(
    "member", ["famepy/py.typed", f"famepy-{VERSION}.dist-info/unexpected.txt"]
)
def test_unexpected_wheel_content_is_not_self_attesting(good, tree, member):
    make_wheel(good["wheel"], extra={member: b"unexpected content"})
    with pytest.raises(verify.VerificationError):
        run_verify(good, tree)


def test_sdist_extra_file_must_exist_in_reviewed_source(good, tree):
    make_sdist(good["sdist"], extra={"private-notes.txt": b"unexpected content"})
    with pytest.raises(verify.VerificationError, match="absent from source tree"):
        run_verify(good, tree)


def test_rebuilt_installation_metadata_must_match(good, tree, monkeypatch):
    def rebuild(sdist_path, scratch):
        folder = scratch / "rebuilt"
        folder.mkdir()
        target = make_wheel(folder / good["wheel"].name)
        metadata = f"famepy-{VERSION}.dist-info/METADATA"
        with zipfile.ZipFile(target) as z:
            original = z.read(metadata)
        # make_wheel's metadata override is appended below after generation.
        with zipfile.ZipFile(target) as z:
            members = {n: z.read(n) for n in z.namelist() if not n.endswith("/RECORD")}
        members[metadata] = original.replace(
            b"\n\nreadme", b"\nRequires-Dist: unexpected-dependency\n\nreadme"
        )
        record_name = f"famepy-{VERSION}.dist-info/RECORD"
        rows = [_record_line(n, d) for n, d in members.items()] + [record_name + ",,"]
        with zipfile.ZipFile(target, "w") as z:
            for n, d in members.items():
                z.writestr(n, d)
            z.writestr(record_name, "\n".join(rows) + "\n")
        return target

    monkeypatch.setattr(verify, "rebuild_from_sdist", rebuild)
    with pytest.raises(verify.VerificationError, match="installation metadata"):
        run_verify(good, tree)
