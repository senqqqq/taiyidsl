"""Compatibility alias for :mod:`taiyidsl.results.rb_analysis`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.results import rb_analysis as _implementation

_sys.modules[__name__] = _implementation
