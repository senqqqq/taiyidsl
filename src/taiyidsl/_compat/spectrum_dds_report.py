"""Compatibility alias for :mod:`taiyidsl.reporting.spectrum_dds_report`.

Both paths resolve to one module so existing monkeypatches keep their meaning.
"""

import sys as _sys

from taiyidsl.reporting import spectrum_dds_report as _implementation

_sys.modules[__name__] = _implementation
