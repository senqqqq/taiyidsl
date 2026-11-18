"""Compatibility alias for :mod:`taiyidsl.reporting.viewer_execution`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.reporting import viewer_execution as _implementation

_sys.modules[__name__] = _implementation
