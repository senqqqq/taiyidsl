"""Compatibility alias for :mod:`taiyidsl.dsl.sections`."""

import sys as _sys

from .dsl import sections as _implementation

_sys.modules[__name__] = _implementation
