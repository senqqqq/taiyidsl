"""Compatibility alias for :mod:`taiyidsl.dsl.authoring`."""

import sys as _sys

from taiyidsl.dsl import authoring as _implementation

_sys.modules[__name__] = _implementation
