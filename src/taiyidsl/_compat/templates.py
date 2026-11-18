"""Compatibility alias for :mod:`taiyidsl.experiments.templates`.

Both paths share one module, including private helpers and existing patches.
"""

import sys as _sys

from taiyidsl.experiments import templates as _implementation

_sys.modules[__name__] = _implementation
