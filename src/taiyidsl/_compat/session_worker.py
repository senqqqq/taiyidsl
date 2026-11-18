"""Compatibility alias and command entry for :mod:`taiyidsl.runtime.worker`."""

import sys as _sys

from taiyidsl.runtime import worker as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    _sys.modules[__name__] = _implementation
