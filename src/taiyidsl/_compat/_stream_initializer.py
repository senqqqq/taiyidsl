"""Compatibility alias for the offline streamed-source initializer."""

import sys

from taiyidsl.compiler import _stream_initializer as _implementation

sys.modules[__name__] = _implementation
