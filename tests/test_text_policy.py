# SPDX-License-Identifier: MIT
"""The value text policies: ASCII by default, raw bytes, and strict opt-in UTF-8.

Every case here runs against the fake backend, which stores string bytes
exactly. The expected bytes are written out as hex literals so that the
round trips compare against independent data, not against the codec.
"""

import pytest
from tsecon import mm

import famepy
from famepy import DataValidationError, TextEncodingError, bridge, migration
from famepy._constants import FREQUENCY_CASE
from famepy._text import (
    VALUE_TEXT_POLICIES,
    check_value_policy,
    decode_value,
    encode_value,
    from_native,
    to_native,
)
from famepy.bridge import NameList, StringSeries, Text

ACCENT = "caf\xe9"  # U+00E9 -> c3 a9
INTERNAL = "caf\xe9 au lait"
CJK = "\U000065e5\U0000672c\U00008a9e"  # three 3-byte characters
EMOJI = "ok \U0001f600"  # supplementary, 4 bytes
CASES = {
    "internal": (INTERNAL, "636166c3a9206175206c616974"),
    "terminal": (ACCENT, "636166c3a9"),
    "cjk": (CJK, "e697a5e69cace8aa9e"),
    "supplementary": (EMOJI, "6f6b20f09f9880"),
    "supplementary_internal": ("\U0001f600 ok", "f09f9880206f6b"),
    "ascii": ("plain text", "706c61696e2074657874"),
    "empty": ("", ""),
}


# -- the codec ------------------------------------------------------------------


def test_policy_set_and_validation():
    assert VALUE_TEXT_POLICIES == ("ascii", "bytes", "utf-8")
    assert bridge.TEXT_POLICIES == VALUE_TEXT_POLICIES
    for policy in VALUE_TEXT_POLICIES:
        assert check_value_policy(policy) == policy
    for bad in ("utf8", "UTF-8", "latin-1", None, 3, b"utf-8"):
        with pytest.raises(ValueError, match="text must be"):
            check_value_policy(bad)
        if bad is not None:  # None means "not given" to check_policies
            with pytest.raises(ValueError, match="text must be"):
                bridge.check_policies(text=bad)


@pytest.mark.parametrize(("label", "case"), CASES.items())
def test_encode_value_matches_independent_bytes(label, case):
    text, expected_hex = case
    expected = bytes.fromhex(expected_hex)
    assert encode_value(text, "utf-8") == expected
    assert decode_value(expected, "utf-8") == text
    assert decode_value(expected, "bytes") == expected
    # bytes are preserved under every policy, whatever they contain
    for policy in VALUE_TEXT_POLICIES:
        assert encode_value(expected, policy) == expected
        assert encode_value(bytearray(expected), policy) == expected
        assert encode_value(memoryview(expected), policy) == expected
    if text.isascii():
        assert encode_value(text, "ascii") == expected
        assert encode_value(text, "bytes") == expected
        assert decode_value(expected, "ascii") == text
    else:
        for policy in ("ascii", "bytes"):
            with pytest.raises(TextEncodingError, match="ASCII"):
                encode_value(text, policy)
        with pytest.raises(TextEncodingError, match="not valid ASCII"):
            decode_value(expected, "ascii")


def test_codec_failures_are_explicit_and_never_replace():
    with pytest.raises(TextEncodingError, match="UTF-8"):
        encode_value("\ud800", "utf-8")  # lone surrogate
    with pytest.raises(TextEncodingError, match="UTF-8"):
        encode_value("a\udcffb", "utf-8")
    for policy in VALUE_TEXT_POLICIES:
        with pytest.raises(TextEncodingError, match="NUL"):
            encode_value("a\0b", policy)
        with pytest.raises(TextEncodingError, match="NUL"):
            encode_value(b"a\0b", policy)
        with pytest.raises(TypeError):
            encode_value(3, policy)
        with pytest.raises(TypeError):
            decode_value("text", policy)
    for invalid in (b"caf\xe9", b"\xff\xfe", b"\xc3", b"\xed\xa0\x80", b"\xc0\xaf"):
        with pytest.raises(TextEncodingError, match="not valid UTF-8"):
            decode_value(invalid, "utf-8")
        with pytest.raises(TextEncodingError, match="not valid ASCII"):
            decode_value(invalid, "ascii")
        assert decode_value(invalid, "bytes") == invalid
    # Error messages never echo the content.
    with pytest.raises(TextEncodingError) as info:
        encode_value(ACCENT, "ascii")
    assert ACCENT not in str(info.value)
    with pytest.raises(TextEncodingError) as info:
        decode_value(b"caf\xe9", "utf-8")
    assert "caf" not in str(info.value)


