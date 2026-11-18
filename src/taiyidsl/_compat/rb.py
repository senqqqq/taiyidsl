"""Compatibility alias for :mod:`taiyidsl.experiments.rb_sequences`.

Both paths share one module, including private helpers and existing patches.
"""

import sys as _sys

from taiyidsl.experiments import rb_sequences as _implementation

_sys.modules[__name__] = _implementation
