# Contributing

Treat every tracked file, commit, workflow log, issue and distribution as public.
Keep internal project labels, private plans and links, personal contact details,
staff names and institutional discussions out of these surfaces. Retain legally
required copyright notices and factual upstream attribution. Use the project
contact `statespaceecon@gmail.com` where a contact address is needed.

Use synthetic test data. Do not add production databases, connection strings,
credentials, vendor headers, help files, licensed libraries or raw diagnostic
logs. Review generated files as carefully as source. Ignore rules and automated
checks assist review; they cannot prove that an arbitrary file is safe to publish.

## Development

Python 3.11 or newer is required. The foundation is tested without FAME:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pre-commit run --all-files
uv build
```

Unit tests use the in-memory fake backend in `tests/fake_native.py`; it models
the package's own contracts and is not FAME behavior evidence. The validation
runner's self-test drives that fake through real subprocesses.

The independent C shim needs a compiler (MSVC on Windows, GCC/Clang on Linux).
Run from a compiler-enabled terminal:

```sh
uv run python scripts/build_test_shim.py
```

Set `FAMEPY_TEST_SHIM` to the **absolute** path printed by the builder, then run
pytest again. CI requires these tests; an ordinary developer run without the
variable reports them as skipped. The shim contains no vendor code and does not
certify FAME compatibility. See [native validation](docs/native-validation.md).

Install hooks with `uv run pre-commit install` if desired. Before publication,
review Git history and built wheel/sdist contents as well as the current tree.
