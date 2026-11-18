"""Compatibility alias for :mod:`taiyidsl.runtime.spectrum.awg`."""

import sys

from taiyidsl.runtime.spectrum import awg as _implementation

sys.modules[__name__] = _implementation
