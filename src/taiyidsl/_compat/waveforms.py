"""Compatibility alias for :mod:`taiyidsl.backends.artiq.waveforms`."""

import sys
from importlib import import_module

_implementation = import_module("taiyidsl.backends.artiq.waveforms")
sys.modules[__name__] = _implementation
