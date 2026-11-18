"""Compatibility module alias for :mod:`taiyidsl.runtime.streaming`."""

import sys

from taiyidsl.runtime import streaming as _implementation

sys.modules[__name__] = _implementation
