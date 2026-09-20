# SPDX-License-Identifier: MIT
import ctypes as ct
import io
from pathlib import Path

import pytest

import famepy
from famepy import CommandError, ExtendedErrorRetrieval, IncludeError, expand_input, fame
from famepy._command import MAX_COMMAND_BYTES


def test_command_output_capture_quiet_and_stream(session, tmp_path):
    fake = session._native.fake
    assert fame("disp 1", session=session, temp_dir=tmp_path) == b"echo: disp 1\n"
    assert fame("disp 2", session=session, quiet=True, temp_dir=tmp_path) == b""
    stream = io.BytesIO()
    fame("disp 3", session=session, output=stream, temp_dir=tmp_path)
    assert stream.getvalue() == b"echo: disp 3\n"
    assert fake.commands[0].startswith(b'output file("') and fake.commands[0].endswith(b'!")')
    assert fake.commands[1] == b"disp 1" and fake.commands[2] == b"output terminal"
    assert fake.output_path is None
    assert list(tmp_path.iterdir()) == []


def test_failure_restores_output_and_keeps_partial_output_off_message(session, tmp_path):
    fake = session._native.fake
    with pytest.raises(CommandError) as error:
        fame("fail 513", session=session, temp_dir=tmp_path)
    assert error.value.status == 513
    assert error.value.output == b"partial output before failure\n"
    assert "partial" not in str(error.value)
    assert fake.output_path is None
    assert fake.commands[-1] == b"output terminal"
    assert list(tmp_path.iterdir()) == []


def test_redirect_failure(session, tmp_path):
    fake = session._native.fake
    fake.fail_next["cfmfame"] = 67
    with pytest.raises(CommandError) as error:
        fame("disp 1", session=session, temp_dir=tmp_path)
    assert error.value.status == 67 and error.value.output is None
    assert error.value.stage == "redirect" and error.value.restore_status is None
    assert "(redirect)" in str(error.value)
    # The payload is never issued after a failed redirection.
    assert fake.commands == []
    assert list(tmp_path.iterdir()) == []


def test_stages_and_original_error_preservation(session, tmp_path):
    fake = session._native.fake
    fake.refuse_redirect = 513
    with pytest.raises(CommandError) as error:
        fame("disp 1", session=session, temp_dir=tmp_path)
    assert error.value.stage == "redirect" and error.value.status == 513
    assert fake.commands == [fake.commands[0]] and fake.commands[0].startswith(b"output file(")
    fake.refuse_redirect = None
    # Payload failure keeps the payload status even when the restoration fails too.
    fake.refuse_restore = 44
    with pytest.raises(CommandError) as error:
        fame("fail 513", session=session, temp_dir=tmp_path)
    assert error.value.stage == "command" and error.value.status == 513
    assert error.value.restore_status == 44
    assert error.value.output == b"partial output before failure\n"
    with pytest.raises(CommandError) as error:
        fame("disp 1", session=session, temp_dir=tmp_path)
    assert error.value.stage == "restore" and error.value.status == 44
    assert error.value.output == b"echo: disp 1\n"
    assert fake.commands[-1] == b"output terminal"
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        CommandError(1, stage="elsewhere")


def test_output_file_is_created_by_the_library_not_the_package(session, tmp_path, monkeypatch):
    """The redirection names a fresh file inside a private directory."""
    fake = session._native.fake
    seen = {}
    original = fake.execute

    def execute(command):
        if command.startswith(b'output file("'):
            path = Path(command[13:-3].decode("ascii"))
            seen["existed_before"] = path.exists()
            seen["parent_private"] = path.parent.parent == tmp_path
        return original(command)

    monkeypatch.setattr(fake, "execute", execute)
    assert fame("disp 1", session=session, temp_dir=tmp_path) == b"echo: disp 1\n"
    assert seen == {"existed_before": False, "parent_private": True}
    assert list(tmp_path.iterdir()) == []
    # A library that never created the file yields empty output, not an error.
    monkeypatch.setattr(fake, "_emit", lambda text: None)
    fake.refuse_redirect = None
    original_execute = original

    def no_file(command):
        if command.startswith(b'output file("'):
            return 0
        return original_execute(command)

    monkeypatch.setattr(fake, "execute", no_file)
    assert fame("disp 1", session=session, temp_dir=tmp_path) == b""
    assert list(tmp_path.iterdir()) == []


