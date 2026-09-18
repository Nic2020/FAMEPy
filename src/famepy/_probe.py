# SPDX-License-Identifier: MIT
"""Internal child protocol. No exception text crosses this boundary."""

import ctypes as ct
import json
import sys
from typing import Any

from ._abi import GLOBALS, PRESENCE_ONLY, SIGNATURES
from ._discovery import discover
from ._errors import LibraryLoadError, LibraryNotFoundError, UnsupportedPlatformError
from ._runtime import Runtime

# The bootstrap can report import failure even when famepy.__init__ cannot run.
BOOTSTRAP = """import sys
try:
    from famepy._probe import main
except Exception:
    sys.exit(20)
sys.exit(main())
"""

FAILURE_KINDS = {
    20: "package_import_failed",
    21: "invalid_request",
    22: "discovery_failed",
    23: "library_load_failed",
    24: "child_exception",
    25: "child_setup_failed",
}


def failure_kind(code: int, system: str) -> str:
    if code in FAILURE_KINDS:
        return FAILURE_KINDS[code]
    if system == "win32" and (code & 0xFFFFFFFF) >= 0xC0000000:
        return "windows_exception_exit"
    if system != "win32" and code < 0:
        return "signal_exit"
    return "unclassified_exit"


def load_error_class(errno: int | None, winerror: int | None) -> str:
    # Numeric hints are deliberately conservative. In particular, POSIX dlopen
    # often supplies neither code. Do not parse its path-bearing message.
    if winerror in (2, 3, 126):
        return "library_or_dependency_not_found"
    if winerror in (193, 216):
        return "bad_image"
    if winerror == 1114:
        return "initialization_failed"
    if winerror == 5 or (winerror is None and errno in (1, 13)):
        return "access_denied"
    if winerror is None and errno == 2:
        return "library_or_dependency_not_found"
    return "other"


def suppress_error_dialogs() -> None:
    """Set child-only Windows error mode; preserve inherited mode bits."""
    if sys.platform == "win32":
        kernel = ct.WinDLL("kernel32", use_last_error=True)
        get_mode = kernel.GetErrorMode
        get_mode.argtypes = []
        get_mode.restype = ct.c_uint
        set_mode = kernel.SetErrorMode
        set_mode.argtypes = [ct.c_uint]
        set_mode.restype = ct.c_uint
        set_mode(get_mode() | 0x8003)


def probe_library(library: str, root: str | None = None) -> dict[str, Any]:
    owner = Runtime(discover(library, root=root))
    native = owner.load()
    functions = {name: hasattr(native, name) for name in SIGNATURES}
    # Presence-only symbols are reported separately; no declaration is attached.
    presence = {name: hasattr(native, name) for name in PRESENCE_ONLY}
    globals_found = {}
    for name in GLOBALS:
        try:
            # Address existence only. Never dereference a vendor string pointer.
            ct.c_byte.in_dll(native, name)
            globals_found[name] = True
        except ValueError:
            globals_found[name] = False
    return {"functions": functions, "globals": globals_found, "presence_only": presence}


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or not isinstance(request.get("library"), str):
            return 21
        root = request.get("root")
        if root is not None and not isinstance(root, str):
            return 21
    except (ValueError, OSError):
        return 21
    try:
        suppress_error_dialogs()
    except Exception:
        return 25
    try:
        result = probe_library(request["library"], root)
    except LibraryLoadError as error:
        print(json.dumps({"errno": error.errno, "winerror": error.winerror}))
        return 23
    except (LibraryNotFoundError, UnsupportedPlatformError):
        return 22
    except Exception:
        return 24
    print(json.dumps(result, sort_keys=True))
    return 0
