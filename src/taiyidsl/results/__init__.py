"""Collected observations, image processing, archives, and offline analysis.

This package does not trigger hardware, retry shots, or manage a run journal.
Imports stay lazy so reading scalar results does not load image or fit tooling.
"""

from importlib import import_module

__all__ = ["ResultSet", "ImageResults", "synthesize", "STATUSES"]

_EXPORTS = {
    "ResultSet": ("scalar", "ResultSet"),
    "ImageResults": ("imaging", "ImageResults"),
    "synthesize": ("synthetic", "synthesize"),
    "STATUSES": ("scalar", "STATUSES"),
    # Historical private imports are read-through compatibility only. New
    # internal consumers use the owning module instead of the package facade.
    "_jsonable": ("archives", "_jsonable"),
    "_atomic_hdf5": ("archives", "_atomic_hdf5"),
    "_probability": ("synthetic", "_probability"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(f".{module}", __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | _EXPORTS.keys())
