# SPDX-License-Identifier: MIT
"""FAME CHLI bindings and TimeSeriesEconPy integration; import never loads CHLI.

Runtime: ``initialize()``, ``finalize()``, ``version()`` (one-shot per process;
``reset()`` is unsupported and raises).
Databases: ``open_database()``, ``work_database()``, ``Database``.
Objects: ``quick_info()``, ``list_objects()``, ``read_object()``, ``write_object()``.
Commands: ``run_command()``. The TimeSeriesEconPy bridge (values, every
reference frequency anchor, workspaces) lives in ``famepy.bridge``.
"""

from ._command import expand_input, run_command
from ._constants import (
    FREQUENCIES,
    AccessMode,
    Basis,
    ObjectClass,
    ObjectType,
    Observed,
    frequency_code,
    frequency_name,
)
from ._data import (
    RawScalar,
    RawSeries,
    classify_by_sentinel,
    delete_object,
    missing_type,
    namelist_members,
    read_object,
    scalar,
    series,
    write_object,
)
from ._database import Database, open_database, work_database
from ._errors import (
    CommandError,
    DataValidationError,
    FameError,
    IncludeError,
    InheritedRuntimeError,
    LibraryLoadError,
    LibraryNotFoundError,
    LicensingConfigurationError,
    NameTruncatedError,
    RuntimeStateError,
    StaleHandleError,
    SymbolNotFoundError,
    UnsupportedOperationError,
    UnsupportedPlatformError,
    check_status,
)
from ._native import RangeSpec, Sentinels
from ._objects import ObjectInfo, Period, index_to_period, period_to_index, quick_info
from ._runtime import (
    ExtendedErrorRetrieval,
    Session,
    current_session,
    default_session,
    finalize,
    initialize,
    reset,
    version,
)
from ._text import TextEncodingError, from_native, to_native
from ._wildcard import is_wildcard, list_objects
from .diagnostics import diagnose

__version__ = "0.0.3.dev0"
__all__ = [
    "FREQUENCIES",
    "AccessMode",
    "Basis",
    "CommandError",
    "DataValidationError",
    "Database",
    "ExtendedErrorRetrieval",
    "FameError",
    "IncludeError",
    "InheritedRuntimeError",
    "LibraryLoadError",
    "LibraryNotFoundError",
    "LicensingConfigurationError",
    "NameTruncatedError",
    "ObjectClass",
    "ObjectInfo",
    "ObjectType",
    "Observed",
    "Period",
    "RangeSpec",
    "RawScalar",
    "RawSeries",
    "RuntimeStateError",
    "Sentinels",
    "Session",
    "StaleHandleError",
    "SymbolNotFoundError",
    "TextEncodingError",
    "UnsupportedOperationError",
    "UnsupportedPlatformError",
    "check_status",
    "classify_by_sentinel",
    "current_session",
    "default_session",
    "delete_object",
    "diagnose",
    "expand_input",
    "finalize",
    "frequency_code",
    "frequency_name",
    "from_native",
    "index_to_period",
    "initialize",
    "is_wildcard",
    "list_objects",
    "missing_type",
    "namelist_members",
    "open_database",
    "period_to_index",
    "quick_info",
    "read_object",
    "reset",
    "run_command",
    "scalar",
    "series",
    "to_native",
    "version",
    "work_database",
    "write_object",
]
