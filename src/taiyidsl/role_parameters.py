"""Compatibility alias for :mod:`taiyidsl.config.role_parameters`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from .config import role_parameters as _implementation

_sys.modules[__name__] = _implementation
