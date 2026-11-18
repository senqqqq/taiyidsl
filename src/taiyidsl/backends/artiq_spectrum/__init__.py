"""Hybrid backend namespace; importing data tools does not load the compiler."""

from importlib import import_module

__all__ = [
    "ArtiqSpectrumBackend",
    "native_event",
    "native_factor",
    "native_mapping",
    "native_setup",
]


def __getattr__(name):
    if name in {"native_factor", "_exact_integer"}:
        return getattr(import_module("taiyidsl.compiler.timebase"), name)
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(".backend", __name__), name)
