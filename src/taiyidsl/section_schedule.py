"""Compatibility alias; new code imports :mod:`taiyidsl.compiler.section_schedule`."""

import sys

from .compiler import section_schedule as _implementation

sys.modules[__name__] = _implementation