def test_restore_failure_is_reported(session, tmp_path, monkeypatch):
    fake = session._native.fake
    original = fake.execute

    def flaky(command):
        if command == b"output terminal":
            fake.output_path = None
            return 44
        return original(command)

    monkeypatch.setattr(fake, "execute", flaky)
    with pytest.raises(CommandError) as error:
        fame("disp 1", session=session, temp_dir=tmp_path)
    assert error.value.status == 44 and error.value.output == b"echo: disp 1\n"


def test_command_text_policy(session, tmp_path):
    with pytest.raises(famepy.TextEncodingError):
        fame("disp café", session=session, temp_dir=tmp_path)
    with pytest.raises(ValueError):
        fame(
            b"x" * (MAX_COMMAND_BYTES + 1),
            session=session,
            expand_includes=False,
            temp_dir=tmp_path,
        )
    assert fame(b"disp raw", session=session, temp_dir=tmp_path) == b"echo: disp raw\n"


def test_input_expansion_rules(tmp_path):
    (tmp_path / "one.inp").write_bytes(b"line a\ninput two\n")
    (tmp_path / "two.inp").write_bytes(b"line b")
    (tmp_path / "bang").write_bytes(b"line c")
    (tmp_path / "quoted name.inp").write_bytes(b"line d")
    expanded = expand_input(b"start; input one; end", base_dir=tmp_path)
    assert expanded == b"start;\nline a\n\nline b\n\n; end"
    assert expand_input(b"input bang!", base_dir=tmp_path) == b"\nline c\n"
    assert expand_input(b'input "quoted name"', base_dir=tmp_path) == b"\nline d\n"
    assert expand_input(b'input file("two")', base_dir=tmp_path) == b"\nline b\n"
    assert expand_input(b"INPUT two.inp", base_dir=tmp_path) == b"\nline b\n"
    absolute = str(tmp_path / "two.inp").encode()
    assert expand_input(b"input " + absolute, base_dir=tmp_path / "elsewhere") == b"\nline b\n"
    assert expand_input(b"noinput here", base_dir=tmp_path) == b"noinput here"


def test_input_refusals(tmp_path):
    (tmp_path / "a.inp").write_bytes(b"input b\n")
    (tmp_path / "b.inp").write_bytes(b"input a\n")
    (tmp_path / "self.inp").write_bytes(b"input self")
    (tmp_path / "big.inp").write_bytes(b"x" * 100)
    (tmp_path / "nul.inp").write_bytes(b"a\0b")
    with pytest.raises(IncludeError, match="cycle"):
        expand_input(b"input a", base_dir=tmp_path)
    with pytest.raises(IncludeError, match="cycle"):
        expand_input(b"input self", base_dir=tmp_path)
    with pytest.raises(IncludeError, match="computed"):
        expand_input(b"input file(name)", base_dir=tmp_path)
    with pytest.raises(IncludeError, match="does not exist"):
        expand_input(b"input missing", base_dir=tmp_path)
    with pytest.raises(IncludeError, match="size"):
        expand_input(b"input big", base_dir=tmp_path, max_bytes=50)
    with pytest.raises(IncludeError, match="NUL"):
        expand_input(b"input nul", base_dir=tmp_path)
    with pytest.raises(IncludeError, match="ASCII"):
        expand_input(b"input caf\xc3\xa9", base_dir=tmp_path)
    (tmp_path / "d1.inp").write_bytes(b"input d2")
    (tmp_path / "d2.inp").write_bytes(b"input d3")
    (tmp_path / "d3.inp").write_bytes(b"leaf")
    assert b"leaf" in expand_input(b"input d1", base_dir=tmp_path, max_depth=3)
    with pytest.raises(IncludeError, match="depth"):
        expand_input(b"input d1", base_dir=tmp_path, max_depth=2)


