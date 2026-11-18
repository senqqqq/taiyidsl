"""Compatibility alias for :mod:`taiyidsl.config.pair_calibration`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.config import pair_calibration as _implementation

_sys.modules[__name__] = _implementation
