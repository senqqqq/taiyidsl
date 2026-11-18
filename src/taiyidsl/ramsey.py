"""Compatibility alias for :mod:`taiyidsl.experiments.ramsey`.

Both paths share one module, including private helpers and existing patches.
"""

import sys as _sys

from .experiments import ramsey as _implementation

_sys.modules[__name__] = _implementation
