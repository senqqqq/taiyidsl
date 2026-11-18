"""Shared static scheduling for backends with an integer target timebase."""

import math
from bisect import bisect_left
from dataclasses import replace

from ..dsl.pulses import FlatTopGaussian
from ..dsl.values import resolve
from ..ir import CompileError, ScheduledEvent
from ..units import as_fraction
from .timebase import native_factor


def _qubit_drive(event):
    uid = event.params.get("qubit")
    return (
        event.kind == "play"
        and uid is not None
        and (
            event.params["frame"] is not None
            if "frame" in event.params
            else event.signal in {f"{uid}.raman", f"{uid}.rf"}
        )
    )


def resolve_virtual_z(logical):
    """Compatibility entry point for the unified, idempotent frame resolver."""
    return resolve_frames(logical)


def resolve_frames(logical):
    """Resolve one persistent frame F after final scheduling.

    shift_phase adds its angle; Rz subtracts its angle. Both resets clear F.
    Rz contributions are retained only as provenance, never subtracted again.
    reset_phase also establishes a new carrier reference for physical lowerers.
    Stable ordering preserves zero-duration causal updates at a shared time.
    """
    ordered = sorted(logical, key=lambda event: event.start_mu)
    starts, ends = {}, {}
    for event in ordered:
        if not _qubit_drive(event) or not event.duration_mu:
            continue
        uid = event.params.get("frame", event.params.get("qubit"))
        if uid is None:
            continue
        starts.setdefault(uid, []).append(event.start_mu)
        previous = ends.setdefault(uid, [])
        previous.append(max(event.end_mu, previous[-1] if previous else 0))
    frames, virtual, references = {}, {}, {}
    has_virtual = any(event.kind == "rz" for event in ordered)
    for event in ordered:
        if event.kind in {"rz", "shift_phase", "reset_frame", "reset_phase"}:
            uid = event.signal
            if event.kind in {"rz", "reset_phase"}:
                index = bisect_left(starts.get(uid, ()), event.start_mu) - 1
                if index >= 0 and ends[uid][index] > event.start_mu:
                    label = "Virtual Z" if event.kind == "rz" else "reset_phase"
                    raise CompileError(
                        f"{label} cannot occur inside an active qubit pulse"
                    )
            previous = frames.get(uid, 0.0)
            if event.kind == "rz":
                angle = event.params["angle"]
                value = previous - angle
                virtual[uid] = virtual.get(uid, 0.0) + angle
                event.params["convention"] = (
                    "Rz(theta)=exp(-i*theta*Z/2); drive frame increment=-theta"
                )
            elif event.kind == "shift_phase":
                angle = event.params.get(
                    "angle",
                    event.params.get("phase", 0.0)
                    - event.params.get("previous_phase", 0.0),
                )
                event.params["angle"] = angle
                value = previous + angle
            else:
                value = 0.0
                virtual[uid] = 0.0
                if event.kind == "reset_phase":
                    references[uid] = event.start_mu
            if not all(math.isfinite(v) for v in (value, virtual.get(uid, 0.0))):
                raise CompileError("Accumulated frame phase must remain finite")
            frames[uid] = value
            event.params.update(
                phase=value,
                previous_phase=previous,
                accumulated_virtual_z=virtual.get(uid, 0.0),
                drive_phase_offset=-virtual.get(uid, 0.0),
            )
            event.params.pop("carrier_reference_mu", None)
            if uid in references:
                event.params["carrier_reference_mu"] = references[uid]
        elif event.kind == "play":
            uid = event.params.get("frame", event.params.get("qubit"))
            value = frames.get(uid, 0.0) if _qubit_drive(event) else 0.0
            # Idempotent: phase solving may be repeated after structural scheduling.
            base = event.params.get(
                "requested_phase",
                event.params["phase"] - event.params.get("frame_phase", 0.0),
            )
            event.params.update(
                requested_phase=base, phase=base + value, frame_phase=value
            )
            if has_virtual and _qubit_drive(event):
                event.params["virtual_z_phase"] = virtual.get(uid, 0.0)
                event.params["virtual_z_drive_offset"] = -virtual.get(uid, 0.0)
            event.params.pop("carrier_reference_mu", None)
            if _qubit_drive(event) and uid in references:
                event.params["carrier_reference_mu"] = references[uid]
    return logical


