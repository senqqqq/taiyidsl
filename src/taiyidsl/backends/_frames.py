"""Compatibility alias for :mod:`taiyidsl.backends.artiq._frames`."""

import sys
from importlib import import_module

_implementation = import_module("taiyidsl.backends.artiq._frames")
sys.modules[__name__] = _implementation
