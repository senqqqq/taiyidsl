"""Compatibility alias for :mod:`taiyidsl.results.rb_results`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from .results import rb_results as _implementation

_sys.modules[__name__] = _implementation
