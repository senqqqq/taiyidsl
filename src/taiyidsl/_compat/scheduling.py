"""Compatibility alias; new code imports :mod:`taiyidsl.compiler.scheduling`."""

import sys

from taiyidsl.compiler import scheduling as _implementation

sys.modules[__name__] = _implementation
