"""Compatibility alias for taiyidsl.backends.artiq_spectrum.banks."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("taiyidsl.backends.artiq_spectrum.banks")
