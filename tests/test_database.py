# SPDX-License-Identifier: MIT
import contextlib
import threading

import pytest
from canonical import scalar_object
from fake_native import S_EXISTS, S_MISSING_FILE, S_READONLY

import famepy
from famepy import AccessMode, HLIError, StaleHandleError, TextEncodingError


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, AccessMode.READONLY),
        ("readonly", AccessMode.READONLY),
        ("CREATE", AccessMode.CREATE),
        (3, AccessMode.OVERWRITE),
        ("update", AccessMode.UPDATE),
        ("Shared", AccessMode.SHARED),
        (6, AccessMode.WRITE),
        ("direct_write", AccessMode.DIRECT_WRITE),
        ("direct-write", AccessMode.DIRECT_WRITE),
        (AccessMode.DIRECT_WRITE, AccessMode.DIRECT_WRITE),
    ],
)
def test_seven_access_mode_representations(value, expected):
    assert famepy._constants.access_mode(value) == expected


@pytest.mark.parametrize("value", [0, 8, "readwrite", True, 1.0, None])
def test_invalid_access_modes(value):
    with pytest.raises((ValueError, TypeError)):
        famepy._constants.access_mode(value)


def test_default_mode_is_readonly_and_requires_existing_file(session, tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(HLIError) as error:
        famepy.opendb(path, session=session)
    assert error.value.status == S_MISSING_FILE
    database = famepy.opendb(path, "create", session=session)
    assert database.mode == AccessMode.CREATE
    famepy.closedb(database)
    with famepy.opendb(path, session=session) as readonly:
        assert readonly.mode == AccessMode.READONLY
        assert not readonly.is_writable
        assert readonly.is_open
    assert not readonly.is_open


def test_create_refuses_existing_and_overwrite_replaces(session, tmp_path):
    path = tmp_path / "x.db"
    with famepy.opendb(path, "create", session=session) as database:
        famepy.do_write(scalar_object("a", "precision", 1.5), database)
        famepy.postdb(database)
    with pytest.raises(HLIError) as error:
        famepy.opendb(path, "create", session=session)
    assert error.value.status == S_EXISTS
    with famepy.opendb(path, "overwrite", session=session) as database:
        assert famepy.listdb(database) == []


def test_close_does_not_post(session, tmp_path):
    path = tmp_path / "x.db"
    with famepy.opendb(path, "create", session=session) as database:
        famepy.do_write(scalar_object("kept", "precision", 1.0), database)
        famepy.postdb(database)
        famepy.do_write(scalar_object("lost", "precision", 2.0), database)
    with famepy.opendb(path, session=session) as database:
        names = [info.name_text for info in famepy.listdb(database)]
    assert names == ["KEPT"]


def test_post_requires_writable_and_open(session, tmp_path):
    path = tmp_path / "x.db"
    famepy.closedb(famepy.opendb(path, "create", session=session))
    database = famepy.opendb(path, session=session)
    with pytest.raises(HLIError) as error:
        famepy.postdb(database)
    assert error.value.status == S_READONLY
    famepy.closedb(database)
    with pytest.raises(StaleHandleError):
        famepy.postdb(database)
    with pytest.raises(StaleHandleError):
        with database:
            pass


def test_repr_and_errors_never_echo_the_name(session, tmp_path):
    secret = tmp_path / "private-name.db"
    database = famepy.opendb(secret, "create", session=session)
    assert "private" not in repr(database)
    assert repr(database).startswith("FameDatabase(database, mode=create, open)")
    famepy.closedb(database)
    with pytest.raises(HLIError) as error:
        famepy.opendb(tmp_path / "private-missing.db", session=session)
    assert "private" not in str(error.value)


def test_remote_connection_string_is_passed_through_and_redacted(session, monkeypatch):
    seen = []
    fake = session._native.fake
    original = fake.open_database

    def spy(name, mode):
        seen.append((name, mode))
        return original(name, mode) if False else 77

    monkeypatch.setattr(fake, "open_database", spy)
    database = famepy.opendb("2552@host user secret remote.db", "shared", session=session)
    assert seen == [(b"2552@host user secret remote.db", 5)]
    assert "secret" not in repr(database)
    database._invalidate()


@pytest.mark.parametrize("name", ["", "   ", "café.db", "a\0b"])
def test_invalid_database_names(session, name):
    with pytest.raises((ValueError, TextEncodingError)):
        famepy.opendb(name, session=session)


def test_work_database_singleton(session):
    work = famepy.workdb(session=session)
    assert work.is_work and work.mode == AccessMode.UPDATE
    assert famepy.workdb(session=session) is work
    assert repr(work) == "FameDatabase(work database, mode=update, open)"
    famepy.do_write(scalar_object("w", "precision", 3.0), work)
    famepy.postdb(work)
    famepy.closedb(work)
    assert not work.is_open
    reopened = famepy.workdb(session=session)
    assert reopened is not work and reopened.is_open


def test_bytes_and_pathlike_names(session, tmp_path):
    database = famepy.opendb(bytes(tmp_path / "b.db"), "create", session=session)
    assert isinstance(database, famepy.FameDatabase)
    famepy.closedb(database)
    famepy.closedb(database)


def test_failed_close_keeps_the_handle_tracked_for_retry(session, tmp_path):
    database = famepy.opendb(tmp_path / "c.db", "create", session=session)
    fake = session._native.fake
    fake.fail_next["cfmcldb"] = 903
    with pytest.raises(HLIError):
        famepy.closedb(database)
    assert database.is_open
    assert database in session.open_databases and database.key in fake.handles
    famepy.closedb(database)
    assert not database.is_open
    assert database not in session.open_databases and database.key not in fake.handles
    famepy.closedb(database)


def test_finalize_still_closes_a_handle_whose_close_failed(session, tmp_path):
    database = famepy.opendb(tmp_path / "c.db", "create", session=session)
    fake = session._native.fake
    fake.fail_next["cfmcldb"] = 903
    with pytest.raises(HLIError):
        famepy.closedb(database)
    session.finalize()
    assert session.last_cleanup_statuses == ()
    assert not database.is_open and fake.handles == {}


def test_stale_handle_is_rejected_inside_the_lock(session, tmp_path, monkeypatch):
    """A close and key reuse between validation and the native call is caught."""
    fake = session._native.fake
    old = famepy.opendb(tmp_path / "old.db", "create", session=session)
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = famepy.FameDatabase.operation

    @contextlib.contextmanager
    def gated(self, action):
        if threading.current_thread().name == "writer" and action == "write object":
            entered.set()
            assert release.wait(5)
        with original(self, action) as native:
            yield native

    monkeypatch.setattr(famepy.FameDatabase, "operation", gated)

    def write():
        try:
            famepy.do_write(scalar_object("wrong", "precision", 123.0), old)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    writer = threading.Thread(target=write, name="writer")
    writer.start()
    assert entered.wait(5)
    old_key = old.key
    famepy.closedb(old)
    fake.next_key = old_key
    other = famepy.opendb(tmp_path / "other.db", "create", session=session)
    assert other.key == old_key
    fake.calls.clear()
    release.set()
    writer.join(5)
    assert not writer.is_alive()
    assert [type(error) for error in errors] == [StaleHandleError]
    assert fake.calls == []
    assert "WRONG" not in fake.handles[other.key].objects
    famepy.closedb(other)


def test_close_waits_for_an_operation_in_progress(session, tmp_path, monkeypatch):
    fake = session._native.fake
    database = famepy.opendb(tmp_path / "a.db", "create", session=session)
    inside, release = threading.Event(), threading.Event()
    original = fake.new_object

    def slow_new_object(*args):
        inside.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(fake, "new_object", slow_new_object)
    writer = threading.Thread(
        target=lambda: famepy.do_write(scalar_object("x", "precision", 1.0), database)
    )
    writer.start()
    assert inside.wait(5)
    closer = threading.Thread(target=lambda: famepy.closedb(database))
    closer.start()
    closer.join(0.5)
    assert closer.is_alive() and database.is_open
    release.set()
    writer.join(5)
    closer.join(5)
    assert not database.is_open
    calls = fake.calls
    assert calls.index("fame_write_precisions") < calls.index("cfmcldb")
