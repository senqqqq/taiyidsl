"""Compatibility access to :mod:`taiyidsl.config.transport`."""

from importlib import import_module

_PUBLIC_NAMES = [
    "IZ",
    "SiteRef",
    "MoveProfile",
    "TransportConfig",
    "normalize_transport",
    "SpatialState",
]
__all__ = list(_PUBLIC_NAMES)
_SPECIAL_OWNERS = {
    "SpatialState": "compiler.spatial",
    "_distance": "compiler.spatial",
    "_layout": "compiler.spatial",
}


def __getattr__(name):
    owner = _SPECIAL_OWNERS.get(name, "config.transport")
    return getattr(import_module(f"taiyidsl.{owner}"), name)


def __dir__():
    return sorted(set(globals()) | set(_PUBLIC_NAMES))
