"""Compatibility alias for :mod:`taiyidsl.dsl.operations`."""

import sys as _sys

from taiyidsl.dsl import operations as _implementation

_sys.modules[__name__] = _implementation
