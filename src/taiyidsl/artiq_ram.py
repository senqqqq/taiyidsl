"""Compatibility alias for :mod:`taiyidsl.backends.artiq.codegen_ram`."""

import sys
from importlib import import_module

_implementation = import_module("taiyidsl.backends.artiq.codegen_ram")
sys.modules[__name__] = _implementation
