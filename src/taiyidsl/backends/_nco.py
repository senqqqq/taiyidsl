"""Compatibility alias for :mod:`taiyidsl.backends.artiq._nco`."""

import sys
from importlib import import_module

_implementation = import_module("taiyidsl.backends.artiq._nco")
sys.modules[__name__] = _implementation
