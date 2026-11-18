"""Compatibility alias for :mod:`taiyidsl.runtime.spectrum.dds`."""

import sys

from .runtime.spectrum import dds as _implementation

sys.modules[__name__] = _implementation
