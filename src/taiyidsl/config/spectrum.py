"""Offline contracts for standalone Spectrum cards and hybrid trigger groups.

ARTIQ device fields retain their original machine units. Spectrum backends
schedule events on a 200 ps logical timebase. Timing values in example contracts are
declarations, never evidence of a measured trigger or analog latency.
"""

from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass, field
from fractions import Fraction

HYBRID_TICK_SECONDS = 0.2e-9
SAMPLE_RATE_HZ = 1_250_000_000
EXTERNAL_REFERENCE = "external_reference"
INTERNAL_PLL_BENCH = "internal_pll_bench"


def uncorrected_bench_profile(profile):
    """A bench shot must not silently apply or downgrade a measured profile."""
    return profile.get("status") == "unmeasured" and not (
        profile.keys() - {"status", "revision"}
    )


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class SpectrumCard:
    serial_number: str
    active_channels: tuple[int, ...] = (0, 1)
    model: str = "M4i.6631-x8"
    sample_rate_hz: int = SAMPLE_RATE_HZ
    reference_clock_hz: int | None = None
    memory_bytes: int = 4 * 1024**3
    output_range_mv: int = 500
    trigger_latency_samples: int = 500
    firmware: str | None = None
    cable_id: str = "unmeasured"
    mode: str = "awg"
    dds_routing: str = "16/5"
    # DDS trigger latency is an independent declaration, never the AWG sample
    # latency above. None deliberately requires a timing profile before lowering.
    dds_trigger_latency_ns: float | None = None
    dds_amplitude_slope_step_per_s: float | None = None
    clock_policy: str = EXTERNAL_REFERENCE

    def __post_init__(self):
        if not isinstance(self.serial_number, str) or not self.serial_number:
            raise ValueError("Spectrum serial_number must be explicit")
        if self.model != "M4i.6631-x8":
            raise ValueError("Only M4i.6631-x8 is supported")
        if self.mode not in {"awg", "dds"}:
            raise ValueError("Spectrum mode must be awg or dds")
        if self.dds_routing not in {"16/5", "20/1"}:
            raise ValueError("M4i.6631 DDS routing must be 16/5 or 20/1")
        latency = self.dds_trigger_latency_ns
        if latency is not None and (
            isinstance(latency, bool)
            or not isinstance(latency, (int, float))
            or not math.isfinite(latency)
            or latency < 0
        ):
            raise ValueError("dds_trigger_latency_ns must be finite and nonnegative")
        amplitude_step = self.dds_amplitude_slope_step_per_s
        if amplitude_step is not None and (
            isinstance(amplitude_step, bool)
            or not isinstance(amplitude_step, (int, float))
            or not math.isfinite(amplitude_step)
            or amplitude_step <= 0
        ):
            raise ValueError(
                "dds_amplitude_slope_step_per_s must be finite and positive"
            )
        if self.mode == "awg" and (
            self.dds_routing != "16/5"
            or latency is not None
            or amplitude_step is not None
        ):
            raise ValueError("DDS-specific settings require mode='dds'")
        channels = tuple(self.active_channels)
        if channels not in {(0,), (1,), (0, 1)} or any(
            type(ch) is not int for ch in channels
        ):
            raise ValueError("active_channels must be (0,), (1,), or (0, 1)")
        object.__setattr__(self, "active_channels", channels)
        if (
            type(self.sample_rate_hz) is not int
            or self.sample_rate_hz != SAMPLE_RATE_HZ
        ):
            raise ValueError("The M4i.6631 implementation requires 1.25 GS/s")
        if self.clock_policy == EXTERNAL_REFERENCE:
            if self.reference_clock_hz is None:
                object.__setattr__(self, "reference_clock_hz", 125_000_000)
            elif self.reference_clock_hz != 125_000_000:
                raise ValueError("The reference clock contract requires 125 MHz")
        elif self.clock_policy == INTERNAL_PLL_BENCH:
            if self.mode != "awg" or self.reference_clock_hz is not None:
                raise ValueError(
                    "Internal PLL bench permits AWG only, without a reference frequency"
                )
        else:
            raise ValueError(f"Unknown Spectrum clock_policy: {self.clock_policy!r}")
        _integer(self.memory_bytes, "memory_bytes", 1)
        _integer(self.trigger_latency_samples, "trigger_latency_samples")
        _integer(self.output_range_mv, "output_range_mv", 80)
        if self.output_range_mv > 2000:
            raise ValueError("M4i.6631 output range must not exceed 2000 mV")
        if self.firmware is not None and not isinstance(self.firmware, str):
            raise TypeError("firmware must be a string or None")

    @property
    def dds_channel_cores(self):
        """DDS20 fixed hardware mux; dedicated channel-1 core is number 20."""
        if self.dds_routing == "20/1":
            return {0: tuple(range(20)), 1: (20,)}
        return {0: (*range(8), *range(12, 20)), 1: (8, 9, 10, 11, 20)}

    def to_dict(self):
        """Preserve legacy AWG dictionaries while recording explicit DDS mode."""
        result = asdict(self)
        if self.clock_policy == EXTERNAL_REFERENCE:
            del result["clock_policy"]  # Preserve existing contracts and hashes.
        if self.mode == "awg":
            for key in (
                "mode",
                "dds_routing",
                "dds_trigger_latency_ns",
                "dds_amplitude_slope_step_per_s",
            ):
                result.pop(key)
        return result


