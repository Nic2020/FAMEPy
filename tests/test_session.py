# SPDX-License-Identifier: MIT
import ctypes as ct
import os

import pytest
from fake_native import S_ALREADY_INITIALIZED, S_NOT_INITIALIZED, FakeStatus, make_fake

import famepy
from famepy import (
    ExtendedErrorRetrieval,
    FameError,
    InheritedRuntimeError,
    LicensingConfigurationError,
    RuntimeStateError,
    Session,
    StaleHandleError,
    UnsupportedOperationError,
    _runtime,
)
from famepy._discovery import discover


def _retrieval():
    return ExtendedErrorRetrieval(
        query_length=lambda native: len(native.fake.error_text),
        fetch=lambda native, buffer: ct.memmove(buffer, native.fake.error_text, len(buffer) - 1),
    )


def test_initialize_is_idempotent_and_generation_increments(fake):
    owner = Session(native=fake)
    assert owner.state == "loaded"
    assert owner.initialize() is owner
    assert owner.initialize() is owner
    assert fake.fake.init_count == 1
    assert owner.generation == 1
    assert owner.version() == 11.8
    owner.reset()
    assert owner.generation == 2
    assert fake.fake.init_count == 2 and fake.fake.fin_count == 1
    owner.finalize()
    owner.finalize()
    assert owner.state == "finalized"
    assert fake.fake.fin_count == 2


def test_sentinels_only_after_initialization(fake):
    owner = Session(native=fake)
    with pytest.raises(RuntimeStateError):
        _ = owner.sentinels
    with pytest.raises(RuntimeStateError, match="initialized"):
        owner.version()
    owner.initialize()
    assert owner.sentinels.string_nc == b"NC"
    owner.finalize()
    with pytest.raises(RuntimeStateError):
        _ = owner.sentinels


def test_failed_startup_leaves_runtime_retryable(fake):
    fake.fake.fail_next["cfmini"] = 97
    owner = Session(native=fake)
    with pytest.raises(FameError) as error:
        owner.initialize()
    assert error.value.status == 97
    assert owner.state == "loaded"
    assert _runtime._OWNER is None
    owner.initialize()
    assert owner.is_initialized


def test_failure_after_cfmini_is_torn_down(fake, monkeypatch):
    owner = Session(native=fake)
    monkeypatch.setattr(
        fake.fake, "sentinels", lambda: (_ for _ in ()).throw(FakeStatus(S_NOT_INITIALIZED))
    )
    with pytest.raises(FameError):
        owner.initialize()
    assert owner.state == "finalized"
    assert fake.fake.initialized is False
    assert _runtime._OWNER is None


def test_startup_cleanup_failure_retains_ownership(fake, monkeypatch):
    owner = Session(native=fake)
    monkeypatch.setattr(
        fake.fake, "sentinels", lambda: (_ for _ in ()).throw(FakeStatus(S_NOT_INITIALIZED))
    )
    fake.fake.fail_next["cfmfin"] = 55
    with pytest.raises(FameError):
        owner.initialize()
    assert owner.state == "broken" and _runtime._OWNER is owner
    assert fake.fake.initialized is True
    with pytest.raises(RuntimeStateError, match="owner"):
        Session(native=make_fake()).initialize()
    owner.finalize()
    assert owner.state == "finalized" and _runtime._OWNER is None
    assert fake.fake.initialized is False


def test_single_owner_per_process(fake):
    first = Session(native=fake).initialize()
    second = Session(native=make_fake())
    with pytest.raises(RuntimeStateError, match="owns"):
        second.initialize()
    first.finalize()
    second.initialize()
    second.finalize()


def test_double_native_initialization_is_a_status(fake):
    fake.fake.initialized = True
    owner = Session(native=fake)
    with pytest.raises(FameError) as error:
        owner.initialize()
    assert error.value.status == S_ALREADY_INITIALIZED


