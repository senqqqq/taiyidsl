"""Compatibility alias for :mod:`taiyidsl.config.optics`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from .config import optics as _implementation

_sys.modules[__name__] = _implementation
