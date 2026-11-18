"""Compatibility alias for :mod:`taiyidsl.reporting.paged_output`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from .reporting import paged_output as _implementation

_sys.modules[__name__] = _implementation
