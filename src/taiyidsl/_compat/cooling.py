"""Compatibility alias for :mod:`taiyidsl.experiments.cooling`.

Both paths share one module, including private helpers and existing patches.
"""

import sys as _sys

from taiyidsl.experiments import cooling as _implementation

_sys.modules[__name__] = _implementation