@dataclass(frozen=True)
class SpectrumOutput:
    card: str
    channel: int
    carrier_mode: str = "epoch"
    initial_frequency_hz: float | None = None

    def __post_init__(self):
        if not isinstance(self.card, str) or not self.card:
            raise ValueError("Spectrum output requires a card name")
        if type(self.channel) is not int or self.channel not in {0, 1}:
            raise ValueError("Spectrum output channel must be 0 or 1")
        if self.carrier_mode not in {"epoch", "continuous"}:
            raise ValueError("Spectrum carrier_mode must be epoch or continuous")
        frequency = self.initial_frequency_hz
        if frequency is not None and (
            isinstance(frequency, bool)
            or not isinstance(frequency, (int, float))
            or not math.isfinite(frequency)
            or not 0 <= frequency <= 400e6
        ):
            raise ValueError(
                "initial_frequency_hz must be finite physical Hz in 0..400e6"
            )
        if self.carrier_mode == "continuous" and frequency is None:
            raise ValueError("Continuous Spectrum output requires initial_frequency_hz")
        if self.carrier_mode == "epoch" and frequency is not None:
            raise ValueError("initial_frequency_hz is only used by continuous carriers")


def _validate_outputs(outputs, cards):
    occupied = set()
    for output in outputs.values():
        if not isinstance(output, SpectrumOutput) or output.card not in cards:
            raise ValueError("Spectrum output refers to an unknown card")
        card = cards[output.card]
        if output.channel not in card.active_channels:
            raise ValueError("Spectrum output channel is not enabled")
        if card.mode == "dds" and output.carrier_mode != "epoch":
            raise ValueError("DDS tones do not use AWG carrier_mode settings")
        physical = (output.card, output.channel)
        if physical in occupied:
            raise ValueError("Two Spectrum devices alias one physical output")
        occupied.add(physical)


@dataclass(frozen=True)
class StandaloneSpectrumConfig:
    """One internally clocked card, software-started once per shot."""

    card_name: str
    card: SpectrumCard
    outputs: dict[str, SpectrumOutput]
    signal_frames: dict[str, str | None] = field(default_factory=dict)
    max_tile_samples: int = 500_000

    def __post_init__(self):
        if not isinstance(self.card_name, str) or not self.card_name:
            raise ValueError("Standalone Spectrum requires a card name")
        if not isinstance(self.card, SpectrumCard) or not self.outputs:
            raise ValueError("Standalone Spectrum requires one card and its outputs")
        if self.card.clock_policy == INTERNAL_PLL_BENCH:
            raise ValueError(
                "Standalone Spectrum already uses internal clock and software trigger"
            )
        _validate_outputs(self.outputs, {self.card_name: self.card})
        _integer(self.max_tile_samples, "max_tile_samples", 384)
        if self.max_tile_samples > 500_000:
            raise ValueError("max_tile_samples must not exceed 500000")
        object.__setattr__(self, "outputs", copy.deepcopy(dict(self.outputs)))
        object.__setattr__(self, "signal_frames", dict(self.signal_frames))

    @property
    def contract(self):
        """Settings that determine hardware playback under the internal clock."""
        from ._serialization import config_json_value

        card = self.card
        result = {
            "model": card.model,
            "serial_number": card.serial_number,
            "firmware": card.firmware,
            "mode": card.mode,
            "active_channels": card.active_channels,
            "output_range_mv": card.output_range_mv,
            "sample_rate_hz": card.sample_rate_hz,
            "memory_bytes": card.memory_bytes,
            "clock_source": "internal",
            "trigger_source": "software",
        }
        if card.mode == "dds":
            result["dds_routing"] = card.dds_routing
        return config_json_value(result)

    def to_dict(self):
        from ._serialization import config_json_value

        data = asdict(self)
        data["card"] = self.contract
        # These fixed playback policies belong to the standalone target.
        del data["card"]["clock_source"], data["card"]["trigger_source"]
        return config_json_value(data)

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        values["card"] = SpectrumCard(**values["card"])
        values["outputs"] = {
            name: SpectrumOutput(**output) for name, output in values["outputs"].items()
        }
        return cls(**values)