def test_finalize_closes_databases_and_invalidates_handles(session, tmp_path):
    database = famepy.open_database(tmp_path / "a.db", "create", session=session)
    work = famepy.work_database(session=session)
    assert len(session.open_databases) == 2
    session.reset()
    assert not database.is_open and not work.is_open
    with pytest.raises(StaleHandleError):
        database.post()
    with pytest.raises(StaleHandleError):
        famepy.quick_info(database, "x")
    database.close()
    assert session.last_cleanup_statuses == ()
    fresh = famepy.work_database(session=session)
    assert fresh is not work and fresh.is_open


def test_finalize_records_close_failures(session, tmp_path):
    database = famepy.open_database(tmp_path / "a.db", "create", session=session)
    session._native.fake.fail_next["cfmcldb"] = 903
    session.finalize()
    assert session.last_cleanup_statuses == (903,)
    assert not database.is_open


def test_broken_finalization_retains_ownership_until_a_retry_succeeds(fake):
    owner = Session(native=fake).initialize()
    fake.fake.fail_next["cfmfin"] = 55
    with pytest.raises(FameError):
        owner.finalize()
    assert owner.state == "broken" and _runtime._OWNER is owner
    assert fake.fake.initialized is True
    with pytest.raises(RuntimeStateError, match="retry"):
        owner.initialize()
    # A second wrapper over the SAME backend is stopped by the Python guard,
    # before it can reach cfmini on a library that is still initialized.
    other = Session(native=fake)
    with pytest.raises(RuntimeStateError, match="owner"):
        other.initialize()
    assert fake.fake.init_count == 1
    with pytest.raises(RuntimeStateError):
        famepy.initialize()
    assert famepy.current_session() is owner
    fake.fake.fail_next["cfmfin"] = 56
    with pytest.raises(FameError):
        famepy.finalize()
    assert owner.state == "broken" and _runtime._OWNER is owner
    famepy.finalize()
    assert owner.state == "finalized" and _runtime._OWNER is None
    assert fake.fake.initialized is False
    other.initialize()
    assert other.is_initialized and famepy.current_session() is other
    other.finalize()


def test_inherited_process_is_rejected_even_for_new_wrappers(fake, monkeypatch):
    Session(native=fake).initialize()
    _runtime._after_fork()
    assert _runtime._INHERITED
    with pytest.raises(InheritedRuntimeError):
        Session(native=make_fake()).initialize()
    with pytest.raises(InheritedRuntimeError):
        famepy.current_session()


def test_inherited_broken_owner_is_still_rejected(fake):
    owner = Session(native=fake).initialize()
    fake.fake.fail_next["cfmfin"] = 55
    with pytest.raises(FameError):
        owner.finalize()
    _runtime._after_fork()
    assert _runtime._INHERITED
    with pytest.raises(InheritedRuntimeError):
        Session(native=make_fake()).initialize()


def test_pid_change_is_rejected(fake, monkeypatch):
    owner = Session(native=fake).initialize()
    monkeypatch.setattr(os, "getpid", lambda: -5)
    with pytest.raises(InheritedRuntimeError):
        owner.version()


def test_licensing_environment_required_for_native_libraries(tmp_path, monkeypatch):
    path = tmp_path / "chli.dll"
    path.touch()
    owner = Session(discover(path), environ={})
    monkeypatch.setattr(_runtime, "CtypesNative", lambda library: make_fake())
    monkeypatch.setattr(ct, "CDLL", lambda p: object())
    with pytest.raises(LicensingConfigurationError):
        owner.initialize()
    assert owner.state == "loaded"
    licensed = Session(discover(path), environ={"FAME": "x"})
    licensed.initialize()
    assert licensed.is_initialized
    licensed.finalize()


def test_windows_directories_are_added_and_released_on_failure(tmp_path, monkeypatch):
    root = tmp_path / "root"
    library = root / "64" / "chli.dll"
    library.parent.mkdir(parents=True)
    library.touch()
    candidate = discover(library, root=root)
    assert candidate.trusted_directories == (library.parent, root)
    added, closed = [], []

    class Handle:
        def __init__(self, directory):
            self.directory = directory

        def close(self):
            closed.append(self.directory)

    monkeypatch.setattr(_runtime.sys, "platform", "win32")
    monkeypatch.setattr(
        _runtime.os, "add_dll_directory", lambda d: added.append(d) or Handle(d), raising=False
    )
    monkeypatch.setattr(ct, "CDLL", lambda p: (_ for _ in ()).throw(OSError(126, "private")))
    with pytest.raises(famepy.LibraryLoadError):
        _runtime.Runtime(candidate).load()
    assert added == [str(library.parent), str(root)]
    assert closed == added
    monkeypatch.setattr(ct, "CDLL", lambda p: object())
    runtime = _runtime.Runtime(candidate)
    runtime.load()
    assert len(runtime._directories) == 2