def test_names_paths_and_commands_keep_their_boundary():
    """The value codec does not widen ``to_native``/``from_native``."""
    with pytest.raises(TextEncodingError):
        to_native(ACCENT, what="object name")
    with pytest.raises(TextEncodingError):
        from_native(b"caf\xc3\xa9")
    assert to_native("ascii") == b"ascii"


# -- bridge values --------------------------------------------------------------


@pytest.mark.parametrize(("label", "case"), CASES.items())
def test_scalar_round_trip_under_utf8(db, label, case):
    text, expected_hex = case
    expected = bytes.fromhex(expected_hex)
    bridge.write_value(db, "s", text, text="utf-8")
    raw = famepy.read_object(db, "s")
    assert raw.kind == "string" and raw.value == expected
    assert bridge.read_value(db, "s", text="utf-8") == text
    assert bridge.read_scalar(db, "s", text="utf-8") == text
    assert bridge.read_value(db, "s", text="bytes") == expected
    if text.isascii():
        assert bridge.read_value(db, "s") == text
    else:
        with pytest.raises(TextEncodingError):
            bridge.read_value(db, "s")
        with pytest.raises(TextEncodingError):
            bridge.write_value(db, "t", text)
        with pytest.raises(TextEncodingError):
            bridge.write_value(db, "t", text, text="bytes")
    # The whole stored byte sequence is decoded: a terminal multibyte
    # character is not a problem for the package (a wrapper that slices by
    # the byte length would fail on "terminal", "cjk" and "supplementary").
    assert bridge.from_fame(raw, database=db, text="utf-8") == text


def test_to_fame_from_fame_take_the_policy(session):
    raw = bridge.to_fame(ACCENT, session=session, text="utf-8")
    assert raw.value == b"caf\xc3\xa9"
    with pytest.raises(TextEncodingError):
        bridge.to_fame(ACCENT, session=session)
    with pytest.raises(ValueError, match="text must be"):
        bridge.to_fame(ACCENT, session=session, text="latin-1")
    bridge.validate_value(ACCENT, text="utf-8")
    with pytest.raises(TextEncodingError):
        bridge.validate_value(ACCENT)
    assert bridge.from_fame(raw, session=session, text="utf-8") == ACCENT
    assert bridge.from_fame(raw, session=session, text="bytes") == b"caf\xc3\xa9"
    with pytest.raises(TextEncodingError):
        bridge.from_fame(raw, session=session)


