# Releasing

A release is one reviewed pair of distributions (an sdist and a
`py3-none-any` wheel) produced by a single *rehearsal* run of the `release`
workflow, stored as that run's artifact together with a verification
record, and later *promoted* unchanged by the separate `publish` workflow.
The rehearsal never uploads to an index; the promotion never builds. Every
upload is an explicit operator action naming the run and the exact file
digests it approves. Nothing is published on a push or on a tag by itself.

Installable artifacts and index publication are different things: a
rehearsal run's artifact can be downloaded and installed from the run page
by anyone with access to the repository, without any index; publication
to PyPI is the separate promotion described below.

## Before a candidate

1. The evidence in [capability status](capabilities.md) and the
   [parity ledger](parity.md) names the revision it was measured on. A
   candidate that changes shared runtime, bridge or raw code since that
   revision needs the campaign described in
   [native validation](native-validation.md) on each host before it is
   promoted; a candidate that changes only metadata, documentation or the
   workflows does not.
2. `CHANGELOG.md` has an entry for the version, and the version in
   `pyproject.toml`, `src/famepy/__init__.py` and `uv.lock` agree
   (`uv lock` after editing `pyproject.toml`, then `uv lock --check`).
3. The `checks` workflow is green on the exact commit.
4. Locally, from a clean checkout of that commit:

   ```sh
   uv build --out-dir dist
   uv run --with twine twine check --strict dist/*
   FAMEPY_TEST_SHIM=<absolute-shim-path> uv run python scripts/verify_artifacts.py \
       --wheel dist/famepy-<version>-py3-none-any.whl --sdist dist/famepy-<version>.tar.gz \
       --source-sha <commit> --record dist/verification.json --scratch build/release-verify
   ```

   Release mode of `verify_artifacts.py` verifies exactly the supplied
   files: filename, metadata, `RECORD`, license files and `py.typed`; every
   package member byte-identical to the source tree with nothing extra;
   the installed-wheel test suite with the independent C shim and the
   DataEcon extension required (a skipped requirement fails); a wheel
   rebuilt from the supplied sdist whose package content must equal the
   supplied wheel's, tested installed as well. The record it writes names
   the files, their SHA-256, the version and the commit; it is written
   only when everything passed. Without arguments the script keeps its
   ordinary CI behavior (build into a scratch directory and verify that).
5. The wheel and sdist contain no vendor file, native library, database,
   log or path; the script refuses such members.

Two builds of the same commit on different machines or toolchains are not
required to produce byte-identical archives, and nothing here relies on
that. What is promoted is the pair of files that one rehearsal run built,
tested and recorded, identified by their digests; the local build in
step 4 establishes that the commit builds and verifies, not that its bytes
equal the run's.

## Rehearsal: build, verify and store the artifact

Dispatch the `release` workflow from the reviewed commit (a branch or the
tag; dispatching from the tag `v<version>` is what the promotion later
requires). The single job runs the lint, type, hook and test checks with
both extensions required, builds the two distributions with `uv build`,
runs `twine check --strict`, verifies exactly those files with
`verify_artifacts.py` in release mode, writes `SHA256SUMS`, prints the
verification record and the hashes, and uploads the four files (wheel,
sdist, `verification.json`, `SHA256SUMS`) as the run artifact named
`distributions`, kept for 30 days. The job has no publishing permission
and no index credential.

Review the run: download the artifact from the run page, check
`SHA256SUMS` against the files (`sha256sum -c SHA256SUMS`), read
`verification.json`, and install the wheel in a fresh environment:

```sh
python -m pip install --only-binary=:all: famepy-<version>-py3-none-any.whl
python -m famepy            # exits 1 with library_unavailable without FAME
```

The dependencies must resolve as wheels without a compiler. Keep the run
id and the two digests from `SHA256SUMS`: they are the inputs of the
promotion. A rehearsal that fails or that produced something you do not
want to publish is simply not promoted; fix the cause on a new commit and
rehearse again.

## Promotion: publish exactly the reviewed files

1. Tag the rehearsed commit `v<version>` and push the tag (if the
   rehearsal was dispatched from a branch, the tag must point at the same
   commit the run built; the promotion checks that).
