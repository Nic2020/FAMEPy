# SPDX-License-Identifier: MIT
"""FAME CHLI bindings with TimeSeriesEconPy integration, spelled as FAME.jl spells them.

Import never loads CHLI. The public names are the reference's:

* runtime: ``init_chli()``, ``close_chli()``, ``version()``, ``check_status()``
  and ``HLIError`` (one initialization per process; finalization is terminal);
* databases: ``FameDatabase``, ``opendb()``, ``workdb()``, ``postdb()``,
  ``closedb()``;
* objects: ``FameObject``, ``quick_info()``, ``listdb()``, ``do_read()``,
  ``do_write()``;
* commands: ``fame()``;
* the TimeSeriesEconPy bridge: ``refame()``, ``unfame()``, ``readfame()``,
  ``writefame()``; carriers, policies and report variants live in
  ``famepy.bridge``.

Names the reference ends with ``!`` lose the mark (``closedb``, ``do_read``);
``class`` is spelled ``class_`` where it is a keyword; do-block forms are
context managers. Everything else here (the code tables, ``Session``,
``delete_object``, the missing-value helpers, ``diagnose``, ``Period``
conversions, ``famepy.migration``, ``famepy.validation``,
``famepy.benchmarks``) is an extension without a reference spelling.
"""

from ._command import expand_input, fame
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
    classify_by_sentinel,
    delete_object,
    do_read,
    do_write,
    missing_type,
    namelist_members,
)
from ._database import FameDatabase, closedb, opendb, postdb, workdb
from ._errors import (
    CommandError,
    DataValidationError,
    HLIError,
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
from ._native import FameRange, Sentinels
from ._objects import FameObject, Period, index_to_period, period_to_index, quick_info
from ._runtime import (
    ExtendedErrorRetrieval,
    Session,
    close_chli,
    current_session,
    default_session,
    init_chli,
    reset,
    version,
)
from ._text import TextEncodingError, from_native, to_native
from ._wildcard import is_wildcard, listdb
from .bridge._values import refame, unfame
from .bridge._workspace import readfame, writefame
from .diagnostics import diagnose

__version__ = "0.1.0rc2"
__all__ = [
    # Reference exports.
    "FameDatabase",
    "FameObject",
    "check_status",
    "closedb",
    "do_read",
    "do_write",
    "fame",
    "listdb",
    "opendb",
    "postdb",
    "quick_info",
    "readfame",
    "refame",
    "unfame",
    "version",
    "workdb",
    "writefame",
    # Reference qualified names.
    "FameRange",
    "HLIError",
    "Period",
    "close_chli",
    "init_chli",
    # Extensions.
    "FREQUENCIES",
    "AccessMode",
    "Basis",
    "CommandError",
    "DataValidationError",
    "ExtendedErrorRetrieval",
    "IncludeError",
    "InheritedRuntimeError",
    "LibraryLoadError",
    "LibraryNotFoundError",
    "LicensingConfigurationError",
    "NameTruncatedError",
    "ObjectClass",
    "ObjectType",
    "Observed",
    "RuntimeStateError",
    "Sentinels",
    "Session",
    "StaleHandleError",
    "SymbolNotFoundError",
    "TextEncodingError",
    "UnsupportedOperationError",
    "UnsupportedPlatformError",
    "classify_by_sentinel",
    "current_session",
    "default_session",
    "delete_object",
    "diagnose",
    "expand_input",
    "frequency_code",
    "frequency_name",
    "from_native",
    "index_to_period",
    "is_wildcard",
    "missing_type",
    "namelist_members",
    "period_to_index",
    "reset",
    "to_native",
]
