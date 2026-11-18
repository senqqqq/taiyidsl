"""Compatibility alias for :mod:`taiyidsl.backends.artiq.validation_polar_output`."""

import sys
from importlib import import_module

_implementation = import_module("taiyidsl.backends.artiq.validation_polar_output")
sys.modules[__name__] = _implementation
