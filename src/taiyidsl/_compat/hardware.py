"""Compatibility access to :mod:`taiyidsl.config.hardware`."""

from importlib import import_module

_PUBLIC_NAMES = [
    "Device",
    "DeviceCalibration",
    "ControlBinding",
    "ControlRecipe",
    "TriggerStep",
    "TriggerSequence",
    "Protocol",
    "ProtocolStep",
    "DeviceSetup",
    "SignalMap",
    "reference_setup",
]
__all__ = list(_PUBLIC_NAMES)
_SPECIAL_OWNERS = {"reference_setup": "lab"}


def __getattr__(name):
    owner = _SPECIAL_OWNERS.get(name, "config.hardware")
    return getattr(import_module(f"taiyidsl.{owner}"), name)


def __dir__():
    return sorted(set(globals()) | set(_PUBLIC_NAMES))
