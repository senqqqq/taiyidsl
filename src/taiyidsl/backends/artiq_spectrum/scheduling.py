"""Static Spectrum-region scheduling without driver imports or device access."""

from __future__ import annotations

import math
from dataclasses import replace

from taiyidsl.compiler.scheduling import Scheduler, resolve_frames
from taiyidsl.compiler.timebase import native_factor
from taiyidsl.compiler.trigger_routes import resolve_trigger_signal
from taiyidsl.dsl.pulses import HalfSinePhase, PolynomialPhase, SampledPulse
from taiyidsl.dsl.values import resolve
from taiyidsl.ir import CompileError, ScheduledEvent
from taiyidsl.units import as_fraction

_FRAME_OPERATIONS = {"rz", "shift_phase", "reset_frame", "reset_phase"}
_REGION_OPERATIONS = {
    "move",
    "play",
    "trigplay",
    "delay",
    "scope_delay",
    "serial",
    "parallel",
    "repeat",
    "case",
    "barrier",
    "section_delay",
    *_FRAME_OPERATIONS,
}


def validate_sampled_route(node, sample_rate_hz):
    """Reject unsupported card routes or nonrepresentable sample timing."""
    pulse = node.params["pulse"]
    if sample_rate_hz is None:
        raise CompileError(
            f"Sampled pulses require a Spectrum AWG output at {node.source}"
        )
    rate = as_fraction(sample_rate_hz, "frequency")
    interval = as_fraction(pulse.sample_interval, "time") * rate
    if pulse.resample == "strict" and interval != 1:
        raise CompileError(
            f"Sample interval differs from the AWG sample grid at {node.source}; "
            "select linear resampling explicitly"
        )
    if (interval * pulse.sample_count).denominator != 1:
        raise CompileError(
            f"Sampled pulse duration is off the AWG grid at {node.source}"
        )


class SpectrumPlaybackScheduler(Scheduler):
    """Resolve pulse and modulation requests before assigning device timing."""

    spatial = None

    def schedule_play(self, node, frames, realtime):
        pulse, signal = node.params["pulse"], node.params["signal"]
        overrides = {"phase_modulation": None}
        if hasattr(pulse, "phase_modulation_mode"):
            overrides["phase_modulation_mode"] = "local"
        ordinary = replace(
            node,
            kind="play",
            params={**node.params, "pulse": replace(pulse, **overrides)},
        )
        events, duration = super().node(ordinary, frames, realtime)
        if self.spatial is not None:
            self.spatial.apply_coverage(node, events)
        frame = self._signal_frames[signal]
        for event in events:
            event.resources = self._signal_devices[signal]
            event.params.update(
                phase=float(resolve(pulse.phase, self.coordinates, "phase")),
                requested_phase=float(resolve(pulse.phase, self.coordinates, "phase")),
                frame=frame,
                qubit=self._frame_owners[frame]
                if frame is not None
                else event.params.get("qubit"),
                frame_phase=0.0,
                phase_modulation_mode=getattr(pulse, "phase_modulation_mode", "local"),
            )
            modulation = pulse.phase_modulation
            if isinstance(modulation, HalfSinePhase):
                event.params["phase_modulation"] = {
                    "kind": "half_sine",
                    "amplitude": float(
                        resolve(modulation.amplitude, self.coordinates, "phase")
                    ),
                }
            elif isinstance(modulation, PolynomialPhase):
                event.params["phase_modulation"] = {
                    "kind": "polynomial",
                    "coefficients": [
                        float(resolve(value, self.coordinates, "phase"))
                        for value in modulation.coefficients
                    ],
                }
        return events, duration


