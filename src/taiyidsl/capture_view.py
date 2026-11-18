"""Compatibility alias for :mod:`taiyidsl.reporting.capture_view`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from .reporting import capture_view as _implementation

_sys.modules[__name__] = _implementation
