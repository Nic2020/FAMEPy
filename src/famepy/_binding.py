# SPDX-License-Identifier: MIT
"""Internal explicit prototype binding, also used with the independent C shim."""

import ctypes as ct
from typing import Any

from ._abi import INT32, PI, SIGNATURES
from ._errors import SymbolNotFoundError, check_status


class Binding:
    def __init__(self, library: Any) -> None:
        self._library = library
        self._functions: dict[str, Any] = {}

    def call(self, name: str, *args: Any) -> None:
        spec = SIGNATURES[name]
        function = self._functions.get(name)
        if function is None:
            try:
                function = getattr(self._library, name)
            except AttributeError:
                raise SymbolNotFoundError(name) from None
            function.argtypes = ([PI] if spec.convention == "cfm" else []) + list(spec.arguments)
            function.restype = None if spec.convention == "cfm" else INT32
            self._functions[name] = function
        if spec.convention == "cfm":
            status = INT32(-1)
            function(ct.byref(status), *args)
            check_status(status.value)
        else:
            check_status(function(*args))
