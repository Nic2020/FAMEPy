# SPDX-License-Identifier: MIT
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_native import make_fake  # noqa: E402

from famepy import _runtime  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_runtime():
    _runtime._reset_module_state_for_tests()
    yield
    _runtime._reset_module_state_for_tests()


@pytest.fixture
def fake():
    return make_fake(persist=True)


@pytest.fixture
def session(fake):
    owner = _runtime.Session(native=fake)
    owner.initialize()
    yield owner
    if owner.state in ("initialized", "broken"):
        owner.finalize()


@pytest.fixture
def db(session, tmp_path):
    from famepy import closedb, opendb

    database = opendb(tmp_path / "synthetic.db", "create", session=session)
    yield database
    closedb(database)