@dataclass(frozen=True)
class SpectrumGroup:
    trigger_device: str | None = None
    cards: dict[str, SpectrumCard] = field(default_factory=dict)
    outputs: dict[str, SpectrumOutput] = field(default_factory=dict)
    trigger_lead_mu: int = 512
    trigger_width_mu: int = 64
    epoch_grid_mu: int = 32
    epoch_offset_mu: int = 0
    max_tile_samples: int = 500_000
    engine: str = "awg_sequence"
    calibration: dict = field(
        default_factory=lambda: {"status": "unmeasured", "revision": "unmeasured"}
    )
    # Optional logical name for the existing physical trigger path, not a way to
    # select a different wire while reusing the original timing/calibration.
    trigger_signal: str | None = None
    trigger_devices: dict[str, str] | None = None

    def __post_init__(self):
        if (self.trigger_device is None) == (self.trigger_devices is None):
            raise ValueError(
                "Specify exactly one of trigger_device and trigger_devices"
            )
        if self.trigger_device is not None and (
            not isinstance(self.trigger_device, str) or not self.trigger_device
        ):
            raise ValueError("A Spectrum group needs a nonempty ARTIQ trigger device")
        if self.trigger_devices is not None:
            routes = dict(self.trigger_devices)
            if set(routes) != set(self.cards):
                raise ValueError("trigger_devices must cover exactly the group's cards")
            if any(not isinstance(v, str) or not v for v in routes.values()):
                raise ValueError("Every card trigger must name an ARTIQ TTL device")
            object.__setattr__(self, "trigger_devices", copy.deepcopy(routes))
        if self.trigger_signal is not None and (
            not isinstance(self.trigger_signal, str) or not self.trigger_signal
        ):
            raise ValueError("trigger_signal must be a nonempty logical signal name")
        if self.engine != "awg_sequence":
            raise ValueError(
                "Legacy group engine must be awg_sequence; select each card's mode"
            )
        if not self.cards or not self.outputs:
            raise ValueError("A Spectrum group requires cards and outputs")
        if any(not isinstance(c, SpectrumCard) for c in self.cards.values()):
            raise TypeError("cards must contain SpectrumCard contracts")
        if len({c.serial_number for c in self.cards.values()}) != len(self.cards):
            raise ValueError("Two card names cannot select the same serial number")
        if self.internal_clock_bench:
            if len(self.cards) != 1:
                raise ValueError("Internal PLL bench permits only one Spectrum card")
            if not uncorrected_bench_profile(self.calibration):
                raise ValueError(
                    "Internal PLL bench requires unmeasured, uncorrected setpoints"
                )
        _validate_outputs(self.outputs, self.cards)
        for name in ("trigger_lead_mu", "trigger_width_mu", "epoch_grid_mu"):
            _integer(getattr(self, name), name, 1)
        _integer(self.epoch_offset_mu, "epoch_offset_mu")
        if self.epoch_offset_mu >= self.epoch_grid_mu:
            raise ValueError("epoch_offset_mu must lie inside epoch_grid_mu")
        if self.trigger_width_mu > self.trigger_lead_mu:
            raise ValueError("The trigger pulse must finish before the region starts")
        _integer(self.max_tile_samples, "max_tile_samples", 384)
        if self.max_tile_samples > 500_000:
            raise ValueError("max_tile_samples must not exceed 500000")
        object.__setattr__(self, "cards", copy.deepcopy(dict(self.cards)))
        object.__setattr__(self, "outputs", copy.deepcopy(dict(self.outputs)))
        object.__setattr__(self, "calibration", copy.deepcopy(dict(self.calibration)))

    @property
    def internal_clock_bench(self):
        return any(
            card.clock_policy == INTERNAL_PLL_BENCH for card in self.cards.values()
        )

    @property
    def sample_rate_hz(self):
        return SAMPLE_RATE_HZ

    @property
    def card_trigger_devices(self):
        """Return each card's EXT0 source, including legacy shared wiring."""
        if self.trigger_devices is not None:
            return dict(self.trigger_devices)
        return dict.fromkeys(self.cards, self.trigger_device)

    @property
    def trigger_device_names(self):
        return tuple(sorted(set(self.card_trigger_devices.values())))


