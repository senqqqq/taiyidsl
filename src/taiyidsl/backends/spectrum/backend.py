"""Compile an entire shot onto one software-started Spectrum card."""

from __future__ import annotations

from taiyidsl.backends.artiq_spectrum.awg import build_card_program
from taiyidsl.backends.artiq_spectrum.backend import (
    card_sample_assets,
    resolve_card_pulses,
)
from taiyidsl.backends.artiq_spectrum.dds import DDS_TICK_MU
from taiyidsl.backends.artiq_spectrum.scheduling import (
    SpectrumPlaybackScheduler,
    validate_sampled_route,
)
from taiyidsl.backends.base import Backend, BackendCapabilities
from taiyidsl.compiler.scheduling import resolve_frames
from taiyidsl.config.spectrum import HYBRID_TICK_SECONDS
from taiyidsl.dsl.pulses import SampledPulse, Square
from taiyidsl.ir import CompileError, PhysicalPlan, ScheduledEvent

_FRAME_OPERATIONS = {"rz", "shift_phase", "reset_frame", "reset_phase"}
_SAMPLE_TICKS = 4  # 0.8 ns samples on the 0.2 ns common logical timebase.


class _Capabilities(BackendCapabilities):
    supports_phase_modulation = True


class _ShotScheduler(SpectrumPlaybackScheduler):
    ordered_frame_edges = True

    def __init__(self, *args):
        super().__init__(*args)
        self.config = self.setup.standalone_spectrum
        self._signal_devices = {
            signal: tuple(b.device for b in self.mapping.bindings(signal))
            for signal in self.mapping
        }
        self._signal_frames.update(self.config.signal_frames)

    def validate_body(self, body):
        """Check references and mode constraints even in inactive branches."""
        for node in body:
            if node.kind in {"play", "delay", "reserve"}:
                signal = node.params["signal"]
                if signal not in self.mapping and not (
                    node.kind == "reserve" and signal in self.config.outputs
                ):
                    raise CompileError(
                        f"Unmapped Spectrum signal {signal} at {node.source}"
                    )
            if node.kind == "play":
                frame = self.signal_frame(signal)
                if frame is not None and frame not in self._frame_owners:
                    raise CompileError(
                        f"Unknown frame {frame} for {signal} at {node.source}"
                    )
                pulse = node.params["pulse"]
                if isinstance(pulse, SampledPulse):
                    card = self.config.card
                    validate_sampled_route(
                        node, card.sample_rate_hz if card.mode == "awg" else None
                    )
                if self.config.card.mode == "dds" and (
                    not isinstance(pulse, Square)
                    or pulse.phase_modulation is not None
                    or pulse.phase_modulation_mode != "local"
                ):
                    raise CompileError(
                        f"Spectrum DDS requires unmodulated Square pulses at "
                        f"{node.source}"
                    )
            self.validate_body(node.children)

    def section_grid(self):
        return self.backend.signal_grid(None, self.setup, self.mapping)

    def ticks(self, value, source, dimension="time", grid_mu=1):
        # Also validate unqualified Section waits and exact_duration requests.
        return super().ticks(value, source, dimension, self.section_grid())

    def node(self, node, frames, realtime):
        if node.kind == "play":
            return self.schedule_play(node, frames, realtime)
        if node.kind == "scope_delay":
            if not realtime:
                raise CompileError(f"delay requires realtime at {node.source}")
            duration = self.ticks(node.params["duration"], node.source)
            return [
                ScheduledEvent("scope_delay", "", 0, duration, source=node.source)
            ], duration
        events, duration = super().node(node, frames, realtime)
        if node.kind in {"delay", "reserve"}:
            for event in events:
                event.resources = self._signal_devices.get(
                    event.signal, (event.signal,)
                )
        elif node.kind in _FRAME_OPERATIONS:
            for event in events:
                signals = [
                    s for s in self.mapping if self.signal_frame(s) == event.signal
                ]
                if not signals:
                    raise CompileError(
                        f"Frame {event.signal} has no mapped Spectrum outputs"
                    )
                devices = {d for s in signals for d in self._signal_devices[s]}
                event.params.update(
                    target_signals=signals, target_devices=sorted(devices)
                )
        return events, duration


