"""Static scheduling and conservative DDS/TTL physical lowering."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from itertools import product
from pathlib import Path

from .dsl import json_value, resolve
from .hardware import DeviceCalibration, DeviceSetup, SignalMap
from .units import as_fraction


class CompileError(ValueError):
    pass


@dataclass
class ScheduledEvent:
    kind: str
    signal: str
    start_mu: int
    duration_mu: int
    params: dict = field(default_factory=dict)
    source: str = ""
    resources: tuple = ()
    logical_ids: tuple = ()

    @property
    def end_mu(self):
        return self.start_mu + self.duration_mu


@dataclass
class CompiledPoint:
    index: int
    coordinates: dict
    axis_indices: tuple
    logical_events: list[ScheduledEvent]
    physical_events: list[ScheduledEvent]
    duration_mu: int
    origin_mu: int


@dataclass
class CompiledExperiment:
    name: str
    points: list[CompiledPoint]
    shots: int
    averaging_mode: str
    axes: list[dict]
    qubits: dict
    setup: DeviceSetup
    signal_map: SignalMap
    metadata: dict
    provenance: dict
    diagnostics: list
    ir: dict
    _integrity_hash: str = field(init=False, repr=False)

    def __post_init__(self):
        self._integrity_hash = self._checksum()

    def _checksum(self):
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    def assert_integrity(self):
        if self._checksum() != self._integrity_hash:
            raise CompileError(
                "Compiled artifact was modified after compilation; "
                "rebuild it from the experiment"
            )

    def execution_order(self):
        self.assert_integrity()
        if self.averaging_mode == "cyclic":
            return ((p.index, s) for s in range(self.shots) for p in self.points)
        return ((p.index, s) for p in self.points for s in range(self.shots))

    def to_dict(self):
        return {
            "schema_version": 1,
            "name": self.name,
            "shots": self.shots,
            "averaging_mode": self.averaging_mode,
            "axes": self.axes,
            "points": [asdict(p) for p in self.points],
            "qubits": {k: q.to_dict() for k, q in self.qubits.items()},
            "setup": self.setup.to_dict(),
            "signal_map": self.signal_map.to_dict(),
            "metadata": json_value(self.metadata),
            "provenance": self.provenance,
            "diagnostics": self.diagnostics,
            "ir": self.ir,
        }

    def save(self, path):
        self.assert_integrity()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return path

    def visualize(self, path=None):
        self.assert_integrity()
        from .visualization import visualize

        return visualize(self, path)

    def export_artiq(self, output_dir, *, mode="auto"):
        self.assert_integrity()
        from .artiq import export_artiq

        return export_artiq(self, output_dir, mode=mode)


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.children)


class _Scheduler:
    def __init__(
        self, exp, setup, coordinates, quantize, diagnostics, max_events, mapping
    ):
        self.exp, self.setup, self.coordinates = exp, setup, coordinates
        self.quantize, self.diagnostics = quantize, diagnostics
        self.tick = as_fraction(setup.tick_seconds, "time")
        self.max_events = max_events
        self.mapping = mapping

    def signal_grid(self, signal):
        if signal not in self.mapping:
            return 1
        devices = [
            self.setup.devices[b.device]
            for b in self.mapping.bindings(signal)
            if b.device in self.setup.devices
        ]
        return math.lcm(*(d.grid_mu for d in devices)) if devices else 1

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
        for node in nodes:
            new, duration = self.node(node, frames, realtime)
            events.extend(replace(e, start_mu=e.start_mu + cursor) for e in new)
            cursor += duration
            if len(events) > self.max_events:
                raise CompileError(
                    "Per-point event budget exceeded; shorten static repeats"
                )
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
            events, duration = self.serial(
                node.children, frames, realtime or k == "realtime"
            )
            if p.get("exact_duration") is not None:
                expected = self.ticks(p["exact_duration"], node.source)
                if duration != expected:
                    raise CompileError(
                        f"exact_duration requires {expected} mu, "
                        f"got {duration} at {node.source}"
                    )
            return events, duration
        if k == "repeat":
            if p["count"] > self.max_events:
                raise CompileError("Static repeat count exceeds compilation budget")
            events, cursor = [], 0
            for _ in range(p["count"]):
                new, duration = self.serial(node.children, frames, realtime)
                events.extend(replace(e, start_mu=e.start_mu + cursor) for e in new)
                cursor += duration
                if len(events) > self.max_events:
                    raise CompileError("Static repeat exceeds per-point event budget")
            return events, cursor
        if k == "parallel":
            branches, writes, accesses = [], [], []
            for child in node.children:
                local = dict(frames)
                ev, dur = self.node(child, local, realtime)
                write = {
                    e.signal for e in ev if e.kind in {"shift_phase", "reset_frame"}
                }
                access = write | {
                    e.params["qubit"]
                    for e in ev
                    if e.kind == "play" and e.params.get("qubit")
                }
                branches.append((ev, dur, local))
                writes.append(write)
                accesses.append(access)
            for i in range(len(branches)):
                if any(writes[i] & accesses[j] for j in range(len(branches)) if i != j):
                    raise CompileError(
                        "A frame written in one parallel branch is accessed by another"
                    )
            duration = max((d for _, d, _ in branches), default=0)
            if p.get("exact_duration") is not None:
                if duration != self.ticks(p["exact_duration"], node.source):
                    raise CompileError("Parallel block does not satisfy exact_duration")
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
        if k in {"shift_phase", "reset_frame"}:
            frame = p["frame"]
            if frame not in self.exp.qubits:
                raise CompileError(f"Unknown frame {frame}")
            angle = (
                float(resolve(p["angle"], self.coordinates, "phase"))
                if k == "shift_phase"
                else 0
            )
            frames[frame] = (frames.get(frame, 0) + angle) if k == "shift_phase" else 0
            return [
                ScheduledEvent(k, frame, 0, 0, {"phase": frames[frame]}, node.source)
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
            uid, _, role = signal.partition(".")
            q = self.exp.qubits.get(uid)
            defaults = (
                getattr(q.params, role, None) if q and role in {"raman", "rf"} else None
            )
            duration = self.ticks(
                pulse.duration, node.source, grid_mu=self.signal_grid(signal)
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
            amp = float(resolve(amplitude, self.coordinates, "dimensionless"))
            freq = float(resolve(frequency, self.coordinates, "frequency"))
            phase = float(resolve(pulse.phase, self.coordinates, "phase")) + frames.get(
                uid, 0
            )
            if not 0 <= amp <= 1 or freq < 0:
                raise CompileError(
                    "Pulse amplitude must be 0..1 and frequency nonnegative"
                )
            params = {
                "amplitude": amp,
                "frequency": freq,
                "phase": phase,
                "requested_duration_s": float(
                    resolve(pulse.duration, self.coordinates, "time")
                ),
                "qubit": uid if q else None,
            }
            return [
                ScheduledEvent("play", signal, 0, duration, params, node.source)
            ], duration
        raise CompileError(f"Unsupported operation {k}")


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def _check_intervals(intervals):
    by_resource = {}
    for start, end, resources, source in intervals:
        if start == end:
            continue
        for resource in resources:
            by_resource.setdefault(resource, []).append((start, end, source))
    for resource, spans in by_resource.items():
        spans.sort(key=lambda x: (x[0], x[1]))
        for a, b in zip(spans, spans[1:]):
            if _overlap(a, b):
                raise CompileError(
                    f"Resource conflict on {resource}: {a[2]} overlaps {b[2]}"
                )


def _lower(logical, setup, mapping, diagnostics):
    intervals, pulses, reservations = [], [], []
    for idx, event in enumerate(logical):
        event.logical_ids = (f"e{idx}",)
        if event.kind not in {"play", "delay", "reserve", "trigger"}:
            continue
        if event.kind == "reserve" and event.signal not in mapping:
            known = {"kernel_submit"} | set(setup.devices)
            known |= {d.bus for d in setup.devices.values()} | {
                d.update_group for d in setup.devices.values()
            }
            if event.signal not in known:
                raise CompileError(f"Unknown reserved resource {event.signal}")
            resources = (event.signal,)
            intervals.append((event.start_mu, event.end_mu, resources, event.source))
            reservations.append((event.start_mu, event.end_mu, resources, event.source))
            continue
        try:
            bindings = mapping.bindings(event.signal)
        except (ValueError, TypeError) as exc:
            raise CompileError(str(exc)) from exc
        devices = []
        for binding in bindings:
            if binding.device not in setup.devices:
                raise CompileError(f"Unknown mapped device {binding.device}")
            device = setup.devices[binding.device]
            devices.append(device.name)
        intervals.append((event.start_mu, event.end_mu, tuple(devices), event.source))
        event.resources = tuple(devices)
        if event.kind == "reserve":
            reservations.append(
                (event.start_mu, event.end_mu, tuple(devices), event.source)
            )
        if event.kind in {"delay", "reserve"} or event.duration_mu == 0:
            continue
        for binding in bindings:
            device = setup.devices[binding.device]
            cal = setup.calibration.get(device.name, DeviceCalibration())
            latency = device.latency_mu + cal.latency_mu
            start = event.start_mu - latency
            if start % device.grid_mu or event.duration_mu % device.grid_mu:
                raise CompileError(
                    f"Pulse on {device.name} violates its "
                    f"{device.grid_mu} mu device grid"
                )
            if event.duration_mu < device.min_duration_mu:
                raise CompileError(f"Pulse shorter than minimum on {device.name}")
            params = dict(event.params)
            if device.kind == "dds":
                if event.kind == "trigger":
                    raise CompileError("Protocol trigger steps require TTL resources")
                params["frequency"] = (
                    params["frequency"] * binding.frequency_scale
                    + binding.frequency_offset
                    + cal.frequency_offset
                )
                params["phase"] = (
                    params["phase"] * binding.phase_scale
                    + binding.phase_offset
                    + cal.phase_offset
                ) % (2 * math.pi)
                params["amplitude"] *= binding.amplitude_scale * cal.amplitude_scale
                if (
                    not device.min_frequency
                    <= params["frequency"]
                    <= device.max_frequency
                    or params["frequency"] >= device.sysclk_hz / 2
                ):
                    raise CompileError(f"Frequency outside limits on {device.name}")
                if not 0 <= params["amplitude"] <= 1:
                    raise CompileError(
                        f"Calibrated amplitude outside 0..1 on {device.name}"
                    )
            elif event.kind == "play":
                if (
                    params["amplitude"] not in (0, 1)
                    or params["frequency"] != 0
                    or params["phase"] != 0
                ):
                    raise CompileError(
                        "TTL Square only supports amplitude 0/1, no frequency or phase"
                    )
                if params["amplitude"] == 0:
                    continue
            if device.kind == "ttl":
                params.update(frequency=0, phase=0, amplitude=1)
            pulses.append(
                ScheduledEvent(
                    device.kind,
                    device.name,
                    start,
                    event.duration_mu,
                    params,
                    event.source,
                    (device.name,),
                    event.logical_ids,
                )
            )
    _check_intervals(intervals)
    _check_intervals([(p.start_mu, p.end_mu, p.resources, p.source) for p in pulses])
    # Adjacent compatible output intervals form one physical pulse,
    # preserving logical identities.
    merged = []
    for name in sorted({p.signal for p in pulses}):
        seq = sorted((p for p in pulses if p.signal == name), key=lambda p: p.start_mu)
        for pulse in seq:
            last = merged[-1] if merged and merged[-1].signal == name else None

            def signature(p):
                return tuple(
                    p.params.get(k) for k in ("frequency", "phase", "amplitude")
                )

            if (
                last
                and last.end_mu == pulse.start_mu
                and signature(last) == signature(pulse)
            ):
                last.duration_mu += pulse.duration_mu
                last.logical_ids += pulse.logical_ids
            else:
                merged.append(copy.deepcopy(pulse))
    edges = {t for p in merged for t in (p.start_mu, p.end_mu)}
    configs, physical, previous = [], [], {}
    for pulse in sorted(merged, key=lambda p: (p.start_mu, p.signal)):
        device = setup.devices[pulse.signal]
        signature = tuple(
            pulse.params.get(k) for k in ("frequency", "phase", "amplitude")
        )
        old = previous.get(device.name)
        if device.kind == "dds" and (old is None or old[0] != signature):
            earliest = old[1] if old else None
            finish = pulse.start_mu
            # Last-fit configuration avoids active outputs in its shared update group,
            # global submission jobs and edges inside a blocking driver call.
            while True:
                start = (
                    (finish - device.config_duration_mu) // device.config_grid_mu
                ) * device.config_grid_mu
                finish = start + device.config_duration_mu
                if earliest is not None and start < earliest:
                    raise CompileError(
                        f"No legal DDS preparation window on {device.name}; "
                        "strict adjacency/short delay cannot accommodate "
                        "parameter update"
                    )
                blockers = [
                    (c.start_mu, c.end_mu)
                    for c in configs
                    if _overlap((start, finish), (c.start_mu, c.end_mu))
                ]
                group = device.update_group
                for other in merged:
                    other_dev = setup.devices[other.signal]
                    shared = other.signal == device.name or (
                        group and other_dev.update_group == group
                    )
                    if shared and _overlap(
                        (start, finish), (other.start_mu, other.end_mu)
                    ):
                        blockers.append((other.start_mu, other.end_mu))
                resources = {
                    device.name,
                    device.bus,
                    device.update_group,
                    "kernel_submit",
                } - {None}
                for rs, re, rr, _ in reservations:
                    if resources.intersection(rr) and _overlap(
                        (start, finish), (rs, re)
                    ):
                        blockers.append((rs, re))
                interior_edges = [t for t in edges if start < t < finish]
                if not blockers and not interior_edges:
                    break
                finish = min([b[0] for b in blockers] + interior_edges)
            params = {k: pulse.params[k] for k in ("frequency", "phase", "amplitude")}
            params["ftw"] = round(params["frequency"] / device.sysclk_hz * 2**32)
            params["pow"] = round(params["phase"] / (2 * math.pi) * 2**16) % 2**16
            params["asf"] = round(params["amplitude"] * 0x3FFF)
            params["actual_frequency"] = params["ftw"] * device.sysclk_hz / 2**32
            params["actual_phase"] = params["pow"] * (2 * math.pi) / 2**16
            params["actual_amplitude"] = params["asf"] / 0x3FFF
            params["ref_time_mu"] = 0
            configs.append(
                ScheduledEvent(
                    "dds_config",
                    device.name,
                    start,
                    device.config_duration_mu,
                    params,
                    pulse.source,
                    tuple(sorted(resources)),
                    pulse.logical_ids,
                )
            )
        previous[device.name] = (signature, pulse.end_mu)
        for suffix, when in (("on", pulse.start_mu), ("off", pulse.end_mu)):
            physical.append(
                ScheduledEvent(
                    f"{device.kind}_{suffix}",
                    device.name,
                    when,
                    0,
                    {},
                    pulse.source,
                    (device.name,),
                    pulse.logical_ids,
                )
            )
    physical.extend(configs)
    # Exact alignment of t0 and the pre-roll is part of the export contract.
    grids = [
        g
        for d in setup.devices.values()
        for g in (d.grid_mu, d.config_grid_mu if d.kind == "dds" else 1)
    ]
    grid = math.lcm(*grids) if grids else 1
    minimum = min((e.start_mu for e in physical), default=0)
    origin = ((max(0, -minimum) + grid - 1) // grid) * grid
    for event in physical:
        event.start_mu += origin
        if event.kind == "dds_config":
            event.params["ref_time_mu"] = origin
    # Edges at configuration start must be submitted first; edge at finish follows it.
    physical.sort(key=lambda e: (e.start_mu, e.kind == "dds_config", e.signal, e.kind))
    if configs:
        diagnostics.append(
            {
                "kind": "driver_contract",
                "status": "assumed_not_measured",
                "message": (
                    "DDS configuration durations and output model "
                    "require real target validation"
                ),
            }
        )
    return physical, origin


def compile_experiment(
    experiment,
    setup,
    signal_map,
    *,
    backend="artiq",
    allow_quantization=False,
    max_points=10000,
    max_events_per_point=100000,
    calibration=None,
):
    if backend != "artiq":
        raise NotImplementedError("Only the ARTIQ9 reference backend is available")
    if (
        len(experiment.root.children) != 1
        or experiment.root.children[0].kind != "average"
    ):
        raise CompileError("Experiment requires exactly one top-level average block")
    if len(experiment._stack) != 1:
        raise CompileError("Cannot compile an experiment while a builder block is open")
    setup = copy.deepcopy(setup)
    if calibration is not None:
        setup.calibration = copy.deepcopy(dict(calibration))
        setup.__post_init__()
    mapping = SignalMap(copy.deepcopy(dict(signal_map)))
    root = copy.deepcopy(experiment.root)
    avg = root.children[0]
    body, parameters = avg.children, []
    while len(body) == 1 and body[0].kind == "sweep":
        parameters.append(body[0].params["parameter"])
        body = body[0].children
    if any(n.kind in {"average", "sweep"} for n in _walk(body)):
        raise CompileError(
            "Sweeps must form one outer nested chain, before the shot body"
        )
    if len({p.name for p in parameters}) != len(parameters):
        raise CompileError("Sweep axis names must be unique")
    count = math.prod(len(p.values) for p in parameters)
    if count > max_points:
        raise CompileError("Sweep exceeds max_points")
    axes = [
        {
            "name": p.name,
            "values": [float(v) for v in p.values],
            "unit": p.unit,
            "values_unit": {
                "time": "s",
                "frequency": "Hz",
                "phase": "rad",
                "dimensionless": "dimensionless",
            }[p.dimension],
        }
        for p in parameters
    ]
    points, diagnostics = [], []
    acquisition_signature = None
    indices = product(*(range(len(p.values)) for p in parameters))
    for index, axis_indices in enumerate(indices):
        coords = {p.name: p.values[i] for p, i in zip(parameters, axis_indices)}
        scheduler = _Scheduler(
            experiment,
            setup,
            coords,
            allow_quantization,
            diagnostics,
            max_events_per_point,
            mapping,
        )
        logical, duration = scheduler.serial(
            body, {uid: 0 for uid in experiment.qubits}
        )
        acquisitions = [
            (e.params["handle"], e.params["qubit"])
            for e in logical
            if e.kind == "measure"
        ]
        if len(acquisitions) != len(set(acquisitions)):
            raise CompileError(
                "Each handle/qubit may be measured once per shot; use distinct handles"
            )
        signature = tuple(sorted(acquisitions))
        if acquisition_signature is not None and signature != acquisition_signature:
            raise CompileError(
                "All scan points must share the same acquisition handles/qubits"
            )
        acquisition_signature = signature
        physical, origin = _lower(logical, setup, mapping, diagnostics)
        points.append(
            CompiledPoint(
                index,
                {k: float(v) for k, v in coords.items()},
                axis_indices,
                sorted(logical, key=lambda e: e.start_mu),
                physical,
                duration,
                origin,
            )
        )
    ir = json_value(root)
    snapshots = {k: q.snapshot() for k, q in experiment.qubits.items()}
    metadata = copy.deepcopy(experiment.metadata)
    identity = {
        "ir": ir,
        "qubits": {k: q.to_dict() for k, q in snapshots.items()},
        "setup": setup.to_dict(),
        "mapping": mapping.to_dict(),
        "metadata": json_value(metadata),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    provenance = {
        "compiler_version": "0.1.0",
        "target": "ARTIQ 9 / AD9910 / TTL",
        "experiment_ir_hash": hashlib.sha256(
            json.dumps(ir, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
        "content_hash": digest,
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
        "time_reference": (
            "logical signal reference; physical device output model; "
            "not atom-field calibration"
        ),
        "qubits": {k: q.to_dict() for k, q in snapshots.items()},
        "host_period": "unconstrained",
        "phase_policy": "shot_reference_tracking",
    }
    return CompiledExperiment(
        experiment.name,
        points,
        avg.params["shots"],
        avg.params["mode"],
        axes,
        snapshots,
        setup,
        mapping,
        metadata,
        provenance,
        diagnostics,
        ir,
    )
