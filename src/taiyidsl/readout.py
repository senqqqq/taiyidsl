"""Compatibility module alias for :mod:`taiyidsl.runtime.readout`."""

import sys

from .runtime import readout as _implementation

sys.modules[__name__] = _implementation
