"""Compatibility alias for :mod:`taiyidsl.results.imaging`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from .results import imaging as _implementation

_sys.modules[__name__] = _implementation
