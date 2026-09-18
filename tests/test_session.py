# SPDX-License-Identifier: MIT
import ctypes as ct
import os

import pytest
from fake_native import S_ALREADY_INITIALIZED, S_NOT_INITIALIZED, SENTINELS, FakeStatus, make_fake

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


def test_initialize_is_idempotent_and_one_shot(fake):
    owner = Session(native=fake)
    assert owner.state == "loaded" and owner.generation == 0
    assert owner.initialize() is owner
    assert owner.initialize() is owner
    assert fake.fake.init_count == 1
    assert owner.generation == 1
    assert owner.version() == 11.8
    owner.finalize()
    assert owner.state == "finalized" and owner.is_terminal
    assert owner.finalize_status == 0
    owner.finalize()  # harmless Python-level no-op: cfmfin is issued once
    assert fake.fake.fin_count == 1 and fake.fake.calls.count("cfmfin") == 1
    with pytest.raises(RuntimeStateError, match="spawned process"):
        owner.initialize()
    assert fake.fake.calls.count("cfmini") == 1
    assert owner.generation == 1


def test_reset_is_unsupported_and_never_touches_the_runtime(fake):
    owner = Session(native=fake)
    with pytest.raises(UnsupportedOperationError, match="reset"):
        owner.reset()
    assert owner.state == "loaded" and fake.fake.calls == []
    owner.initialize()
    fake.fake.calls.clear()
    with pytest.raises(UnsupportedOperationError, match="spawned process"):
        owner.reset()
    with pytest.raises(UnsupportedOperationError):
        famepy.reset()
    assert owner.state == "initialized" and fake.fake.calls == []
    assert owner.version() == 11.8
    owner.finalize()
    with pytest.raises(UnsupportedOperationError):
        owner.reset()
    assert fake.fake.fin_count == 1 and fake.fake.init_count == 1


def test_sentinels_only_after_initialization(fake):
    owner = Session(native=fake)
    with pytest.raises(RuntimeStateError):
        _ = owner.sentinels
    with pytest.raises(RuntimeStateError, match="initialized"):
        owner.version()
    owner.initialize()
    assert owner.sentinels.string_nc == SENTINELS.string_nc and not SENTINELS.string_nc.isascii()
    owner.finalize()
    with pytest.raises(RuntimeStateError):
        _ = owner.sentinels


def test_pre_native_failures_do_not_consume_initialization(tmp_path, monkeypatch):
    path = tmp_path / "chli.dll"
    path.touch()
    fake = make_fake()
    monkeypatch.setattr(_runtime, "CtypesNative", lambda library: fake)
    monkeypatch.setattr(ct, "CDLL", lambda p: object())
    owner = Session(discover(path), environ={})
    with pytest.raises(LicensingConfigurationError):
        owner.initialize()
    assert owner.state == "loaded" and _runtime._OWNER is None
    assert fake.fake.calls == []
    # The same process may still initialize once the environment is fixed.
    owner._environ = {"FAME": "x"}
    owner.initialize()
    assert owner.is_initialized and fake.fake.init_count == 1
    owner.finalize()


def test_failed_native_initialization_is_terminal(fake):
    fake.fake.fail_next["cfmini"] = 97
    owner = Session(native=fake)
    with pytest.raises(FameError) as error:
        owner.initialize()
    assert error.value.status == 97
    assert owner.state == "failed" and owner.is_terminal
    assert _runtime._OWNER is owner
    with pytest.raises(RuntimeStateError, match="failed"):
        owner.initialize()
    with pytest.raises(RuntimeStateError):
        Session(native=fake).initialize()
    with pytest.raises(RuntimeStateError):
        famepy.initialize()
    assert fake.fake.calls.count("cfmini") == 1
    owner.finalize()  # harmless; no cfmfin is issued for a never-initialized library
    assert "cfmfin" not in fake.fake.calls


def test_setup_failure_after_cfmini_is_terminal_with_one_cleanup_cfmfin(fake, monkeypatch):
    owner = Session(native=fake)
    monkeypatch.setattr(
        fake.fake, "sentinels", lambda: (_ for _ in ()).throw(FakeStatus(S_NOT_INITIALIZED))
    )
    with pytest.raises(FameError) as error:
        owner.initialize()
    assert error.value.status == S_NOT_INITIALIZED
    assert owner.state == "finalized" and owner.finalize_status == 0
    assert fake.fake.initialized is False and fake.fake.fin_count == 1
    assert _runtime._OWNER is owner
    with pytest.raises(RuntimeStateError):
        owner.initialize()
    with pytest.raises(RuntimeStateError):
        Session(native=make_fake()).initialize()
    owner.finalize()
    assert fake.fake.calls.count("cfmfin") == 1