def test_carriers_and_vectors_under_utf8(db):
    bridge.write_value(db, "t", Text("{caf\xe9}"), text="utf-8")
    assert famepy.quick_info(db, "t").kind == "string"
    assert famepy.read_object(db, "t").value == b"{caf\xc3\xa9}"
    assert bridge.read_value(db, "t", text="utf-8") == "{caf\xe9}"
    # Literal braces without the carrier are a namelist under every policy,
    # and namelist members stay printable ASCII: no policy widens names.
    with pytest.raises(ValueError, match="printable ASCII"):
        bridge.write_value(db, "n", "{caf\xe9}", text="utf-8")
    with pytest.raises(ValueError, match="printable ASCII"):
        NameList(["caf\xe9"])
    bridge.write_value(db, "n", "{a,b}", text="utf-8")
    assert bridge.read_value(db, "n", text="utf-8") == NameList(["A", "B"])

    bridge.write_value(db, "v", ["alpha", ACCENT, CJK], text="utf-8")
    raw = famepy.read_object(db, "v")
    assert raw.frequency == FREQUENCY_CASE and raw.first_index == 1
    assert raw.values == [b"alpha", b"caf\xc3\xa9", bytes.fromhex("e697a5e69cace8aa9e")]
    back = bridge.read_value(db, "v", text="utf-8")
    assert isinstance(back, StringSeries) and back.values == ("alpha", ACCENT, CJK)
    assert bridge.read_value(db, "v", text="bytes").values == tuple(raw.values)
    with pytest.raises(TextEncodingError):
        bridge.read_value(db, "v")
    with pytest.raises(TextEncodingError):
        bridge.write_value(db, "w", ("alpha", ACCENT))
    with pytest.raises(TextEncodingError):
        bridge.write_value(db, "w", ["alpha", "\ud800"], text="utf-8")

    series = StringSeries(mm(2020, 1), [ACCENT, None, "", b"raw\xff"])
    bridge.write_value(db, "d", series, text="utf-8")
    raw = famepy.read_object(db, "d")
    codes = famepy.classify_by_sentinel(raw.values, "string", db.session.sentinels).tolist()
    assert codes == [0, 1, 0, 0]
    assert raw.values[0] == b"caf\xc3\xa9" and raw.values[2] == b"" and raw.values[3] == b"raw\xff"
    # The missing observation is classified before any decoding; the raw
    # byte observation makes the strict decode fail as a whole.
    with pytest.raises(TextEncodingError):
        bridge.read_value(db, "d", text="utf-8")
    assert bridge.read_value(db, "d", text="bytes").values == (
        b"caf\xc3\xa9",
        None,
        b"",
        b"raw\xff",
    )
    bridge.write_value(db, "e", StringSeries(mm(2020, 1), [ACCENT, None, ""]), text="utf-8")
    assert bridge.read_value(db, "e", text="utf-8").values == (ACCENT, None, "")
    with pytest.raises(bridge.MissingValueError):
        bridge.read_value(db, "e", text="utf-8", missing="strict")


def test_sentinels_are_never_decoded(db):
    sentinels = db.session.sentinels
    raw = famepy.series(
        "string",
        FREQUENCY_CASE,
        1,
        [sentinels.string_nc, sentinels.string_na, sentinels.string_nd],
    )
    famepy.write_object(db, "m", raw)
    assert bridge.read_value(db, "m", text="utf-8").values == (None, None, None)
    assert bridge.read_value(db, "m").values == (None, None, None)
    famepy.write_object(db, "ms", famepy.scalar("string", sentinels.string_nc))
    assert bridge.read_value(db, "ms", text="utf-8") is None


def test_invalid_stored_bytes_are_refused_not_replaced(db):
    famepy.write_object(db, "s", famepy.scalar("string", b"caf\xe9"))
    with pytest.raises(TextEncodingError):
        bridge.read_value(db, "s", text="utf-8")
    with pytest.raises(TextEncodingError):
        bridge.read_value(db, "s")
    assert bridge.read_value(db, "s", text="bytes") == b"caf\xe9"


def test_names_take_no_policy(db):
    for policy in VALUE_TEXT_POLICIES:
        with pytest.raises(TextEncodingError):
            bridge.write_value(db, "caf\xe9", "x", text=policy)
    with pytest.raises(TextEncodingError):
        bridge.write_workspace(db, {"caf\xe9": "x"}, text="utf-8")


# -- validation before any destructive open --------------------------------------


def test_policy_and_encoding_failures_precede_the_path_open(session, tmp_path):
    fake = session._native.fake
    path = tmp_path / "t.db"
    bridge.write_value(path, "keep", 1.0, mode="create")
    before = famepy.read_object
    fake.calls.clear()
    with pytest.raises(ValueError, match="text must be"):
        bridge.write_value(path, "x", "text", mode="overwrite", text="latin-1")
    with pytest.raises(TextEncodingError):
        bridge.write_value(path, "x", ACCENT, mode="overwrite")
    with pytest.raises(TextEncodingError):
        bridge.write_value(path, "x", "\ud800", mode="overwrite", text="utf-8")
    with pytest.raises(TextEncodingError):
        bridge.write_scalar(path, "x", ACCENT, mode="overwrite", text="bytes")
    with pytest.raises(ValueError, match="text must be"):
        bridge.write_workspace(path, {"x": "text"}, mode="overwrite", text="utf8")
    with pytest.raises(TextEncodingError):
        bridge.write_workspace(path, {"x": ACCENT}, mode="overwrite")
    with pytest.raises(ValueError, match="text must be"):
        bridge.write_workspace_report(path, {"x": "text"}, mode="overwrite", text="utf8")
    with pytest.raises(ValueError, match="text must be"):
        bridge.read_value(path, "keep", text="utf8")
    with pytest.raises(ValueError, match="text must be"):
        bridge.read_workspace(path, text="utf8")
    with pytest.raises(ValueError, match="text must be"):
        bridge.read_workspace_report(path, text="utf8")
    assert fake.calls == []
    assert bridge.read_value(path, "keep") == 1.0 and before is famepy.read_object
    absent = tmp_path / "never.db"
    with pytest.raises(TextEncodingError):
        bridge.write_value(absent, "x", ACCENT, mode="create")
    assert not absent.exists()