@dataclass(frozen=True)
class SpectrumConfig:
    groups: dict[str, SpectrumGroup]
    signal_frames: dict[str, str | None] = field(default_factory=dict)
    artiq_tick_seconds: float = 1e-9

    def __post_init__(self):
        if not self.groups or any(
            not isinstance(k, str) or not k or not isinstance(v, SpectrumGroup)
            for k, v in self.groups.items()
        ):
            raise ValueError("groups must map names to SpectrumGroup contracts")
        tick = self.artiq_tick_seconds
        if isinstance(tick, bool) or not math.isfinite(tick) or tick <= 0:
            raise ValueError("Invalid native ARTIQ tick")
        ratio = Fraction(str(tick)) / Fraction(str(HYBRID_TICK_SECONDS))
        if ratio.denominator != 1:
            raise ValueError("ARTIQ tick must be an integral multiple of 200 ps")
        serials, outputs, triggers, card_names = set(), set(), set(), set()
        if len(self.groups) != 1 and any(
            g.internal_clock_bench for g in self.groups.values()
        ):
            raise ValueError(
                "Internal PLL bench cannot share a setup with another Spectrum group"
            )
        for group in self.groups.values():
            new_serials = {card.serial_number for card in group.cards.values()}
            if serials & new_serials or outputs & set(group.outputs):
                raise ValueError("Spectrum hardware cannot belong to multiple groups")
            group_triggers = set(group.trigger_device_names)
            if group_triggers & triggers:
                raise ValueError("Spectrum groups cannot share an ARTIQ trigger output")
            if card_names & set(group.cards):
                raise ValueError("Spectrum card names must be unique across groups")
            serials |= new_serials
            outputs |= set(group.outputs)
            triggers |= group_triggers
            card_names |= set(group.cards)
        for signal, frame in self.signal_frames.items():
            if not isinstance(signal, str) or not signal:
                raise ValueError("A frame association requires a signal name")
            if frame is not None and (not isinstance(frame, str) or not frame):
                raise ValueError("A signal frame must be a qubit ID or explicit None")
        object.__setattr__(self, "groups", copy.deepcopy(dict(self.groups)))
        object.__setattr__(self, "signal_frames", dict(self.signal_frames))

    def to_dict(self):
        from ._serialization import config_json_value as json_value

        data = asdict(self)
        for name, group in data["groups"].items():
            group["cards"] = {
                uid: card.to_dict() for uid, card in self.groups[name].cards.items()
            }
            if group["trigger_signal"] is None:
                group.pop("trigger_signal")
            if group["trigger_devices"] is None:
                group.pop("trigger_devices")
            if group["trigger_device"] is None:
                group.pop("trigger_device")
            for output in group["outputs"].values():
                if output["carrier_mode"] == "epoch":
                    output.pop("carrier_mode")
                if output["initial_frequency_hz"] is None:
                    output.pop("initial_frequency_hz")
        return json_value(data)

    @classmethod
    def from_dict(cls, data):
        data = copy.deepcopy(dict(data))
        groups = {}
        for name, values in data.pop("groups").items():
            values = dict(values)
            values["cards"] = {k: SpectrumCard(**v) for k, v in values["cards"].items()}
            values["outputs"] = {
                k: SpectrumOutput(**v) for k, v in values["outputs"].items()
            }
            groups[name] = SpectrumGroup(**values)
        return cls(groups=groups, **data)
