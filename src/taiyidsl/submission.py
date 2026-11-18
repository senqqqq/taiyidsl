"""Compatibility alias for :mod:`taiyidsl.backends.artiq.submission`."""

import sys
from importlib import import_module

_implementation = import_module("taiyidsl.backends.artiq.submission")
sys.modules[__name__] = _implementation
