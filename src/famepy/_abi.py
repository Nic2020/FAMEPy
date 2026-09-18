# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Declarations adapted from FAME.jl, not verified vendor headers.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
# See licenses/FAME.jl.txt.
"""Candidate ABI. Do not treat this table as vendor ABI certification."""

import ctypes as ct
from dataclasses import dataclass
from typing import Any

INT32 = ct.c_int32
J = ct.c_int64
PI = ct.POINTER(INT32)
PJ = ct.POINTER(J)
S = ct.c_char_p
C = ct.POINTER(ct.c_char)


class FameRange(ct.Structure):
    _fields_ = [("frequency", INT32), ("start", J), ("end", J)]


R = ct.POINTER(FameRange)


@dataclass(frozen=True)
class Signature:
    convention: str
    arguments: tuple[Any, ...]


# cfm functions have an additional leading status pointer and return void.
# fame functions return their status. Text the library may modify (``C``)
# always travels in an owned writable buffer; ``S`` is input-only text.
SIGNATURES: dict[str, Signature] = {
    "cfmini": Signature("cfm", ()),
    "cfmfin": Signature("cfm", ()),
    "cfmver": Signature("cfm", (ct.POINTER(ct.c_float),)),
    "cfmferr": Signature("cfm", (C,)),
    "cfmfame": Signature("cfm", (S,)),
    "cfmopwk": Signature("cfm", (PI,)),
    "cfmopdb": Signature("cfm", (PI, C, INT32)),
    "cfmpodb": Signature("cfm", (INT32,)),
    "cfmcldb": Signature("cfm", (INT32,)),
    "cfmsopt": Signature("cfm", (C, C)),
    "cfmnlen": Signature("cfm", (INT32, C, INT32, PI)),
    "cfmgtnl": Signature("cfm", (INT32, C, INT32, C, INT32, PI)),
    "cfmwtnl": Signature("cfm", (INT32, C, INT32, C)),
    "cfmdlob": Signature("cfm", (INT32, C)),
    "cfmnwob": Signature("cfm", (INT32, C, INT32, INT32, INT32, INT32, INT32)),
    "cfmispm": Signature("cfm", (ct.c_double, PI)),
    "cfmisnm": Signature("cfm", (ct.c_float, PI)),
    "cfmisbm": Signature("cfm", (INT32, PI)),
    "cfmissm": Signature("cfm", (S, PI)),
    "fame_index_to_year_period": Signature("fame", (INT32, J, PI, PI)),
    # Uses the Bridge.jl 64-bit candidate; Objects.jl differs. Confirm with headers.
    "fame_year_period_to_index": Signature("fame", (INT32, PJ, INT32, INT32)),
    "fame_quick_info": Signature("fame", (INT32, S, PI, PI, PI, PJ, PJ)),
    "fame_init_wildcard": Signature("fame", (INT32, PI, S, INT32, S)),
    "fame_get_next_wildcard": Signature("fame", (INT32, C, PI, PI, PI, PJ, PJ, INT32, PI)),
    "fame_free_wildcard": Signature("fame", (INT32,)),
    "fame_get_precisions": Signature("fame", (INT32, S, R, ct.POINTER(ct.c_double))),
    "fame_get_numerics": Signature("fame", (INT32, S, R, ct.POINTER(ct.c_float))),
    "fame_get_booleans": Signature("fame", (INT32, S, R, PI)),
    "fame_get_dates": Signature("fame", (INT32, S, R, PJ)),
    "fame_len_strings": Signature("fame", (INT32, S, R, PI)),
    "fame_get_strings": Signature("fame", (INT32, S, R, ct.POINTER(C), PI, PI)),
    "fame_write_precisions": Signature("fame", (INT32, S, R, ct.POINTER(ct.c_double))),
    "fame_write_numerics": Signature("fame", (INT32, S, R, ct.POINTER(ct.c_float))),
    "fame_write_booleans": Signature("fame", (INT32, S, R, PI)),
    "fame_write_dates": Signature("fame", (INT32, S, R, INT32, PJ)),
    "fame_write_strings": Signature("fame", (INT32, S, R, ct.POINTER(S))),
    "fame_date_missing_type": Signature("fame", (J, PI)),
}

# Text arguments the older calling convention documents as both input and
# output (the library trims and upper-cases them in place), by position after
# the status pointer. The binding never passes immutable Python bytes there:
# each gets an owned NUL-terminated copy that lives for the call. Every other
# text argument is documented as input only (the newer convention declares
# its inputs const).
WRITABLE_TEXT: dict[str, tuple[int, ...]] = {
    "cfmopdb": (1,),
    "cfmsopt": (0, 1),
    "cfmnlen": (1,),
    "cfmgtnl": (1,),
    "cfmwtnl": (1, 3),
    "cfmdlob": (1,),
    "cfmnwob": (1,),
}

# Declared in the installed headers on both inspected installations, needed to
# size extended-error buffers, but its exact signature is not yet established.
# Presence is probed; no call is made until a verified declaration exists.
PRESENCE_ONLY = ("cfmlerr",)

# Candidate declared C types of the native globals (reference usage). Numeric
# globals are read after initialization; string globals are three-byte arrays.
GLOBAL_TYPES: dict[str, Any] = {
    "FAME_INDEX_NC": J,
    "FAME_INDEX_NA": J,
    "FAME_INDEX_ND": J,
    "FPRCNC": ct.c_double,
    "FPRCNA": ct.c_double,
    "FPRCND": ct.c_double,
    "FNUMNC": ct.c_float,
    "FNUMNA": ct.c_float,
    "FNUMND": ct.c_float,
    "FBOONC": INT32,
    "FBOONA": INT32,
    "FBOOND": INT32,
    "FSTRNC": ct.c_char * 3,
    "FSTRNA": ct.c_char * 3,
    "FSTRND": ct.c_char * 3,
}

GLOBALS = tuple(
    [f"FAME_INDEX_{kind}" for kind in ("NC", "NA", "ND")]
    + [
        f"{prefix}{kind}"
        for prefix in ("FPRC", "FNUM", "FBOO", "FSTR")
        for kind in ("NC", "NA", "ND")
    ]
)


def layout() -> dict[str, int]:
    """Describe Python's candidate layout without loading native code."""
    return {
        "int_bytes": ct.sizeof(INT32),
        "index_bytes": ct.sizeof(J),
        "range_bytes": ct.sizeof(FameRange),
        "range_alignment": ct.alignment(FameRange),
        "frequency_offset": FameRange.frequency.offset,
        "start_offset": FameRange.start.offset,
        "end_offset": FameRange.end.offset,
    }
