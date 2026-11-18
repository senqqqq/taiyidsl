"""Compatibility alias for :mod:`taiyidsl.reporting.output_simulator`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.reporting import output_simulator as _implementation

_sys.modules[__name__] = _implementation
