"""Compatibility alias for :mod:`taiyidsl.reporting.experiment_report`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.reporting import experiment_report as _implementation

_sys.modules[__name__] = _implementation
