"""Compatibility alias for :mod:`taiyidsl.backends.artiq._ram_boundary`."""

import sys
from importlib import import_module

_implementation = import_module("taiyidsl.backends.artiq._ram_boundary")
sys.modules[__name__] = _implementation
