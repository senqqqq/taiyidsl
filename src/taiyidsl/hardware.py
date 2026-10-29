"""Explicit device contracts. Reference values are assumptions, not measurements."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Device:
    name: str
    kind: str
    config_duration_mu: int = 1000
    grid_mu: int = 4
    min_duration_mu: int = 4
    bus: str | None = None
    update_group: str | None = None
    sysclk_hz: float = 1e9
    latency_mu: int = 0
    min_frequency: float = 0
    max_frequency: float = 400e6
    config_grid_mu: int = 8

    def __post_init__(self):
        if not self.name.isidentifier() or self.kind not in {"dds", "ttl"}:
            raise ValueError("Device needs an identifier name and kind dds or ttl")
        for name in (
            "grid_mu",
            "min_duration_mu",
            "config_duration_mu",
            "config_grid_mu",
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.latency_mu, int) or self.latency_mu < 0:
            raise ValueError("latency_mu must be a nonnegative integer")
        if self.config_duration_mu % self.grid_mu:
            raise ValueError("Configuration duration must be a multiple of device grid")
        if self.kind == "dds" and self.config_duration_mu % self.config_grid_mu:
            raise ValueError(
                "DDS configuration duration must respect configuration grid"
            )
        if not math.isfinite(self.sysclk_hz) or self.sysclk_hz <= 0:
            raise ValueError("sysclk_hz must be positive and finite")
        if not (0 <= self.min_frequency < self.max_frequency <= self.sysclk_hz / 2):
            raise ValueError("Frequency limits must lie within Nyquist range")


@dataclass(frozen=True)
class DeviceCalibration:
    latency_mu: int = 0
    phase_offset: float = 0
    amplitude_scale: float = 1
    frequency_offset: float = 0
    revision: str = "unmeasured"

    def __post_init__(self):
        if (
            isinstance(self.latency_mu, bool)
            or not isinstance(self.latency_mu, int)
            or self.latency_mu < 0
        ):
            raise ValueError("Calibration latency must be nonnegative integer mu")
        for value in (self.phase_offset, self.amplitude_scale, self.frequency_offset):
            if not math.isfinite(value):
                raise ValueError("Calibration values must be finite")
        if self.amplitude_scale <= 0:
            raise ValueError("Amplitude scale must be positive")


@dataclass(frozen=True)
class ControlBinding:
    device: str
    frequency_scale: float = 1
    frequency_offset: float = 0
    phase_scale: float = 1
    phase_offset: float = 0
    amplitude_scale: float = 1

    def __post_init__(self):
        for key in (
            "frequency_scale",
            "frequency_offset",
            "phase_scale",
            "phase_offset",
            "amplitude_scale",
        ):
            if not math.isfinite(getattr(self, key)):
                raise ValueError("Control transforms must be finite")


@dataclass(frozen=True)
class ControlRecipe:
    outputs: tuple[ControlBinding, ...]

    def __post_init__(self):
        object.__setattr__(self, "outputs", tuple(self.outputs))
        if not self.outputs or len({b.device for b in self.outputs}) != len(
            self.outputs
        ):
            raise ValueError("Recipe requires distinct, nonempty outputs")


@dataclass(frozen=True)
class ProtocolStep:
    signal: str
    duration: object
    offset: object = 0


@dataclass(frozen=True)
class Protocol:
    steps: tuple[ProtocolStep, ...]
    description: str = ""

    def __post_init__(self):
        object.__setattr__(self, "steps", tuple(self.steps))
        if not self.steps:
            raise ValueError("A protocol must have at least one explicit trigger step")


@dataclass
class DeviceSetup:
    devices: dict[str, Device]
    tick_seconds: float = 1e-9
    protocols: dict[str, Protocol] = field(default_factory=dict)
    calibration: dict[str, DeviceCalibration] = field(default_factory=dict)
    name: str = "reference-offline-only"
    timing_verified: bool = False

    def __post_init__(self):
        if not math.isfinite(self.tick_seconds) or self.tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        for name, device in self.devices.items():
            if name != device.name:
                raise ValueError("Device dictionary key must match device.name")
        if set(self.calibration) - set(self.devices):
            raise ValueError("Calibration refers to unknown device")
        self.devices = copy.deepcopy(dict(self.devices))
        self.protocols = copy.deepcopy(dict(self.protocols))
        self.calibration = copy.deepcopy(dict(self.calibration))

    def to_dict(self):
        from .dsl import json_value

        return json_value(asdict(self))


class SignalMap(dict):
    """Logical signal -> device name, binding, or multi-output ControlRecipe."""

    def bindings(self, signal):
        if signal not in self:
            raise ValueError(f"Unmapped logical signal: {signal}")
        binding = self[signal]
        if isinstance(binding, str):
            return (ControlBinding(binding),)
        if isinstance(binding, ControlBinding):
            return (binding,)
        if isinstance(binding, ControlRecipe):
            return binding.outputs
        raise TypeError(f"Invalid signal binding for {signal}")

    def to_dict(self):
        return {key: [asdict(b) for b in self.bindings(key)] for key in self}


def reference_setup(qubits):
    """An illustrative board per qubit; trigger protocols are NOT lab preparation."""
    from .units import us

    devices = {}
    mapping = SignalMap()
    items = qubits.values() if hasattr(qubits, "values") else qubits
    for index, q in enumerate(items):
        for role, channel in (("raman", 0), ("rf", 1)):
            name = f"urukul{index}_ch{channel}"
            devices[name] = Device(
                name, "dds", bus=f"spi{index}", update_group=f"urukul{index}"
            )
            mapping[f"{q.uid}.{role}"] = name
        for role in ("prepare", "readout"):
            name = f"ttl_{index}_{role}"
            devices[name] = Device(name, "ttl", grid_mu=1, min_duration_mu=1)
            mapping[f"{q.uid}.{role}"] = name
    setup = DeviceSetup(
        devices=devices,
        protocols={
            "standard": Protocol(
                (ProtocolStep("{qubit}.prepare", 20 * us),),
                "Illustrative preparation trigger only; supply laboratory protocol",
            ),
            "state_readout": Protocol(
                (ProtocolStep("{qubit}.readout", None),),
                "Illustrative camera/readout trigger; data transfer is host-side",
            ),
        },
    )
    return setup, mapping