def test_module_level_api(fake, monkeypatch, tmp_path):
    path = tmp_path / "chli.dll"
    path.touch()
    monkeypatch.setattr(_runtime, "CtypesNative", lambda library: fake)
    monkeypatch.setattr(ct, "CDLL", lambda p: object())
    monkeypatch.setenv("FAME", str(tmp_path))
    with pytest.raises(RuntimeStateError):
        famepy.current_session()
    owner = famepy.initialize(path)
    assert famepy.current_session() is owner
    assert famepy.initialize() is owner
    with pytest.raises(RuntimeStateError):
        famepy.initialize(tmp_path / "other.dll")
    assert famepy.version() == 11.8
    assert famepy.reset() is owner and owner.generation == 2
    famepy.finalize()
    assert owner.state == "finalized"
    famepy.finalize()


def test_library_is_fixed_for_the_process_once_loaded(fake, monkeypatch, tmp_path):
    path = tmp_path / "chli.dll"
    path.touch()
    other = tmp_path / "other.dll"
    other.touch()
    first = famepy.default_session(path)
    # Nothing has been loaded yet, so a different library may still be chosen.
    replaced = famepy.default_session(other)
    assert replaced is not first
    monkeypatch.setattr(_runtime, "CtypesNative", lambda library: fake)
    monkeypatch.setattr(ct, "CDLL", lambda p: object())
    monkeypatch.setenv("FAME", str(tmp_path))
    owner = famepy.initialize()
    assert owner is replaced
    owner.finalize()
    with pytest.raises(RuntimeStateError, match="fixed"):
        famepy.default_session(path)
    with pytest.raises(RuntimeStateError, match="fixed"):
        famepy.initialize(path)
    assert famepy.initialize() is owner
    owner.finalize()


def test_extended_error_is_captured_at_the_failure(session, tmp_path):
    with pytest.raises(UnsupportedOperationError, match="cfmlerr"):
        session.extended_error_text()
    session.extended_error_retrieval = _retrieval()
    with pytest.raises(RuntimeStateError, match="captured"):
        session.extended_error_text()
    session._native.fake.error_text = b"synthetic message"
    with pytest.raises(FameError) as error:
        famepy.open_database(tmp_path / "missing.db", session=session)
    assert error.value.extended_text == b"synthetic message"
    assert "synthetic" not in str(error.value)
    assert session.extended_error_text() == b"synthetic message"
    # The stored text belongs to that failure; it is not re-read later.
    session._native.fake.error_text = b"changed later"
    assert session.extended_error_text() == b"synthetic message"


def test_extended_error_capture_failure_never_masks_the_status(session, tmp_path):
    for length in (2**20, -1):
        session.extended_error_retrieval = ExtendedErrorRetrieval(
            query_length=lambda native, length=length: length, fetch=lambda native, buffer: None
        )
        with pytest.raises(FameError) as error:
            famepy.open_database(tmp_path / "missing.db", session=session)
        assert error.value.extended_text is None
        assert session.extended_error_capture_failure == "DataValidationError"
        with pytest.raises(RuntimeStateError):
            session.extended_error_text()
    session.extended_error_retrieval = ExtendedErrorRetrieval(
        query_length=lambda native: 0, fetch=lambda native, buffer: None
    )
    with pytest.raises(FameError) as error:
        famepy.open_database(tmp_path / "missing.db", session=session)
    assert error.value.extended_text == b""
    assert session.extended_error_text() == b""


def test_operations_hold_the_process_lock(session):
    with session.operation("test"):
        assert _runtime.LOCK.acquire(blocking=False)
        _runtime.LOCK.release()
    with pytest.raises(RuntimeStateError):
        with Session(native=make_fake()).operation("test"):
            pass
