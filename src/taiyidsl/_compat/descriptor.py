"""Compatibility alias for :mod:`taiyidsl.config.descriptor`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.config import descriptor as _implementation

_sys.modules[__name__] = _implementation
