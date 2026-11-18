"""Explicit device contracts. Reference values are assumptions, not measurements."""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Device:
    """Common channel contract with legacy DDS fields for API compatibility.

    A backend validates its device kinds and target-specific clock/register
    constraints at compilation. New targets may use dataclass subclasses for
    additional configuration; DeviceSetup snapshots and serializes those fields.
    """

    name: str
    kind: str
    config_duration_mu: int = 3000
    grid_mu: int = 4
    min_duration_mu: int = 4
    bus: str | None = None
    update_group: str | None = None
    sysclk_hz: float = 1e9
    latency_mu: int = 0
    min_frequency: float = 0
    max_frequency: float = 400e6
    config_grid_mu: int = 8
    # Measured/declared IO_UPDATE-to-envelope delay, separate from output latency.
    # None means the channel has no usable RAM playback timing contract yet.
    ram_latency_mu: int | None = None
    # Declared RF-off budget for disabling/rearming resident RAM between calls.
    ram_rearm_duration_mu: int | None = None
    # Opt-in RF-off budget for POW-only updates while RAM stays enabled.
    ram_phase_update_duration_mu: int | None = None
    # Opt-in continuous ASF RAM: separately declared coherent carrier-reset/POW
    # output latency. Matched latency does NOT equate this with RAM amplitude.
    ram_phase_latency_mu: int | None = None
    # Explicit matched single-tone commit-to-output contract; None keeps the
    # legacy RF-off set_mu path. These are declarations, not measurements.
    square_latency_mu: int | None = None
    square_prepare_duration_mu: int = 1280
    spi_ref_period_mu: int = 8
    # Legacy configurations keep their recorded tracking policy. Continuous
    # operation requires a separately declared, fixed-FTW phase epoch.
    nco_mode: str = "legacy_tracking"
    nco_epoch_latency_mu: int | None = None
    # POW-only output latency with autoclear disabled; NOT the legacy joint
    # POW/autoclear ram_phase_latency_mu contract.
    ram_pow_latency_mu: int | None = None
    # Optional polar RAM keeps phase samples beside unchanged ASF samples.
    # Its phase pipeline is distinct from the independent POW-register path.
    ram_destination: str = "asf"
    ram_polar_phase_latency_mu: int | None = None
    ram_profile_control: str = "urukul_rev9"
    # Installed Urukul control-logic protocol, independent of DDS playback mode.
    # None keeps legacy unspecified topology; it does not detect the instrument.
    urukul_proto_rev: int | None = None
    # Profile-selection output delays are measured from the end cursor of the
    # real CPLD set_profile call, not from its first SPI write or IO_UPDATE.
    ram_profile_latency_mu: int | None = None
    ram_profile_phase_latency_mu: int | None = None
    # Digital idle state; trig drives its opposite, then restores this level.
    ttl_idle_level: int = 0

    def __post_init__(self):
        if not self.name.isidentifier() or not self.kind.isidentifier():
            raise ValueError("Device name and kind must be identifiers")
        if type(self.ttl_idle_level) is not int or self.ttl_idle_level not in {0, 1}:
            raise ValueError("ttl_idle_level must be integer 0 or 1")
        if self.kind != "ttl" and self.ttl_idle_level != 0:
            raise ValueError("ttl_idle_level applies only to TTL devices")
        if self.nco_mode not in {"legacy_tracking", "continuous"}:
            raise ValueError("nco_mode must be legacy_tracking or continuous")
        if self.ram_destination not in {"asf", "powasf"}:
            raise ValueError("ram_destination must be asf or powasf")
        if self.ram_profile_control not in {"urukul_rev8", "urukul_rev9"}:
            raise ValueError("ram_profile_control must be urukul_rev8 or urukul_rev9")
        if self.urukul_proto_rev is not None and (
            type(self.urukul_proto_rev) is not int
            or self.urukul_proto_rev not in {8, 9}
        ):
            raise ValueError("urukul_proto_rev must be integer 8, 9 or None")
        if self.ram_destination == "powasf" and self.urukul_proto_rev is not None:
            if self.ram_profile_control != f"urukul_rev{self.urukul_proto_rev}":
                raise ValueError("urukul_proto_rev conflicts with RAM profile control")
        if self.ram_destination == "powasf" and self.kind != "dds":
            raise ValueError("POWASF RAM requires a DDS device")
        for name in (
            "grid_mu",
            "min_duration_mu",
            "config_duration_mu",
            "config_grid_mu",
            "square_prepare_duration_mu",
            "spi_ref_period_mu",
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.latency_mu, int) or self.latency_mu < 0:
            raise ValueError("latency_mu must be a nonnegative integer")
        for name in (
            "ram_latency_mu",
            "ram_rearm_duration_mu",
            "ram_phase_update_duration_mu",
            "ram_phase_latency_mu",
            "square_latency_mu",
            "nco_epoch_latency_mu",
            "ram_pow_latency_mu",
            "ram_polar_phase_latency_mu",
            "ram_profile_latency_mu",
            "ram_profile_phase_latency_mu",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer or None")

    def to_dict(self):
        result = asdict(self)
        if self.ttl_idle_level == 0:
            result.pop("ttl_idle_level")
        return result


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
class TriggerStep:
    """One timed logical trigger; duration=None uses the qubit readout default."""

    signal: str
    duration: object
    offset: object = 0


@dataclass(frozen=True)
class TriggerSequence:
    """Finite trigger steps, distinct from experiment operation builders."""

    steps: tuple[TriggerStep, ...]
    description: str = ""

    def __post_init__(self):
        object.__setattr__(self, "steps", tuple(self.steps))
        if not self.steps:
            raise ValueError("A trigger sequence needs at least one explicit step")


# Compatibility names for existing users and serialized Python construction.
ProtocolStep = TriggerStep
Protocol = TriggerSequence


@dataclass
class DeviceSetup:
    devices: dict[str, Device]
    tick_seconds: float = 1e-9
    protocols: dict[str, Protocol] = field(default_factory=dict)
    calibration: dict[str, DeviceCalibration] = field(default_factory=dict)
    name: str = "reference-offline-only"
    timing_verified: bool = False
    # Declared target RTIO capacity; the reference target uses coarse 8 mu / 8 lanes.
    rtio_coarse_mu: int = 8
    rtio_lane_count: int = 8
    # Optional offline target contract; native ARTIQ device mu fields are unchanged.
    spectrum: object = None

    @classmethod
    def from_descriptor(cls, descriptor, *, device_db, spec_db=None):
        """Resolve databases and wiring without importing or opening drivers."""
        from .descriptor import setup_from_descriptor

        return setup_from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)

    def connections(self):
        """Detached logical signal to instrument/physical-port overview."""
        return copy.deepcopy(getattr(self, "_connections", []))

    def trigger_connections(self):
        """Detached ARTIQ TTL to Spectrum-card wiring overview."""
        return copy.deepcopy(getattr(self, "_trigger_connections", []))

    def describe(self):
        """Inspect database addresses, effective card settings and signal wiring.

        This detached author-facing view is configuration, not a hardware probe
        or a replacement for the existing target serialization in to_dict().
        """
        from .descriptor import describe_setup

        return describe_setup(self)

    @property
    def shared_outputs(self):
        """Declared global fields and their affected logical signals."""
        return copy.deepcopy(getattr(self, "_shared_outputs", {}))

    @property
    def zone_coverage(self):
        """Optical output to illuminated interaction zones, before occupancy."""
        return copy.deepcopy(getattr(self, "_zone_coverage", {}))

    def __post_init__(self):
        if self.spectrum is not None:
            from .spectrum import SpectrumConfig

            if not isinstance(self.spectrum, SpectrumConfig):
                raise TypeError("spectrum must be a SpectrumConfig")
            self.spectrum = copy.deepcopy(self.spectrum)
        for value in (self.rtio_coarse_mu, self.rtio_lane_count):
            if type(value) is not int or value <= 0:
                raise ValueError(
                    "RTIO coarse period and lane count must be positive integers"
                )
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
        from ._serialization import config_json_value as json_value

        result = asdict(self)
        result["devices"] = {
            name: device.to_dict() for name, device in self.devices.items()
        }
        if self.spectrum is None:
            result.pop("spectrum")
        else:
            result["spectrum"] = self.spectrum.to_dict()
        if self.zone_coverage:
            result["zone_coverage"] = self.zone_coverage
        transport = getattr(self, "_transport", None)
        if transport is not None:
            result["transport"] = transport.to_dict()
        return json_value(result)


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
