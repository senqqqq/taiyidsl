"""Compatibility access to :mod:`taiyidsl.config.spectrum`."""

from importlib import import_module

_PUBLIC_NAMES = [
    "SAMPLE_RATE_HZ",
    "HYBRID_TICK_SECONDS",
    "SpectrumCard",
    "SpectrumOutput",
    "SpectrumGroup",
    "SpectrumConfig",
    "resolve_trigger_signal",
]
__all__ = list(_PUBLIC_NAMES)
_SPECIAL_OWNERS = {"resolve_trigger_signal": "compiler.trigger_routes"}


def __getattr__(name):
    owner = _SPECIAL_OWNERS.get(name, "config.spectrum")
    return getattr(import_module(f"taiyidsl.{owner}"), name)


def __dir__():
    return sorted(set(globals()) | set(_PUBLIC_NAMES))
