"""Compatibility alias; new code imports :mod:`taiyidsl.compiler.shared_fields`."""

import sys

from taiyidsl.compiler import shared_fields as _implementation

sys.modules[__name__] = _implementation