# -- workspaces -----------------------------------------------------------------


def test_workspace_writes_read_and_contain_under_utf8(session, tmp_path):
    path = tmp_path / "w.db"
    data = {"ok": INTERNAL, "vec": [ACCENT, "b"], "plain": "plain"}
    names = bridge.write_workspace(path, data, mode="create", text="utf-8")
    assert names == ("ok", "vec", "plain")
    with famepy.open_database(path, session=session) as db:
        assert famepy.read_object(db, "OK").value == bytes.fromhex("636166c3a9206175206c616974")
    read = bridge.read_workspace(path, text="utf-8")
    assert read.ok == INTERNAL and read.vec.values == (ACCENT, "b") and read.plain == "plain"
    with pytest.raises(TextEncodingError):
        bridge.read_workspace(path)
    with pytest.raises(TextEncodingError):
        bridge.write_workspace(path, data, mode="update")

    report = bridge.read_workspace_report(path)
    assert not report.complete and list(report.workspace) == ["plain"]
    assert sorted((f.name, f.error_type, f.status) for f in report.failures) == [
        ("OK", "TextEncodingError", None),
        ("VEC", "TextEncodingError", None),
    ]
    fallback = bridge.read_workspace_report(path, raw_fallback=True)
    assert fallback.complete and fallback.raw == ("OK", "VEC")
    assert fallback.workspace.ok.value == bytes.fromhex("636166c3a9206175206c616974")
    assert bridge.read_workspace_report(path, text="utf-8").complete

    contained = bridge.write_workspace_report(
        path, {"good": ACCENT, "bad": "\ud800", "nul": "a\0b"}, mode="update", text="utf-8"
    )
    assert contained.written == ("good",) and contained.posted
    assert [(f.name, f.error_type) for f in contained.failures] == [
        ("bad", "TextEncodingError"),
        ("nul", "TextEncodingError"),
    ]
    assert bridge.read_value(path, "good", text="utf-8") == ACCENT
    nothing = bridge.write_workspace_report(
        tmp_path / "unopened.db", {"bad": "\ud800"}, mode="create", text="utf-8"
    )
    assert nothing.written == () and not nothing.posted and not (tmp_path / "unopened.db").exists()


# -- migration keeps its ASCII contract ------------------------------------------


@pytest.mark.skipif(not migration.dataecon_available()[0], reason="DataEcon extension unavailable")
def test_migration_does_not_take_the_utf8_policy(session, tmp_path):
    """A UTF-8 string written under the policy is a contained migration failure."""
    source = tmp_path / "t.db"
    with famepy.open_database(source, "create", session=session) as db:
        bridge.write_value(db, "accent", ACCENT, text="utf-8")
        bridge.write_value(db, "plain", "plain")
        db.post()
    report = migration.migrate(source, tmp_path / "t.daec", session=session)
    outcomes = {e.name: (e.action, e.error_type) for e in report.entries}
    assert outcomes == {"ACCENT": ("failed", "TextEncodingError"), "PLAIN": ("stored", None)}
    with pytest.raises(TypeError):
        migration.MigrationOptions(text="utf-8")


def test_read_helpers_reject_series_and_scalars_consistently(db):
    bridge.write_value(db, "s", ACCENT, text="utf-8")
    with pytest.raises(DataValidationError):
        bridge.read_tseries(db, "s")
