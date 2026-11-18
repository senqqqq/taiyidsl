"""Compatibility alias for :mod:`taiyidsl.config.parameters`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.config import parameters as _implementation

_sys.modules[__name__] = _implementation