def test_setup_cleanup_failure_is_broken_and_keeps_the_original_error(fake, monkeypatch):
    owner = Session(native=fake)
    monkeypatch.setattr(
        fake.fake, "sentinels", lambda: (_ for _ in ()).throw(FakeStatus(S_NOT_INITIALIZED))
    )
    fake.fake.fail_next["cfmfin"] = 55
    with pytest.raises(FameError) as error:
        owner.initialize()
    assert error.value.status == S_NOT_INITIALIZED  # the setup failure, not the cleanup
    assert "cleanup cfmfin failed" in getattr(error.value, "__notes__", [])
    assert owner.state == "broken" and owner.finalize_status == 55
    assert _runtime._OWNER is owner
    with pytest.raises(RuntimeStateError, match="broken"):
        owner.initialize()
    with pytest.raises(RuntimeStateError, match="spawned process"):
        Session(native=make_fake()).initialize()
    owner.finalize()
    famepy.finalize()
    assert fake.fake.calls.count("cfmfin") == 1
    assert owner.state == "broken"


def test_single_owner_per_process_even_after_finalization(fake):
    first = Session(native=fake).initialize()
    second = Session(native=make_fake())
    with pytest.raises(RuntimeStateError, match="owns"):
        second.initialize()
    first.finalize()
    with pytest.raises(RuntimeStateError, match="spawned process"):
        second.initialize()
    assert second.state == "loaded" and second._native.fake.calls == []
    with pytest.raises(RuntimeStateError, match="spawned process"):
        famepy.current_session()


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
    session.finalize()
    assert not database.is_open and not work.is_open
    with pytest.raises(StaleHandleError, match="finalized"):
        database.post()
    with pytest.raises(StaleHandleError):
        famepy.quick_info(database, "x")
    with pytest.raises(StaleHandleError):
        with database:
            pass
    database.close()
    assert session.last_cleanup_statuses == ()
    with pytest.raises(RuntimeStateError):
        famepy.work_database(session=session)
    assert session._native.fake.calls.count("cfmcldb") == 2


def test_finalize_records_close_failures(session, tmp_path):
    database = famepy.open_database(tmp_path / "a.db", "create", session=session)
    session._native.fake.fail_next["cfmcldb"] = 903
    session.finalize()
    assert session.last_cleanup_statuses == (903,)
    assert not database.is_open


def test_broken_finalization_is_terminal_and_never_retried(fake):
    owner = Session(native=fake).initialize()
    fake.fake.fail_next["cfmfin"] = 55
    with pytest.raises(FameError) as error:
        owner.finalize()
    assert error.value.status == 55
    assert owner.state == "broken" and owner.finalize_status == 55
    assert _runtime._OWNER is owner
    with pytest.raises(RuntimeStateError, match="broken"):
        owner.initialize()
    other = Session(native=fake)
    with pytest.raises(RuntimeStateError, match="spawned process"):
        other.initialize()
    with pytest.raises(RuntimeStateError):
        famepy.initialize()
    with pytest.raises(RuntimeStateError, match="broken"):
        famepy.current_session()
    with pytest.raises(RuntimeStateError, match="broken"):
        owner.version()
    # Repeated Python-level cleanup is harmless and issues no second cfmfin.
    owner.finalize()
    famepy.finalize()
    assert fake.fake.calls.count("cfmfin") == 1 and fake.fake.init_count == 1
    assert owner.state == "broken"


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
    with pytest.raises(UnsupportedOperationError):
        famepy.reset()
    assert owner.is_initialized and owner.generation == 1
    famepy.finalize()
    assert owner.state == "finalized"
    famepy.finalize()
    fake.fake.calls.clear()
    with pytest.raises(RuntimeStateError, match="spawned process"):
        famepy.initialize()
    with pytest.raises(RuntimeStateError, match="spawned process"):
        famepy.initialize(path)
    other = tmp_path / "other.dll"
    other.touch()
    with pytest.raises(RuntimeStateError, match="spawned process"):
        famepy.initialize(other)
    with pytest.raises(RuntimeStateError, match="spawned process"):
        famepy.default_session(other)
    with pytest.raises(RuntimeStateError, match="spawned process"):
        famepy.version()
    assert fake.fake.calls == []


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
    with pytest.raises(RuntimeStateError, match="fixed"):
        famepy.default_session(path)
    with pytest.raises(RuntimeStateError, match="fixed"):
        famepy.initialize(path)
    assert famepy.initialize() is owner
    owner.finalize()
    with pytest.raises(RuntimeStateError, match="spawned process"):
        famepy.default_session(path)
    assert famepy.default_session() is owner


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
