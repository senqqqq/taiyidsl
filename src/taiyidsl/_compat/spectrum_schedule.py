"""Compatibility alias for taiyidsl.backends.artiq_spectrum.scheduling."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("taiyidsl.backends.artiq_spectrum.scheduling")
