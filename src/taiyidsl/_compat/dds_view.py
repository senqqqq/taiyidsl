"""Compatibility alias for :mod:`taiyidsl.reporting.dds_view`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.reporting import dds_view as _implementation

_sys.modules[__name__] = _implementation
