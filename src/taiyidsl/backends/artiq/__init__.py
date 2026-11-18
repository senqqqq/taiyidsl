"""ARTIQ target entry, without eagerly loading code generation or mock drivers.

New library code imports the owning module under taiyidsl.backends.artiq.
"""

from importlib import import_module

_OWNERS = {
    "ArtiqBackend": "taiyidsl.backends.artiq.backend",
    "quantize_ad9910": "taiyidsl.backends.artiq.backend",
    "_overlap": "taiyidsl.backends.artiq.backend",
    "_check_intervals": "taiyidsl.backends.artiq.backend",
    "_prepare_ram": "taiyidsl.backends.artiq.backend",
    "_lower": "taiyidsl.backends.artiq.backend",
    "ControlBinding": "taiyidsl.config.hardware",
    "DeviceCalibration": "taiyidsl.config.hardware",
    "CompileError": "taiyidsl.ir",
    "PhysicalPlan": "taiyidsl.ir",
    "ScheduledEvent": "taiyidsl.ir",
    "validate_submission_plan": "taiyidsl.backends.artiq.submission",
    "ad9910_amplitude_ram": "taiyidsl.backends.artiq.waveforms",
    "hardware_frame_events": "taiyidsl.backends.artiq._frames",
    "finalize_nco_epochs": "taiyidsl.backends.artiq._nco",
    "ConfigurationRequest": "taiyidsl.backends.artiq._preparation",
    "place_configurations": "taiyidsl.backends.artiq._preparation",
    "lower_ram_boundary": "taiyidsl.backends.artiq._ram_boundary",
    "lower_ram_polar": "taiyidsl.backends.artiq._ram_polar",
    "lower_square": "taiyidsl.backends.artiq._square",
    "Backend": "taiyidsl.backends.base",
    "BackendCapabilities": "taiyidsl.backends.base",
}
_PUBLIC_NAMES = [
    "ArtiqBackend",
    "Backend",
    "BackendCapabilities",
    "CompileError",
    "ConfigurationRequest",
    "ControlBinding",
    "DeviceCalibration",
    "PhysicalPlan",
    "ScheduledEvent",
    "ad9910_amplitude_ram",
    "finalize_nco_epochs",
    "hardware_frame_events",
    "lower_ram_boundary",
    "lower_ram_polar",
    "lower_square",
    "place_configurations",
    "quantize_ad9910",
    "validate_submission_plan",
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
