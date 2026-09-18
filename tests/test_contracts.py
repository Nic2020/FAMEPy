# SPDX-License-Identifier: MIT
"""Constants, text policy and pure validation contracts (no backend)."""

import numpy as np
import pytest

import famepy
from famepy import DataValidationError, RangeSpec, TextEncodingError, from_native, to_native
from famepy._abi import GLOBAL_TYPES, GLOBALS, PRESENCE_ONLY, SIGNATURES
from famepy._constants import (
    FREQUENCIES,
    ObjectType,
    frequency_code,
    frequency_name,
    is_date_type,
    type_code,
    type_name,
)
from famepy._native import MAX_OBSERVATIONS, check_buffer


def test_text_boundary():
    assert to_native("abc") == b"abc"
    assert to_native(b"\xff\x00"[:1]) == b"\xff"
    assert to_native(bytearray(b"x")) == b"x"
    with pytest.raises(TextEncodingError) as error:
        to_native("café", what="name")
    assert "caf" not in str(error.value)
    with pytest.raises(TextEncodingError):
        to_native(b"a\0b")
    with pytest.raises(TypeError):
        to_native(3)
    assert from_native(b"abc") == "abc"
    with pytest.raises(TextEncodingError):
        from_native(b"\xff")
    assert from_native(b"\xff", strict=False) == "\\xff"
    with pytest.raises(TypeError):
        from_native("abc")


def test_frequency_tables():
    assert frequency_code("monthly") == 129 == frequency_code(129)
    assert frequency_code("Quarterly December") == 162
    assert frequency_name(232) == "case"
    assert type_code("precision") == 5 and type_code("monthly") == 129
    assert type_code(ObjectType.STRING) == 4
    assert is_date_type(129) and not is_date_type(5) and not is_date_type(7)
    assert type_name(129) == "date:monthly" and type_name(2) == "namelist"
    assert len(FREQUENCIES) == 58
    for bad in ("nope", 7, True, 1.5):
        with pytest.raises((ValueError, TypeError)):
            frequency_code(bad)
    with pytest.raises(ValueError):
        frequency_name(7)
    with pytest.raises(ValueError):
        type_name(7)


def test_range_spec_validation():
    spec = RangeSpec(129, 5, 9)
    assert spec.length == 5
    native = spec.to_ctypes()
    assert (native.frequency, native.start, native.end) == (129, 5, 9)
    with pytest.raises(DataValidationError):
        RangeSpec(129, 9, 5)
    with pytest.raises(DataValidationError):
        RangeSpec(129, 0, MAX_OBSERVATIONS)
    with pytest.raises(DataValidationError):
        RangeSpec(129, 2**63, 2**63)
    with pytest.raises(DataValidationError):
        RangeSpec(-1, 0, 0)
    with pytest.raises(DataValidationError):
        RangeSpec(129, True, 1)


def test_check_buffer_rules():
    ok = np.zeros(3)
    assert check_buffer(ok, np.float64, 3, what="x") is ok
    with pytest.raises(DataValidationError):
        check_buffer([0.0], np.float64, 1, what="x")
    with pytest.raises(DataValidationError):
        check_buffer(ok, np.float64, 2, what="x")
    readonly = np.zeros(1)
    readonly.flags.writeable = False
    with pytest.raises(DataValidationError):
        check_buffer(readonly, np.float64, 1, what="x", writable=True)
    check_buffer(readonly, np.float64, 1, what="x")


def test_abi_inventory_is_complete():
    assert len(SIGNATURES) == 37
    assert len(GLOBALS) == 15 and set(GLOBAL_TYPES) == set(GLOBALS)
    assert PRESENCE_ONLY == ("cfmlerr",)
    assert "cfmlerr" not in SIGNATURES


def test_status_messages_include_known_codes():
    for status in (13, 18, 67, 97, 98, 513):
        assert str(status) in str(famepy.FameError(status))
    error = famepy.FameError(5, operation="cfmopdb")
    assert "cfmopdb" in str(error) and error.operation == "cfmopdb"


def test_public_surface_exports_are_importable():
    for name in famepy.__all__:
        assert getattr(famepy, name) is not None
    assert famepy.__version__ == "0.0.2.dev0"
