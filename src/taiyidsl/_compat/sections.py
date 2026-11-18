"""Compatibility alias for :mod:`taiyidsl.dsl.sections`."""

import sys as _sys

from taiyidsl.dsl import sections as _implementation

_sys.modules[__name__] = _implementation
