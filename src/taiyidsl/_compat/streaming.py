"""Compatibility alias; new code imports :mod:`taiyidsl.compiler.streaming`."""

import sys

from taiyidsl.compiler import streaming as _implementation

sys.modules[__name__] = _implementation
