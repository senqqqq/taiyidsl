"""Compatibility exports for the canonical ARTIQ implementation package.

New library code imports the owning module under taiyidsl.backends.artiq.
"""

from importlib import import_module

_OWNERS = {
    "square_preload_lines": "taiyidsl.backends.artiq.codegen_square",
    "nco_release_stage_lines": "taiyidsl.backends.artiq.codegen_square",
    "square_effect_record": "taiyidsl.backends.artiq._contracts",
}
_PUBLIC_NAMES = [
    "nco_release_stage_lines",
    "square_effect_record",
    "square_preload_lines",
]
__all__ = list(_PUBLIC_NAMES)


def __getattr__(name):
    try:
        owner = _OWNERS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(import_module(owner), name)


def __dir__():
    return sorted(set(globals()) | set(_OWNERS))