def test_run_command_expands_relative_to_base_dir(session, tmp_path):
    (tmp_path / "inc.inp").write_bytes(b"disp included")
    result = fame("input inc", session=session, base_dir=tmp_path, temp_dir=tmp_path)
    assert result == b"echo: disp included\n"  # the fake echoes each non-empty line
    assert not (tmp_path / "inc.inp").read_bytes() != b"disp included"


def test_temp_paths_are_validated_before_redirection(session, tmp_path, monkeypatch):
    import famepy._command as module

    for name in ('q"uote', "café"):
        directory = tmp_path / name  # not created: such names are refused before any use
        monkeypatch.setattr(module.tempfile, "mkdtemp", lambda d=directory, **k: str(d))
        with pytest.raises(famepy.TextEncodingError):
            fame("disp 1", session=session, temp_dir=tmp_path)
        assert session._native.fake.commands == []


def test_consecutive_input_statements_are_all_expanded(tmp_path):
    (tmp_path / "a.inp").write_bytes(b"display 1")
    (tmp_path / "b.inp").write_bytes(b"display 2")
    assert (
        expand_input(b"input a\ninput b\n", base_dir=tmp_path) == b"\ndisplay 1\n\n\ndisplay 2\n\n"
    )
    assert expand_input(b"input a;input b", base_dir=tmp_path) == b"\ndisplay 1\n;\ndisplay 2\n"
    assert (
        expand_input(b"input a; input b; input a", base_dir=tmp_path)
        == b"\ndisplay 1\n;\ndisplay 2\n;\ndisplay 1\n"
    )
    assert (
        expand_input(b"INPUT a\n\nInput b", base_dir=tmp_path) == b"\ndisplay 1\n\n\n\ndisplay 2\n"
    )
    # INPUT inside a quoted argument or after other text is not a statement.
    assert expand_input(b'display "input a"', base_dir=tmp_path) == b'display "input a"'
    assert expand_input(b"show input a", base_dir=tmp_path) == b"show input a"


def test_input_file_read_errors_are_redacted(tmp_path):
    (tmp_path / "directory.inp").mkdir()
    with pytest.raises(IncludeError) as error:
        expand_input(b"input directory", base_dir=tmp_path)
    assert getattr(error.value, "filename", None) is None
    assert "directory" not in str(error.value) and str(tmp_path) not in str(error.value)
    assert error.value.__cause__ is None


def test_extended_error_is_captured_before_output_restoration(session, tmp_path, monkeypatch):
    fake = session._native.fake
    seen = []

    def fetch(native, buffer):
        seen.append(list(native.fake.commands))
        ct.memmove(buffer, native.fake.error_text, len(buffer) - 1)

    session.extended_error_retrieval = ExtendedErrorRetrieval(
        lambda native: len(native.fake.error_text), fetch
    )
    original = fake.execute

    def execute(command):
        status = original(command)
        if command == b"output terminal":
            fake.error_text = b"after output restoration"
        return status

    monkeypatch.setattr(fake, "execute", execute)
    with pytest.raises(CommandError) as error:
        fame("fail 513", session=session, temp_dir=tmp_path)
    assert error.value.extended_text == b"synthetic failure for fail 513"
    assert seen[0][-1] == b"fail 513"  # captured before "output terminal" was issued
    assert session.extended_error_text() == b"synthetic failure for fail 513"
    assert "synthetic" not in str(error.value)
    assert fake.commands[-1] == b"output terminal"
    assert list(tmp_path.iterdir()) == []


def test_display_sums_are_evaluated_by_the_fake(session, tmp_path):
    assert fame("display 2+2", session=session, temp_dir=tmp_path) == b"4\n"
