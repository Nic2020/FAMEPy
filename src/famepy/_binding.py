# SPDX-License-Identifier: MIT
"""Internal explicit prototype binding, also used with the independent C shim."""

from __future__ import annotations

import ctypes as ct
from typing import Any

from ._abi import INT32, PI, SIGNATURES
from ._errors import SymbolNotFoundError, check_status


class Binding:
    """Resolve declared prototypes lazily and normalize the two status conventions."""

    def __init__(self, library: Any) -> None:
        self._library = library
        self._functions: dict[str, Any] = {}

    def has_symbol(self, name: str) -> bool:
        return hasattr(self._library, name)

    def _resolve(self, name: str) -> Any:
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
        return function

    def call_status(self, name: str, *args: Any) -> int:
        """Call and return the raw status without raising."""
        spec = SIGNATURES[name]
        function = self._resolve(name)
        if spec.convention == "cfm":
            status = INT32(-1)
            function(ct.byref(status), *args)
            return int(status.value)
        return int(function(*args))

    def call(self, name: str, *args: Any) -> None:
        check_status(self.call_status(name, *args), operation=name)