class SpectrumBackend(Backend):
    name = "spectrum"
    version = "3"
    target = "Single software-started M4i.6631-x8, AWG sequence or DDS20"
    phase_policy = "shot_relative_to_card_playback_start"
    capabilities = _Capabilities(
        device_kinds=frozenset({"spectrum"}),
        pulse_shapes=frozenset({"square", "flat_top_gaussian", "sampled"}),
        operations=frozenset(
            {"play", "delay", "scope_delay", "reserve", *_FRAME_OPERATIONS}
        ),
    )

    def prepare_setup(self, setup):
        if setup.standalone_spectrum is None:
            raise CompileError(
                "spectrum requires a standalone Spectrum card configuration"
            )
        setup.tick_seconds = HYBRID_TICK_SECONDS
        return setup

    def validate_setup(self, setup, mapping):
        config = setup.standalone_spectrum
        if setup.spectrum or getattr(setup, "_transport", None) or setup.zone_coverage:
            raise CompileError(
                "Standalone Spectrum playback requires a single waveform card"
            )
        if setup.calibration or any(d.latency_mu for d in setup.devices.values()):
            raise CompileError(
                "Standalone Spectrum playback uses the card-local timing origin"
            )
        if set(config.outputs) != set(setup.devices):
            raise CompileError(
                "Each Spectrum device must name an output on the standalone card"
            )
        for signal in mapping:
            for binding in mapping.bindings(signal):
                if binding.device not in config.outputs:
                    raise CompileError(
                        f"Unknown mapped Spectrum output {binding.device}"
                    )
        if set(config.signal_frames) - set(mapping):
            raise CompileError("Standalone signal_frames includes an unmapped signal")
        for name, output in config.outputs.items():
            device = setup.devices[name]
            frequency = output.initial_frequency_hz
            if frequency is not None and not (
                device.min_frequency
                <= frequency
                <= min(device.max_frequency, config.card.sample_rate_hz / 2)
            ):
                raise CompileError(
                    f"Spectrum initial frequency exceeds the contract for {name}"
                )

    def signal_grid(self, signal, setup, mapping):
        return (
            DDS_TICK_MU
            if setup.standalone_spectrum.card.mode == "dds"
            else _SAMPLE_TICKS
        )

    def schedule(
        self,
        experiment,
        body,
        setup,
        mapping,
        coordinates,
        allow_quantization,
        diagnostics,
        max_events,
    ):
        if allow_quantization:
            raise CompileError("Standalone Spectrum playback requires strict timing")
        scheduler = _ShotScheduler(
            experiment,
            setup,
            coordinates,
            False,
            diagnostics,
            max_events,
            mapping,
            self,
        )
        scheduler.validate_body(body)
        events, duration = scheduler.serial(body, {uid: 0 for uid in experiment.qubits})
        resolve_frames(events)
        occupied_until = {}
        for event in sorted(events, key=lambda e: e.start_mu):
            if event.kind in _FRAME_OPERATIONS:
                event.params["effective_frame_phase"] = event.params["phase"]
            if event.kind in {"play", "delay", "reserve"} and event.duration_mu:
                for device in event.resources:
                    if event.start_mu < occupied_until.get(device, 0):
                        raise CompileError(
                            f"Overlapping Spectrum operations on {device} "
                            f"at {event.source}"
                        )
                    occupied_until[device] = event.end_mu
        timeline = ScheduledEvent(
            "spectrum_timeline", setup.standalone_spectrum.card_name, 0, duration
        )
        return [timeline, *events], duration

    def lower(self, logical_events, setup, mapping, diagnostics):
        config = setup.standalone_spectrum
        timeline, *events = logical_events
        for index, event in enumerate(logical_events):
            event.logical_ids = (f"e{index}",)
        pulses = resolve_card_pulses(
            events,
            config.card_name,
            config.card,
            config.outputs,
            setup,
            mapping,
            diagnostics,
            start_mu=0,
            reference_kind="shot",
        )
        contract = config.contract
        duration_samples = timeline.duration_mu // _SAMPLE_TICKS
        if config.card.mode == "dds":
            from .dds import build_square_program

            program = build_square_program(contract, pulses, duration_samples)
            duration_mu = timeline.duration_mu
        else:
            program = build_card_program(
                contract,
                pulses,
                duration_samples,
                max_tile_samples=config.max_tile_samples,
                sample_assets=card_sample_assets(pulses, setup),
            )
            duration_mu = program["physical_duration_samples"] * _SAMPLE_TICKS
        shot = ScheduledEvent(
            "spectrum_shot",
            config.card_name,
            0,
            duration_mu,
            {"contract": contract, "program": program},
            resources=tuple(config.outputs),
            logical_ids=tuple(e.logical_ids[0] for e in logical_events),
        )
        return PhysicalPlan([shot], origin_mu=0)

    def export(self, compiled, output_dir, **options):
        from .export import export_spectrum

        return export_spectrum(compiled, output_dir, **options)
