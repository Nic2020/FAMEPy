# SPDX-License-Identifier: MIT
import ctypes as ct
import os
from pathlib import Path

import numpy as np
import pytest

from famepy import FameError, SymbolNotFoundError, diagnose
from famepy._abi import C, FameRange, layout
from famepy._binding import Binding

pytestmark = pytest.mark.native_shim


@pytest.fixture(scope="module")
def native():
    configured = os.environ.get("FAMEPY_TEST_SHIM")
    if not configured:
        if os.environ.get("FAMEPY_REQUIRE_SHIM") == "1":
            pytest.fail("Required C shim is not configured.")
        pytest.skip("Set FAMEPY_TEST_SHIM to run independent C-library tests.")
    path = Path(configured)
    assert path.is_absolute() and path.is_file(), "Configured C shim is missing or not absolute"
    return ct.CDLL(str(path))


def test_layout_against_compiler(native):
    values = layout()
    for name, key in [
        ("shim_range_size", "range_bytes"),
        ("shim_start_offset", "start_offset"),
        ("shim_end_offset", "end_offset"),
    ]:
        function = getattr(native, name)
        function.argtypes = []
        function.restype = ct.c_int32
        assert function() == values[key]


def test_status_pointer_and_return(native):
    binding = Binding(native)
    version = ct.c_float()
    binding.call("cfmver", ct.byref(version))
    assert version.value == 4.25
    key = ct.c_int32()
    binding.call("cfmopdb", ct.byref(key), b"synthetic", 1)
    assert key.value == 42
    with pytest.raises(FameError) as error:
        binding.call("cfmopdb", ct.byref(key), b"synthetic", 2)
    assert error.value.status == 67


def test_index_output_really_64bit(native):
    class Guarded(ct.Structure):
        _fields_ = [("before", ct.c_int64), ("value", ct.c_int64), ("after", ct.c_int64)]

    output = Guarded(91, 0, 92)
    pointer = ct.cast(ct.byref(output, Guarded.value.offset), ct.POINTER(ct.c_int64))
    Binding(native).call("fame_year_period_to_index", 129, pointer, 2020, 3)
    assert output.value == (2020 << 32) + 3
    assert (output.before, output.after) == (91, 92)


def test_bulk_numpy_buffer_and_null_scalar_range(native):
    values = np.full(5, -99.0, dtype=np.float64)
    range_ = FameRange(129, 2**33, 2**33 + 2)
    binding = Binding(native)
    binding.call(
        "fame_get_precisions",
        42,
        b"synthetic",
        ct.byref(range_),
        values[1:].ctypes.data_as(ct.POINTER(ct.c_double)),
    )
    assert values.tolist() == [-99, 2**33 + 0.5, 2**33 + 1.5, 2**33 + 2.5, -99]
    scalar = ct.c_double()
    binding.call("fame_get_precisions", 42, b"synthetic", None, ct.byref(scalar))
    assert scalar.value == 12.5
    range_.end = range_.start - 1
    with pytest.raises(FameError):
        binding.call("fame_get_precisions", 42, b"synthetic", ct.byref(range_), ct.byref(scalar))


def test_writable_string_pointer_array(native):
    left, right = ct.create_string_buffer(4), ct.create_string_buffer(3)
    pointers = (C * 2)(ct.cast(left, C), ct.cast(right, C))
    lengths = (ct.c_int32 * 2)(3, 2)
    Binding(native).call("fame_get_strings", 42, b"synthetic", None, pointers, lengths, None)
    assert (left.value, right.value) == (b"abc", b"xy")
    assert list(lengths) == [3, 2]


def test_probe_finds_only_available_symbols(native):
    report = diagnose(os.environ["FAMEPY_TEST_SHIM"], probe=True)
    assert report["status"] == "symbols_missing"
    assert report["functions"]["cfmver"] is True
    assert report["functions"]["cfmini"] is False
    assert report["globals"]["FPRCNA"] is True
    assert report["globals"]["FSTRNA"] is True
    assert report["globals"]["FSTRNC"] is True
    assert report["globals"]["FSTRND"] is False
    assert report["abi_verified"] is False
    assert report["native_calls_executed"] is False


def test_missing_function_is_package_error(native):
    with pytest.raises(SymbolNotFoundError) as error:
        Binding(native).call("cfmini")
    assert error.value.symbol == "cfmini"
    assert os.environ["FAMEPY_TEST_SHIM"] not in str(error.value)
    assert error.value.__suppress_context__
