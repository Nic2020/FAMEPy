# SPDX-License-Identifier: MIT AND BSD-3-Clause
# Conversion semantics adapted from FAME.jl (Bridge.jl); see licenses/FAME.jl.txt.
# Copyright (c) 2020-2024, Bank of Canada. All rights reserved.
"""TimeSeriesEconPy bridge: the carriers, policies and helpers behind the conversions.

The reference's bridge functions are the package-level ``refame``,
``unfame``, ``readfame`` and ``writefame`` (see ``famepy``). This namespace
holds what surrounds them:

* frequencies (``fame_frequency``, ``tsecon_frequency``, ``mit_to_index``,
  ``index_to_mit``, ``mit_to_year_period``, ``year_period_to_mit``): the
  supported calendar set and index conversions through the library's own
  year/period functions;
* the value carriers ``NameList``, ``Text``, ``DateSeries`` and
  ``StringSeries`` with the ``missing``, ``empty`` and ``text`` policies
  (the text policy selects the encoding of string values in both directions:
  ``ascii`` by default, ``bytes``, or strict ``utf-8``) and the errors
  ``MissingValueError``, ``EmptySeriesError``, ``UnsupportedFrequencyError``;
* the workspace helpers: ``readfame_report`` and ``writefame_report`` (the
  reference's per-object error logging as records), ``ReadReport``,
  ``WriteReport``, ``ObjectFailure``, ``NameCollisionError``,
  ``WorkspaceCycleError``, ``resolve_names`` and ``flatten_names``.

FAMEPy depends on TimeSeriesEconPy through its public API only; the
dependency never runs the other way. Path targets validate everything that
does not need the database (types, frequencies, dtypes, exact integer
conversion, policies, object names) before the database file is opened, so
an invalid input never truncates or creates a file; they post after success
and always close. Handle targets never post.
"""

from __future__ import annotations

from ._frequencies import (
    SUPPORTED_FREQUENCY_CODES,
    UnsupportedFrequencyError,
    fame_frequency,
    index_to_mit,
    is_supported_frequency,
    mit_to_index,
    mit_to_year_period,
    owner_session,
    tsecon_frequency,
    year_period_to_mit,
)
from ._values import (
    EMPTY_POLICIES,
    MISSING_POLICIES,
    TEXT_POLICIES,
    DateSeries,
    EmptySeriesError,
    MissingValueError,
    NameList,
    StringSeries,
    Text,
    check_policies,
    validate_value,
)
from ._workspace import (
    NameCollisionError,
    ObjectFailure,
    ReadReport,
    WorkspaceCycleError,
    WriteReport,
    flatten_names,
    readfame_report,
    resolve_names,
    writefame_report,
)

__all__ = [
    "EMPTY_POLICIES",
    "MISSING_POLICIES",
    "SUPPORTED_FREQUENCY_CODES",
    "TEXT_POLICIES",
    "DateSeries",
    "EmptySeriesError",
    "MissingValueError",
    "NameCollisionError",
    "NameList",
    "ObjectFailure",
    "ReadReport",
    "StringSeries",
    "Text",
    "UnsupportedFrequencyError",
    "WorkspaceCycleError",
    "WriteReport",
    "check_policies",
    "fame_frequency",
    "flatten_names",
    "index_to_mit",
    "is_supported_frequency",
    "mit_to_index",
    "mit_to_year_period",
    "owner_session",
    "readfame_report",
    "resolve_names",
    "tsecon_frequency",
    "validate_value",
    "writefame_report",
    "year_period_to_mit",
]
