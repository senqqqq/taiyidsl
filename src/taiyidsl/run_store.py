"""Compatibility module alias for :mod:`taiyidsl.runtime.run_store`."""

import sys

from .runtime import run_store as _implementation

sys.modules[__name__] = _implementation