class SpectrumScheduler(SpectrumPlaybackScheduler):
    """Preserve one explicit region while reusing the existing timing rules."""

    @property
    def ordered_frame_edges(self):
        return self._region is not None

    def __init__(self, *args, body):
        super().__init__(*args)
        self.config = self.setup.spectrum
        self.groups = self.config.groups
        self._region = None
        self._seen_groups = set()
        self._repeat_indices = ()
        self._paths = {}
        self._output_groups = {}
        self._dds_outputs = set()
        for group_name, group in self.groups.items():
            for output in group.outputs:
                if output in self._output_groups:
                    raise CompileError(
                        f"Spectrum output {output} belongs to two groups"
                    )
                self._output_groups[output] = group_name
                if group.cards[group.outputs[output].card].mode == "dds":
                    self._dds_outputs.add(output)
        self._signal_devices = {
            signal: self._devices_for(signal) for signal in self.mapping
        }
        self._signal_frames = {
            signal: self._frame_for(signal, require_declared=False)
            for signal in self.mapping
        }
        self.spatial = None
        if getattr(self.setup, "_transport", None) is not None or getattr(
            self.setup, "zone_coverage", {}
        ):
            from taiyidsl.compiler.spatial import SpatialState

            self.spatial = SpatialState(self)
        self._validate_tree(body)

    def _devices_for(self, signal):
        try:
            names = tuple(binding.device for binding in self.mapping.bindings(signal))
        except (TypeError, ValueError) as exc:
            raise CompileError(str(exc)) from exc
        for name in names:
            if name not in self.setup.devices:
                raise CompileError(f"Unknown mapped device {name}")
        return names

    def _frame_for(self, signal, *, require_declared=True):
        q, role = self.signal_qubit(signal)
        uid = q.uid if q else signal.partition(".")[0]
        declared = q is not None and role in q.drive_names
        default = (
            q.drive_frame(role)
            if declared
            else (uid if role in {"rf", "raman"} else None)
        )
        spectrum = any(
            name in self._output_groups for name in self._signal_devices[signal]
        )
        if signal in self.config.signal_frames:
            frame = self.config.signal_frames[signal]
            if not spectrum and frame != default:
                raise CompileError(
                    f"ARTIQ signal {signal} must retain its existing frame association"
                )
        elif declared or role in {"rf", "raman"}:
            frame = default
        elif spectrum and require_declared:
            raise CompileError(
                f"Spectrum signal {signal} requires an explicit signal_frames "
                "association, including None for an unassociated signal"
            )
        else:
            frame = None
        return frame

    def _check_group_signal(self, signal, group):
        devices = self._signal_devices.get(signal)
        if devices is None:
            self._devices_for(signal)  # Raise the same unmapped-signal diagnostic.
        if not devices or any(
            self._output_groups.get(name) != group for name in devices
        ):
            raise CompileError(f"Signal {signal} is outside Spectrum group {group}")

    def _validate_tree(
        self,
        nodes,
        *,
        group=None,
        path=(),
        outside_parallel=False,
        spatial_unordered=False,
    ):
        """Reject foreign operations even in inactive cases and zero repeats."""
        for index, node in enumerate(nodes):
            node_path = (*path, index)
            self._paths[id(node)] = node_path
            kind = node.kind
            if kind in {"play", "trigplay"} and isinstance(
                node.params["pulse"], SampledPulse
            ):
                devices = self._signal_devices.get(node.params["signal"], ())
                awg_rate = (
                    self.groups[group].sample_rate_hz
                    if (
                        group is not None
                        and devices
                        and all(
                            self._output_groups.get(name) == group for name in devices
                        )
                        and not self._dds_outputs.intersection(devices)
                    )
                    else None
                )
                validate_sampled_route(node, awg_rate)
            if kind in {"play", "trigplay"} and self._dds_outputs.intersection(
                self._signal_devices.get(node.params["signal"], ())
            ):
                raise CompileError(
                    "Ordinary play/trigplay cannot target native DDS transport outputs"
                )
            if kind in _FRAME_OPERATIONS:
                frame = node.params["qubit"] if kind == "rz" else node.params["frame"]
                _, devices = self._frame_targets(frame)
                if self._dds_outputs.intersection(devices):
                    raise CompileError(
                        "Qubit phase/frame operations cannot target native DDS "
                        "transport outputs"
                    )
            unordered = (
                spatial_unordered
                or kind == "parallel"
                or (kind == "serial" and node.params.get("schedule") == "asap")
            )
            if kind == "move":
                if self.spatial is None:
                    raise CompileError("move requires LabConfig.transport")
                if group != self.spatial.group:
                    raise CompileError(
                        "move requires its controller's triggered_sequence"
                    )
                if unordered:
                    raise CompileError(
                        "move requires serial scheduling, not parallel/ASAP"
                    )
            child_group = group
            if kind == "triggered_sequence":
                if group is not None:
                    raise CompileError("triggered_sequence blocks cannot be nested")
                child_group = node.params["group"]
                if node.params.get("phase_reference", "sequence") not in {
                    "sequence",
                    "shot",
                }:
                    raise CompileError(
                        "Spectrum phase_reference must be sequence or shot"
                    )
                if child_group not in self.groups:
                    raise CompileError(f"Unknown Spectrum group {child_group}")
                resolve_trigger_signal(
                    self.groups[child_group],
                    self.mapping,
                    self.setup,
                    node.params.get("trigger"),
                    source=node.source,
                )
            elif group is not None:
                if kind not in _REGION_OPERATIONS:
                    raise CompileError(
                        f"{kind} is not allowed inside a Spectrum triggered_sequence "
                        f"at {node.source}"
                    )
                if kind in {"play", "trigplay", "delay"}:
                    self._check_group_signal(node.params["signal"], group)
                if kind in {"play", "trigplay"}:
                    signal = node.params["signal"]
                    frame = self._frame_for(signal)
                    if frame is not None and frame not in self._frame_owners:
                        raise CompileError(
                            f"Unknown frame association {frame} for {signal}"
                        )
            elif kind in {"trigplay", "scope_delay"}:
                raise CompileError(f"{kind} requires an enclosing triggered_sequence")
            elif kind == "play":
                devices = self._signal_devices.get(node.params["signal"], ())
                if any(name in self._output_groups for name in devices):
                    raise CompileError(
                        "Spectrum outputs require trigplay inside triggered_sequence"
                    )
            if group is None and kind in _FRAME_OPERATIONS and outside_parallel:
                frame = node.params["qubit"] if kind == "rz" else node.params["frame"]
                _, devices = self._frame_targets(frame)
                if any(name in self._output_groups for name in devices):
                    raise CompileError(
                        "Spectrum phase commands outside triggered_sequence "
                        "require a serial realtime position"
                    )
            self._validate_tree(
                node.children,
                group=child_group,
                path=node_path,
                outside_parallel=outside_parallel
                or (group is None and kind == "parallel"),
                spatial_unordered=unordered,
            )

    def section_grid(self):
        """The active sample grid, or the native grid outside a triggered region."""
        if self._region is not None:
            return self._sample_grid(self._region[0])
        return native_factor(self.setup)

    def _sample_grid(self, group):
        value = 1 / (
            as_fraction(self.groups[group].sample_rate_hz, "frequency") * self.tick
        )
        if value.denominator != 1 or value <= 0:
            raise CompileError("Spectrum samples must be exact integer scheduler ticks")
        return int(value)

    def ticks(self, value, source, dimension="time", grid_mu=1):
        if self._region is None:
            return super().ticks(value, source, dimension, grid_mu)
        # A quantizing outer compile option cannot change the requested AWG timing.
        previous = self.quantize
        self.quantize = False
        try:
            return super().ticks(value, source, dimension, grid_mu)
        finally:
            self.quantize = previous

    def _tag(self, events):
        if self._region is not None:
            group, region_id = self._region
            for event in events:
                event.params.setdefault("route", "spectrum")
                event.params.setdefault("group", group)
                event.params.setdefault("region_id", region_id)
                event.params.setdefault("repeat_indices", list(self._repeat_indices))
        return events

    def _frame_targets(self, frame):
        signals = [
            s for s, associated in self._signal_frames.items() if associated == frame
        ]
        devices = sorted(
            {name for signal in signals for name in self._signal_devices[signal]}
        )
        return signals, devices

    def _frame_node(self, node, frames, realtime):
        frame = node.params["qubit"] if node.kind == "rz" else node.params["frame"]
        signals, devices = self._frame_targets(frame)
        logical_frame = False
        if self._region is None:
            if any(name in self._output_groups for name in devices):
                groups = {self._output_groups.get(name) for name in devices}
                if None in groups or len(groups) != 1:
                    raise CompileError(
                        "A phase command outside triggered_sequence requires a "
                        "Spectrum-only qubit associated with one group"
                    )
                logical_frame = True
        else:
            group, _ = self._region
            outside = [
                name for name in devices if self._output_groups.get(name) != group
            ]
            if outside:
                raise CompileError(
                    f"Frame {frame} targets outputs outside Spectrum group {group}: "
                    f"{', '.join(outside)}; no implicit ARTIQ frame update is allowed"
                )
            if not devices:
                raise CompileError(f"Frame {frame} has no mapped Spectrum outputs")
        events, duration = super().node(node, frames, realtime)
        if self._region is not None or logical_frame:
            for event in events:
                event.params.update(target_devices=devices, target_signals=signals)
                if logical_frame:
                    event.params.update(route="logical_frame", group=next(iter(groups)))
        return self._tag(events), duration

    def _play_node(self, node, frames, realtime):
        if self._region is None:
            raise CompileError("trigplay requires an enclosing triggered_sequence")
        events, duration = self.schedule_play(node, frames, realtime)
        return self._tag(events), duration

    def _repeat_node(self, node, frames, realtime):
        count = resolve(node.params["count"], self.coordinates, "dimensionless")
        if count.denominator != 1 or count < 0:
            raise CompileError(
                f"Repeat requires a nonnegative integer at {node.source}"
            )
        if count > self.max_events:
            raise CompileError("Static repeat count exceeds compilation budget")
        events, cursor = [], 0
        previous = self._repeat_indices
        origin = self._time_origin
        try:
            for index in range(int(count)):
                self._time_origin = origin + cursor
                self._repeat_indices = (*previous, index)
                new, duration = self.serial(node.children, frames, realtime)
                events.extend(
                    replace(event, start_mu=event.start_mu + cursor) for event in new
                )
                cursor += duration
                if len(events) > self.max_events:
                    raise CompileError("Static repeat exceeds per-point event budget")
        finally:
            self._repeat_indices = previous
            self._time_origin = origin
        return events, cursor

    def node(self, node, frames, realtime):
        kind = node.kind
        if kind == "move":
            if self.spatial is None or self._region is None:
                raise CompileError(
                    "move requires a configured Spectrum transport region"
                )
            event = self.spatial.move(node)
            return self._tag([event]), event.duration_mu
        if kind in {"native_timed", "native_delay"}:
            if not realtime or self._region is not None:
                raise CompileError("Native timed prelude requires outer realtime")
            native_grid = (
                as_fraction(self.config.artiq_tick_seconds, "time") / self.tick
            )
            duration = self.ticks(
                node.params["duration"], node.source, grid_mu=int(native_grid)
            )
            params = {
                key: value for key, value in node.params.items() if key != "duration"
            }
            params.update(route="native_artiq", native_output="opaque_not_simulated")
            return [
                ScheduledEvent(
                    kind,
                    node.params.get("method", "native_timeline"),
                    0,
                    duration,
                    params,
                    node.source,
                )
            ], duration
        if kind == "triggered_sequence":
            if not realtime:
                raise CompileError("triggered_sequence must be inside realtime")
            group = node.params["group"]
            if group in self._seen_groups:
                raise CompileError(
                    "Only one triggered sequence region per group and shot is "
                    f"supported: {group}"
                )
            self._seen_groups.add(group)
            path = self._paths[id(node)]
            region_id = f"{group}:" + ".".join(str(index) for index in path)
            self._region = (group, region_id)
            try:
                events, duration = self.serial(node.children, frames, realtime)
            finally:
                self._region = None
            descriptor = ScheduledEvent(
                "spectrum_region",
                group,
                0,
                duration,
                {
                    "group": group,
                    "region_id": region_id,
                    "source_path": list(path),
                    "repeat_indices": list(self._repeat_indices),
                    "phase_reference": node.params.get("phase_reference", "sequence"),
                    "frame_ids": sorted(
                        {
                            frame
                            for signal, frame in self._signal_frames.items()
                            if frame in self._frame_owners
                            and any(
                                self._output_groups.get(device) == group
                                for device in self._signal_devices[signal]
                            )
                        }
                    ),
                },
                node.source,
                tuple(sorted(self.groups[group].outputs)),
            )
            if self.spatial is not None and self.spatial.group == group:
                descriptor.params["transport_initial_tones"] = (
                    self.spatial.initial_tones
                )
                descriptor.params["transport_card"] = self.spatial.config.card
            trigger_signal = resolve_trigger_signal(
                self.groups[group],
                self.mapping,
                self.setup,
                node.params.get("trigger"),
                source=node.source,
            )
            if trigger_signal is not None:
                descriptor.params["trigger_signal"] = trigger_signal
                for event in events:
                    event.params["trigger_signal"] = trigger_signal
            return [descriptor, *events], duration
        if kind == "trigplay" or (kind == "play" and self._region is not None):
            return self._play_node(node, frames, realtime)
        if kind in _FRAME_OPERATIONS:
            return self._frame_node(node, frames, realtime)
        if kind == "repeat":
            return self._repeat_node(node, frames, realtime)
        if kind == "scope_delay":
            if self._region is None:
                raise CompileError(
                    "scope_delay requires an enclosing triggered_sequence"
                )
            group, _ = self._region
            duration = self.ticks(
                node.params["duration"], node.source, grid_mu=self._sample_grid(group)
            )
            event = ScheduledEvent(
                "scope_delay",
                group,
                0,
                duration,
                {
                    "requested_duration_s": float(
                        resolve(node.params["duration"], self.coordinates, "time")
                    )
                },
                node.source,
            )
            return self._tag([event]), duration
        events, duration = super().node(node, frames, realtime)
        if self._region is not None and kind == "delay":
            for event in events:
                event.resources = self._signal_devices[event.signal]
        return self._tag(events), duration

    def validate_regions(self, events):
        regions = {
            event.params["region_id"]: event
            for event in events
            if event.kind == "spectrum_region"
        }
        occupied = {}
        for event in events:
            region_id = event.params.get("region_id")
            if region_id is None or event.kind == "spectrum_region":
                continue
            region = regions[region_id]
            grid = self._sample_grid(region.signal)
            if (event.start_mu - region.start_mu) % grid or event.duration_mu % grid:
                raise CompileError(
                    f"Spectrum event violates the sample grid at {event.source}"
                )
            if event.kind not in {"play", "delay"} or not event.duration_mu:
                continue
            for resource in event.resources:
                occupied.setdefault(resource, []).append(event)
        for resource, intervals in occupied.items():
            previous = None
            for event in sorted(intervals, key=lambda item: item.start_mu):
                if previous is not None and event.start_mu < previous.end_mu:
                    raise CompileError(
                        f"Conflicting overlapping Spectrum operations on {resource}: "
                        f"{previous.source} and {event.source}"
                    )
                previous = event


