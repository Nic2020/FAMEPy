# SPDX-License-Identifier: MIT
import contextlib
import threading

import pytest
from fake_native import S_EXISTS, S_MISSING_FILE, S_READONLY

import famepy
from famepy import AccessMode, Database, FameError, StaleHandleError, TextEncodingError


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
    with pytest.raises(FameError) as error:
        famepy.open_database(path, session=session)
    assert error.value.status == S_MISSING_FILE
    database = famepy.open_database(path, "create", session=session)
    assert database.mode == AccessMode.CREATE
    database.close()
    with famepy.open_database(path, session=session) as readonly:
        assert readonly.mode == AccessMode.READONLY
        assert not readonly.is_writable
        assert readonly.is_open
    assert not readonly.is_open


def test_create_refuses_existing_and_overwrite_replaces(session, tmp_path):
    path = tmp_path / "x.db"
    with famepy.open_database(path, "create", session=session) as database:
        famepy.write_object(database, "a", famepy.scalar("precision", 1.5))
        database.post()
    with pytest.raises(FameError) as error:
        famepy.open_database(path, "create", session=session)
    assert error.value.status == S_EXISTS
    with famepy.open_database(path, "overwrite", session=session) as database:
        assert famepy.list_objects(database) == []


def test_close_does_not_post(session, tmp_path):
    path = tmp_path / "x.db"
    with famepy.open_database(path, "create", session=session) as database:
        famepy.write_object(database, "kept", famepy.scalar("precision", 1.0))
        database.post()
        famepy.write_object(database, "lost", famepy.scalar("precision", 2.0))
    with famepy.open_database(path, session=session) as database:
        names = [info.name_text for info in famepy.list_objects(database)]
    assert names == ["KEPT"]


def test_post_requires_writable_and_open(session, tmp_path):
    path = tmp_path / "x.db"
    famepy.open_database(path, "create", session=session).close()
    database = famepy.open_database(path, session=session)
    with pytest.raises(FameError) as error:
        database.post()
    assert error.value.status == S_READONLY
    database.close()
    with pytest.raises(StaleHandleError):
        database.post()
    with pytest.raises(StaleHandleError):
        with database:
            pass


def test_repr_and_errors_never_echo_the_name(session, tmp_path):
    secret = tmp_path / "private-name.db"
    database = famepy.open_database(secret, "create", session=session)
    assert "private" not in repr(database)
    assert repr(database).startswith("Database(database, mode=create, open)")
    database.close()
    with pytest.raises(FameError) as error:
        famepy.open_database(tmp_path / "private-missing.db", session=session)
    assert "private" not in str(error.value)


def test_remote_connection_string_is_passed_through_and_redacted(session, monkeypatch):
    seen = []
    fake = session._native.fake
    original = fake.open_database

    def spy(name, mode):
        seen.append((name, mode))
        return original(name, mode) if False else 77

    monkeypatch.setattr(fake, "open_database", spy)
    database = famepy.open_database("2552@host user secret remote.db", "shared", session=session)
    assert seen == [(b"2552@host user secret remote.db", 5)]
    assert "secret" not in repr(database)
    database._invalidate()


@pytest.mark.parametrize("name", ["", "   ", "café.db", "a\0b"])
def test_invalid_database_names(session, name):
    with pytest.raises((ValueError, TextEncodingError)):
        famepy.open_database(name, session=session)


def test_work_database_singleton(session):
    work = famepy.work_database(session=session)
    assert work.is_work and work.mode == AccessMode.UPDATE
    assert famepy.work_database(session=session) is work
    assert repr(work) == "Database(work database, mode=update, open)"
    famepy.write_object(work, "w", famepy.scalar("precision", 3.0))
    work.post()
    work.close()
    assert not work.is_open
    reopened = famepy.work_database(session=session)
    assert reopened is not work and reopened.is_open


def test_bytes_and_pathlike_names(session, tmp_path):
    database = famepy.open_database(bytes(tmp_path / "b.db"), "create", session=session)
    assert isinstance(database, Database)
    database.close()
    database.close()


def test_failed_close_keeps_the_handle_tracked_for_retry(session, tmp_path):
    database = famepy.open_database(tmp_path / "c.db", "create", session=session)
    fake = session._native.fake
    fake.fail_next["cfmcldb"] = 903
    with pytest.raises(FameError):
        database.close()
    assert database.is_open
    assert database in session.open_databases and database.key in fake.handles
    database.close()
    assert not database.is_open
    assert database not in session.open_databases and database.key not in fake.handles
    database.close()


def test_finalize_still_closes_a_handle_whose_close_failed(session, tmp_path):
    database = famepy.open_database(tmp_path / "c.db", "create", session=session)
    fake = session._native.fake
    fake.fail_next["cfmcldb"] = 903
    with pytest.raises(FameError):
        database.close()
    session.finalize()
    assert session.last_cleanup_statuses == ()
    assert not database.is_open and fake.handles == {}


def test_stale_handle_is_rejected_inside_the_lock(session, tmp_path, monkeypatch):
    """A close and key reuse between validation and the native call is caught."""
    fake = session._native.fake
    old = famepy.open_database(tmp_path / "old.db", "create", session=session)
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = Database.operation

    @contextlib.contextmanager
    def gated(self, action):
        if threading.current_thread().name == "writer" and action == "write object":
            entered.set()
            assert release.wait(5)
        with original(self, action) as native:
            yield native

    monkeypatch.setattr(Database, "operation", gated)

    def write():
        try:
            famepy.write_object(old, "wrong", famepy.scalar("precision", 123.0))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    writer = threading.Thread(target=write, name="writer")
    writer.start()
    assert entered.wait(5)
    old_key = old.key
    old.close()
    fake.next_key = old_key
    other = famepy.open_database(tmp_path / "other.db", "create", session=session)
    assert other.key == old_key
    fake.calls.clear()
    release.set()
    writer.join(5)
    assert not writer.is_alive()
    assert [type(error) for error in errors] == [StaleHandleError]
    assert fake.calls == []
    assert "WRONG" not in fake.handles[other.key].objects
    other.close()


def test_close_waits_for_an_operation_in_progress(session, tmp_path, monkeypatch):
    fake = session._native.fake
    database = famepy.open_database(tmp_path / "a.db", "create", session=session)
    inside, release = threading.Event(), threading.Event()
    original = fake.new_object

    def slow_new_object(*args):
        inside.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(fake, "new_object", slow_new_object)
    writer = threading.Thread(
        target=lambda: famepy.write_object(database, "x", famepy.scalar("precision", 1.0))
    )
    writer.start()
    assert inside.wait(5)
    closer = threading.Thread(target=database.close)
    closer.start()
    closer.join(0.5)
    assert closer.is_alive() and database.is_open
    release.set()
    writer.join(5)
    closer.join(5)
    assert not database.is_open
    calls = fake.calls
    assert calls.index("fame_write_precisions") < calls.index("cfmcldb")
