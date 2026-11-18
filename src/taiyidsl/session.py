"""Compatibility alias for :mod:`taiyidsl.runtime.session`."""

import sys as _sys

from .runtime import session as _implementation

_sys.modules[__name__] = _implementation