def _snapshot_spectrum_frames(events):
    """Record already resolved F; keep Rz history only as source diagnostics."""
    phases, virtual = {}, {}
    regions = {
        event.params["region_id"]: event
        for event in events
        if event.kind == "spectrum_region"
    }
    for region in regions.values():
        region.params["terminal_frames"] = {}

    def snapshot(frame):
        value = phases.get(frame, 0.0)
        return {
            "frame_phase": value,
            # Legacy H-V readers retain F. This is source history, not a second frame.
            "hardware_phase": value + virtual.get(frame, 0.0),
            "effective_phase": value,
            "virtual_z": virtual.get(frame, 0.0),
        }

    for event in sorted(events, key=lambda item: item.start_mu):
        frame = None
        if event.kind == "spectrum_region":
            event.params["entry_frames"] = {
                frame: snapshot(frame) for frame in event.params.get("frame_ids", ())
            }
            event.params["terminal_frames"] = {
                frame: dict(value)
                for frame, value in event.params["entry_frames"].items()
            }
        elif event.kind in _FRAME_OPERATIONS:
            frame = event.signal
            phases[frame] = event.params["phase"]
            virtual[frame] = event.params.get("accumulated_virtual_z", 0.0)
            if not math.isfinite(phases[frame]):
                raise CompileError("Accumulated Spectrum frame must remain finite")
            if event.params.get("route") == "spectrum":
                event.params["effective_frame_phase"] = phases[frame]
        elif event.kind == "play" and event.params.get("route") == "spectrum":
            frame = event.params["frame"]
        if frame is not None and event.params.get("route") == "spectrum":
            regions[event.params["region_id"]].params["terminal_frames"][frame] = (
                snapshot(frame)
            )
    return events


def schedule_spectrum(
    experiment,
    body,
    setup,
    mapping,
    coordinates,
    allow_quantization,
    diagnostics,
    max_events,
    backend,
):
    """Return logical events and duration; no Spectrum driver is imported."""
    scheduler = SpectrumScheduler(
        experiment,
        setup,
        coordinates,
        allow_quantization,
        diagnostics,
        max_events,
        mapping,
        backend,
        body=body,
    )
    events, duration = scheduler.serial(body, {uid: 0 for uid in experiment.qubits})
    if scheduler.spatial is not None:
        scheduler.spatial.finish()
    scheduler.validate_regions(events)
    events = resolve_frames(events)
    return _snapshot_spectrum_frames(events), duration