2. Dispatch the `publish` workflow **from that tag** with three inputs: the
   rehearsal `run_id` and the `wheel_sha256` and `sdist_sha256` copied
   from the reviewed `SHA256SUMS`. The inputs are validated as a positive
   integer and two 64-digit lowercase hex strings before anything else,
   and are passed to the checks as arguments, never interpolated into a
   script.
3. The `promote` job (the only job with `id-token: write`) fetches the run
   and artifact metadata from the GitHub API, downloads that run's
   `distributions` artifact, and runs `scripts/check_promotion.py`, which
   refuses unless: the run is in this repository, executed
   `.github/workflows/release.yml`, completed successfully, was an operator
   dispatch and built the tag's commit; exactly one unexpired artifact of
   that name exists for that run and commit; the download holds exactly the
   four files for the tag's version; `SHA256SUMS`, the operator's digests
   and the verification record (result passed, both extensions required,
   same commit, same version, same filenames and digests, identical rebuilt
   content) all agree with the bytes; and the wheel's own metadata carries
   the version. Only then are the wheel and the sdist copied to an upload
   directory and uploaded with `pypa/gh-action-pypi-publish` through
   trusted publishing, with PEP 740 attestations. Nothing is built or
   tested in this job.
4. After the upload, install from the index in a fresh environment and
   repeat the check of the rehearsal review; then create the GitHub
   release from the tag with the changelog entry and the two digests.

### If an upload fails part-way

Twine uploads the files one at a time, so a failure is not a rollback: the
wheel may be on the index while the sdist is not, or the reverse. Do not
re-dispatch blindly. Look at the project's file list on PyPI
(`https://pypi.org/project/famepy/<version>/#files`) and compare each
listed file's digest with the approved one. A filename that reached the
index cannot be replaced or re-uploaded with different bytes; if the
approved file is the one listed, only the missing file needs to be
uploaded. This workflow deliberately does not automate partial-upload recovery.
Stop and prepare an explicitly reviewed upload of only the missing, hash-verified
file, or choose a new version. Do not edit or move the released tag to change its
workflow, rebuild different bytes under the same filename, or enable blind
skip-existing behavior. A file that
reached the index with unapproved bytes means the approved artifact was
not what was uploaded: yank the release and investigate before anything
else. Either way the approved artifact stays in the rehearsal run for
comparison. Pre-upload rejections (the promotion checks, an invalid
publisher configuration) leave the index unchanged.

## Manual approval path and one-time setup

The approval gate is the promotion dispatch itself: a person with write
access to the repository chooses to run `publish` from the tag and types
the run id and the digests of the files reviewed in the rehearsal. The
run cannot publish anything else. Where the repository's plan and
visibility support it, GitHub environment protections strengthen that
gate; they do not replace it, and the workflow's reference to the `pypi`
environment does not by itself create any protection (a workflow that
names a missing environment creates it without rules). Per the current
GitHub documentation, required reviewers, wait timers and custom
protection rules are available for private repositories only on
GitHub Enterprise; deployment tag restrictions and environment secrets
are available for private repositories on Pro and Team plans and for all
public repositories.

One-time setup, all operator decisions (nothing here is done by the
workflows):

| Step | Where | What |
|---|---|---|
| Repository visibility and plan | GitHub | decide whether the repository is public when releasing; this determines which environment protections apply (above) |
| Environment `pypi` | repository settings, Environments | create it; where available, restrict deployment branches and tags to `v*` and add required reviewers |
| Pending trusted publisher | `https://pypi.org/manage/account/publishing/` | project name `FAMEPy`, owner `Nic2020`, repository `FAMEPy`, workflow `publish.yml`, environment `pypi` |
| Name check | `https://pypi.org/project/famepy/` | confirm no project exists immediately before the first promotion; a pending publisher does not reserve the name, and the project is created only by the first successful upload |

Name normalization on the index follows the packaging specification:
letters are case-folded and runs of `-`, `_` and `.` become one `-`, so
`FAMEPy`, `famepy` and `FAMEPY` are the same project, while `Fame-Py`
normalizes to `fame-py`, a different project. No TestPyPI upload is part
of this procedure; the rehearsal artifact is the test installation.

## Version scheme

Versions follow PEP 440: `0.1.0rc1`, `0.1.0rc2`, ... for candidates, then
`0.1.0`. A release candidate is installed by `pip` only when requested by
version or with `--pre`. Development versions (`.dev`) are never promoted.