class Scheduler:
    def __init__(
        self,
        exp,
        setup,
        coordinates,
        quantize,
        diagnostics,
        max_events,
        mapping,
        backend,
    ):
        self.exp, self.setup, self.coordinates = exp, setup, coordinates
        self.quantize, self.diagnostics = quantize, diagnostics
        self.tick = as_fraction(setup.tick_seconds, "time")
        self.max_events = max_events
        self.mapping = mapping
        self.backend = backend
        self._time_origin = 0
        self._signal_qubits = {}
        for q in exp.qubits.values():
            for role, signal in q.signals.items():
                if signal in self._signal_qubits:
                    raise CompileError(
                        f"Logical signal {signal} has multiple Qubit owners"
                    )
                self._signal_qubits[signal] = (q.uid, role)
        self._frame_owners = {}
        for q in exp.qubits.values():
            names = {q.frame, *(q.drive_frame(role) for role in q.drive_names)} - {None}
            for name in names:
                if name in self._frame_owners and self._frame_owners[name] != q.uid:
                    raise CompileError(f"Ambiguous control frame identifier {name}")
                self._frame_owners[name] = q.uid
        self._signal_frames = {}
        for signal in mapping:
            q, role = self.signal_qubit(signal)
            self._signal_frames[signal] = (
                q.drive_frame(role) if q and role in q.drive_names else None
            )

    @property
    def time_origin(self):
        """Absolute origin of the relative subtree currently being scheduled."""
        return self._time_origin

    def section_grid(self):
        """Preserve the configured native grid even on a pure ARTIQ target."""
        if getattr(self.setup, "spectrum", None) is not None:
            return native_factor(self.setup)
        return 1

    def signal_frame(self, signal):
        """Resolve the live signal association, including backend aliases."""
        if signal in self._signal_frames:
            return self._signal_frames[signal]
        qubit, role = self.signal_qubit(signal)
        return qubit.drive_frame(role) if qubit and role in qubit.drive_names else None

    def frame_owner(self, frame, default=None):
        """Return the qubit owning a frame, or the caller's explicit fallback."""
        return self._frame_owners.get(frame, default)

    def signal_qubit(self, signal):
        """Resolve explicit signal roles before legacy dotted-name inference."""
        if signal in self._signal_qubits:
            uid, role = self._signal_qubits[signal]
            return self.exp.qubits[uid], role
        uid, _, role = signal.partition(".")
        q = self.exp.qubits.get(uid)
        return (q if q is not None and not q._unified else None), role

    def frame_initial_states(self, frame):
        uid = self._frame_owners.get(frame)
        if uid is None:
            raise CompileError(f"Unknown frame {frame}")
        q = self.exp.qubits[uid]
        return {
            q.signal(role): {
                "frequency": q.drive(role).frequency,
                "amplitude": q.drive(role).amplitude
                if q.drive(role).amplitude is not None
                else 0.0,
            }
            for role in q.drive_names
            if q.drive_frame(role) == frame and q.drive(role).frequency is not None
        }

    def signal_grid(self, signal):
        return self.backend.signal_grid(signal, self.setup, self.mapping)

    def ticks(self, value, source, dimension="time", grid_mu=1):
        t = resolve(value, self.coordinates, dimension)
        if t < 0:
            raise CompileError(f"Negative duration at {source}")
        raw = t / (self.tick * grid_mu)
        nearest = round(raw)
        # Remove only floating representation noise, never a meaningful
        # fraction of a tick.
        if raw.denominator == 1 or abs(float(raw - nearest)) < 1e-8:
            return nearest * grid_mu
        if not self.quantize:
            raise CompileError(
                f"Duration {float(t)} s is not representable on "
                f"{grid_mu} mu timing grid at {source}"
            )
        if t > 0 and nearest == 0:
            raise CompileError("Quantization would erase a nonzero operation")
        self.diagnostics.append(
            {
                "kind": "time_quantization",
                "source": source,
                "requested_s": float(t),
                "actual_s": float(nearest * self.tick * grid_mu),
                "grid_mu": grid_mu,
            }
        )
        return nearest * grid_mu

    def serial(self, nodes, frames, realtime=False):
        events, cursor = [], 0
        origin = self._time_origin
        try:
            for node in nodes:
                self._time_origin = origin + cursor
                new, duration = self.node(node, frames, realtime)
                events.extend(replace(e, start_mu=e.start_mu + cursor) for e in new)
                cursor += duration
                if len(events) > self.max_events:
                    raise CompileError(
                        "Per-point event budget exceeded; shorten static repeats"
                    )
        finally:
            self._time_origin = origin
        return events, cursor

    def node(self, node, frames, realtime):
        p, k = node.params, node.kind
        if k == "case":
            parameter = p["parameter"]
            if parameter.name not in self.coordinates:
                raise CompileError(f"Unbound case parameter {parameter.name}")
            return (
                self.serial(node.children, frames, realtime)
                if self.coordinates[parameter.name] == p["value"]
                else ([], 0)
            )
        if k in {"serial", "realtime"}:
            if p.get("schedule") == "asap":
                from .section_schedule import schedule_asap

                events, duration = schedule_asap(self, node, frames, realtime)
            else:
                events, duration = self.serial(
                    node.children, frames, realtime or k == "realtime"
                )
            if p.get("operation_name"):
                targets = p.get("operation_qubits", ())
                unknown = set(targets) - set(self.exp.qubits)
                if unknown:
                    raise CompileError(f"Operation refers to unknown qubits: {unknown}")
                for event in events:
                    event.params.setdefault("operation_path", []).insert(
                        0, p["operation_name"]
                    )
            if p.get("exact_duration") is not None:
                expected = self.ticks(p["exact_duration"], node.source)
                if duration != expected:
                    raise CompileError(
                        f"exact_duration requires {expected} mu, "
                        f"got {duration} at {node.source}"
                    )
            return events, duration
        if k in {"barrier", "section_delay"}:
            if not realtime:
                raise CompileError(f"{k} requires realtime")
            duration = (
                self.ticks(p["duration"], node.source) if k == "section_delay" else 0
            )
            if k == "barrier":
                unknown = set(p.get("qubits", ())) - set(self.exp.qubits)
                if unknown:
                    raise CompileError(f"Unknown barrier qubits: {unknown}")
            # Pure structure: barriers/delays have no output for lowerers to emit.
            return [], duration
        if k == "repeat":
            count = resolve(p["count"], self.coordinates, "dimensionless")
            if count.denominator != 1 or count < 0:
                raise CompileError(
                    f"Repeat requires a nonnegative integer at {node.source}"
                )
            if count > self.max_events:
                raise CompileError("Static repeat count exceeds compilation budget")
            events, cursor = [], 0
            origin = self._time_origin
            try:
                for _ in range(int(count)):
                    self._time_origin = origin + cursor
                    new, duration = self.serial(node.children, frames, realtime)
                    events.extend(replace(e, start_mu=e.start_mu + cursor) for e in new)
                    cursor += duration
                    if len(events) > self.max_events:
                        raise CompileError(
                            "Static repeat exceeds per-point event budget"
                        )
            finally:
                self._time_origin = origin
            return events, cursor
        if k == "parallel":
            branches, writes, accesses, virtual_writes = [], [], [], []
            layer_resources = []
            for child in node.children:
                local = dict(frames)
                ev, dur = self.node(child, local, realtime)
                if "layer_qubits" in p:
                    from .resources import node_resources

                    resources = node_resources(self, child, ev)
                    targets = {name for kind, name in resources if kind == "qubit"}
                    if not targets <= set(p["layer_qubits"]):
                        raise CompileError("Layer branch accesses an undeclared qubit")
                    if any(resources & previous for previous in layer_resources):
                        raise CompileError(
                            "Layer branches share a qubit or physical resource"
                        )
                    layer_resources.append(resources)
                write = {
                    e.signal
                    for e in ev
                    if e.kind in {"shift_phase", "reset_frame", "reset_phase", "rz"}
                }
                access = write | {
                    e.params.get("frame", e.params["qubit"])
                    for e in ev
                    if _qubit_drive(e)
                }
                branches.append((ev, dur, local))
                writes.append(write)
                accesses.append(access)
                virtual_writes.append(
                    {e.signal for e in ev if e.kind in {"rz", "reset_phase"}}
                )
            for i in range(len(branches)):
                if any(
                    virtual_writes[i] & accesses[j]
                    for j in range(len(branches))
                    if i != j
                ):
                    raise CompileError(
                        "Virtual Z and same-qubit operations require one serial branch"
                    )
                if any(writes[i] & writes[j] for j in range(len(branches)) if i != j):
                    raise CompileError(
                        "A frame cannot be written by multiple parallel branches"
                    )
            duration = max((d for _, d, _ in branches), default=0)
            if p.get("exact_duration") is not None:
                if duration != self.ticks(p["exact_duration"], node.source):
                    raise CompileError("Parallel block does not satisfy exact_duration")
            # Explicit parallel has no cross-branch causal order at one instant.
            # Mid-pulse frame changes remain valid; a start-boundary change must be
            # written in the pulse's serial branch to define which phase it sees.
            frame_edges, drive_edges = [], []
            for ev, dur, _ in branches:
                offset = duration - dur if p["align"] == "end" else 0
                frame_edges.append(
                    {
                        (e.signal, e.start_mu + offset)
                        for e in ev
                        if e.kind in {"shift_phase", "reset_frame", "reset_phase"}
                    }
                )
                drive_edges.append(
                    {
                        (e.params.get("frame", e.params["qubit"]), e.start_mu + offset)
                        for e in ev
                        if _qubit_drive(e) and e.duration_mu
                    }
                )
            for i, edges in enumerate(frame_edges):
                if getattr(self, "_region", None) is not None and any(
                    edges & starts for j, starts in enumerate(drive_edges) if i != j
                ):
                    raise CompileError(
                        "Hardware frame update and pulse start at the same time "
                        "require one serial branch"
                    )
            events = []
            for i, (ev, dur, local) in enumerate(branches):
                offset = duration - dur if p["align"] == "end" else 0
                events.extend(replace(e, start_mu=e.start_mu + offset) for e in ev)
                for frame in writes[i]:
                    frames[frame] = local[frame]
            return events, duration
        if k in {"average", "sweep"}:
            raise CompileError(
                "Sweeps must form a single outer chain inside the average block"
            )
        if k not in {"prepare"} and not realtime:
            raise CompileError(f"{k} must be inside a realtime block at {node.source}")
        if k == "rz":
            uid = p["qubit"]
            if uid not in self.exp.qubits:
                raise CompileError(f"Unknown qubit {uid}")
            angle = float(resolve(p["angle"], self.coordinates, "phase"))
            frames[uid] = frames.get(uid, 0.0) - angle
            return [
                ScheduledEvent(
                    "rz",
                    uid,
                    0,
                    0,
                    {
                        "angle": angle,
                        "initial_states": self.frame_initial_states(uid),
                    },
                    node.source,
                )
            ], 0
        if k in {"shift_phase", "reset_frame", "reset_phase"}:
            frame = p["frame"]
            if frame not in self._frame_owners:
                raise CompileError(f"Unknown frame {frame}")
            angle = (
                float(resolve(p["angle"], self.coordinates, "phase"))
                if k == "shift_phase"
                else 0
            )
            previous_phase = frames.get(frame, 0)
            frames[frame] = (previous_phase + angle) if k == "shift_phase" else 0
            return [
                ScheduledEvent(
                    k,
                    frame,
                    0,
                    0,
                    {
                        "angle": angle,
                        "phase": frames[frame],
                        "previous_phase": previous_phase,
                        "initial_states": self.frame_initial_states(frame),
                    },
                    node.source,
                )
            ], 0
        if k in {"prepare", "measure"}:
            uid = p["qubit"]
            if uid not in self.exp.qubits:
                raise CompileError(f"Unknown qubit {uid}")
            if p["protocol"] not in self.setup.protocols:
                raise CompileError(
                    f"Unknown protocol {p['protocol']}; define it in DeviceSetup"
                )
            protocol = self.setup.protocols[p["protocol"]]
            ev, duration = [], 0
            for step in protocol.steps:
                signal = step.signal.format(qubit=uid)
                grid = self.signal_grid(signal)
                start = self.ticks(step.offset, node.source, grid_mu=grid)
                value = (
                    step.duration
                    if step.duration is not None
                    else self.exp.qubits[uid].params.readout.duration
                )
                length = self.ticks(value, node.source, grid_mu=grid)
                if length <= 0:
                    raise CompileError("Protocol trigger duration must be nonzero")
                ev.append(
                    ScheduledEvent(
                        "trigger",
                        signal,
                        start,
                        length,
                        {"protocol": p["protocol"], "qubit": uid},
                        node.source,
                    )
                )
                duration = max(duration, start + length)
            ev.append(
                ScheduledEvent(
                    k,
                    f"{uid}.readout" if k == "measure" else f"{uid}.prepare",
                    0,
                    duration,
                    dict(p),
                    node.source,
                )
            )
            return ev, duration
        if k == "trig":
            duration = self.ticks(
                p["duration"], node.source, grid_mu=self.signal_grid(p["signal"])
            )
            if duration <= 0:
                raise CompileError("Trigger width must be nonzero")
            return [
                ScheduledEvent("trig", p["signal"], 0, duration, {}, node.source)
            ], duration
        if k == "acquire":
            if set(p["qubits"]) - set(self.exp.qubits):
                raise CompileError("Acquisition refers to unknown qubits")
            duration = self.ticks(
                p["duration"], node.source, grid_mu=self.signal_grid(p["signal"])
            )
            if duration <= 0:
                raise CompileError("Acquisition trigger duration must be nonzero")
            return [
                ScheduledEvent(
                    "trigger",
                    p["signal"],
                    0,
                    duration,
                    {"handle": p["handle"]},
                    node.source,
                ),
                ScheduledEvent(
                    "acquire",
                    p["signal"],
                    0,
                    duration,
                    {
                        "signal": p["signal"],
                        "handle": p["handle"],
                        "qubits": p["qubits"],
                        "metadata": p["metadata"],
                        "trigger_duration_s": float(duration * self.tick),
                    },
                    node.source,
                ),
            ], duration
        if k in {"delay", "reserve"}:
            duration = self.ticks(
                p["duration"], node.source, grid_mu=self.signal_grid(p["signal"])
            )
            return [
                ScheduledEvent(
                    k,
                    p["signal"],
                    0,
                    duration,
                    {
                        "requested_duration_s": float(
                            resolve(p["duration"], self.coordinates, "time")
                        )
                    },
                    node.source,
                )
            ], duration
        if k == "play":
            pulse, signal = p["pulse"], p["signal"]
            if (
                pulse.phase_modulation is not None
                or getattr(pulse, "phase_modulation_mode", "local") != "local"
            ):
                raise CompileError("Phase modulation requires Spectrum trigplay")
            q, role = self.signal_qubit(signal)
            uid = q.uid if q else None
            defaults = q.drive(role) if q and role in q.drive_names else None
            waveform = {}
            if isinstance(pulse, FlatTopGaussian):
                flat = resolve(pulse.flat_duration, self.coordinates, "time")
                sigma = as_fraction(pulse.sigma, "time")
                cutoff = as_fraction(pulse.cutoff_sigma, "dimensionless")
                requested_duration = flat + 2 * cutoff * sigma
                waveform = {
                    "shape": "flat_top_gaussian",
                    "flat_duration_s": float(flat),
                    "sigma_s": float(sigma),
                    "cutoff_sigma": float(cutoff),
                    "sample_interval_s": float(
                        as_fraction(pulse.sample_interval, "time")
                    ),
                }
            else:
                requested_duration = resolve(pulse.duration, self.coordinates, "time")
            duration = self.ticks(
                requested_duration, node.source, grid_mu=self.signal_grid(signal)
            )
            amplitude = (
                pulse.amplitude
                if pulse.amplitude is not None
                else (defaults.amplitude if defaults else 1)
            )
            frequency = (
                pulse.frequency
                if pulse.frequency is not None
                else (defaults.frequency if defaults else 0)
            )
            if amplitude is None or frequency is None:
                raise CompileError(
                    f"Signal {signal} requires explicit pulse amplitude/frequency "
                    "or declared Qubit defaults"
                )
            amp = float(resolve(amplitude, self.coordinates, "dimensionless"))
            freq = float(
                resolve(frequency, self.coordinates, "frequency")
                + resolve(p.get("detuning", 0), self.coordinates, "frequency")
            )
            requested_phase = float(resolve(pulse.phase, self.coordinates, "phase"))
            frame = self._signal_frames.get(signal)
            phase = requested_phase + frames.get(frame, 0)
            if not 0 <= amp <= 1 or freq < 0:
                raise CompileError(
                    "Pulse amplitude must be 0..1 and frequency nonnegative"
                )
            params = {
                "amplitude": amp,
                "frequency": freq,
                "phase": phase,
                "requested_phase": requested_phase,
                "requested_duration_s": float(requested_duration),
                "qubit": uid if q else None,
                "frame_phase": frames.get(frame, 0),
                **waveform,
            }
            if "global_operation" in p:
                params["global_operation"] = p["global_operation"]
                params["operation_qubits"] = p["operation_qubits"]
            if (
                q
                and role in q.drive_names
                and (frame != uid or role not in {"rf", "raman"})
            ):
                params["frame"] = frame
            return [
                ScheduledEvent("play", signal, 0, duration, params, node.source)
            ], duration
        raise CompileError(f"Unsupported operation {k}")
