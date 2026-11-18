"""Per-compilation planned positions, never mutations of user qubits."""

from __future__ import annotations

import copy
import math
from itertools import combinations

from ..config.models import Position
from ..config.transport import SiteRef, _interpolate, normalize_transport
from ..ir import CompileError, ScheduledEvent


def _distance(a, b):
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


def _layout(positions):
    return {uid: p.to_dict() for uid, p in sorted(positions.items())}


class SpatialState:
    """One scheduler's state; shared objects and unscheduled trees stay untouched."""

    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.config = normalize_transport(getattr(scheduler.setup, "_transport", None))
        if self.config is None:
            raise CompileError("move/zone coverage requires LabConfig.transport")
        cfg = self.config
        self.zones = {zone.uid: zone for zone in cfg.zones}
        self.positions = {
            uid: q.position
            for uid, q in scheduler.exp.qubits.items()
            if q.position is not None
        }
        missing = set(cfg.bindings) - self.positions.keys()
        if missing:
            raise CompileError(
                f"Transport needs registered positions for {sorted(missing)}"
            )
        for uid in cfg.bindings:
            if self.positions[uid].coordinate_system != cfg.coordinate_system:
                raise CompileError(f"Transport coordinate system mismatch for {uid}")
        groups = [
            (name, group)
            for name, group in scheduler.setup.spectrum.groups.items()
            if cfg.card in group.cards
        ]
        if len(groups) != 1:
            raise CompileError(
                "Transport card must belong to exactly one Spectrum group"
            )
        self.group, group = groups[0]
        self.card = group.cards[cfg.card]
        if getattr(self.card, "mode", "awg") != "dds":
            raise CompileError("AOD transport requires a native Spectrum DDS card")
        self.resources = tuple(
            name for name, output in group.outputs.items() if output.card == cfg.card
        )
        channels = {
            output.channel
            for output in group.outputs.values()
            if output.card == cfg.card
        }
        if not {cfg.x_channel, cfg.y_channel} <= channels:
            raise CompileError("Both crossed AOD channels need configured outputs")
        for values in cfg.bindings.values():
            for axis in ("x", "y"):
                channel, core = getattr(cfg, f"{axis}_channel"), values[f"{axis}_core"]
                if core not in self.card.dds_channel_cores[channel]:
                    raise CompileError(
                        f"DDS core {core} is not routed to channel {channel}"
                    )
        coverage = getattr(scheduler.setup, "zone_coverage", {})
        unknown = {z for zones in coverage.values() for z in zones} - self.zones.keys()
        if unknown:
            raise CompileError(f"Unknown optical coverage zones: {sorted(unknown)}")
        self.initial = dict(self.positions)
        self.tones = self._tones(self.positions)
        self.initial_tones = copy.deepcopy(self.tones)
        inverse_scales = [
            max(abs((b[0] - a[0]) / (b[1] - a[1])) for a, b in zip(table, table[1:]))
            for table in (cfg.x_calibration, cfg.y_calibration)
        ]
        self.frequency_tolerance_hz = min(
            cfg.frequency_tolerance_hz,
            cfg.position_tolerance / math.hypot(*inverse_scales),
        )
        self.moved = False
        self._check_paths(self.positions)

    def _tones(self, positions):
        cfg, tones, coordinates = self.config, {}, {}
        for uid, binding in cfg.bindings.items():
            for axis in ("x", "y"):
                core = binding[f"{axis}_core"]
                coordinate = getattr(positions[uid], axis)
                if (
                    core in coordinates
                    and abs(coordinate - coordinates[core]) > cfg.position_tolerance
                ):
                    raise CompileError(
                        f"Atoms sharing DDS core {core} need one {axis} trajectory"
                    )
                coordinates[core] = coordinate
                table = getattr(cfg, f"{axis}_calibration")
                frequency = _interpolate(table, coordinate, 1)
                amplitude = cfg.initial_amplitudes[core] * _interpolate(
                    table, coordinate, 2
                )
                if not 0 < amplitude <= 1:
                    raise CompileError("AOD amplitude compensation exceeds (0, 1]")
                step = 1_250_000_000 / 2**32
                realized = round(frequency / step) * step
                if abs(realized - frequency) > cfg.frequency_tolerance_hz:
                    raise CompileError(
                        "DDS endpoint frequency exceeds transport tolerance"
                    )
                inverse = sorted((row[1], row[0]) for row in table)
                # Quantization can place an endpoint just beyond the lookup domain.
                nearest = min(max(realized, inverse[0][0]), inverse[-1][0])
                inverse_position = _interpolate(inverse, nearest, 1)
                segment = inverse[:2] if realized < inverse[0][0] else inverse[-2:]
                if nearest != realized:
                    inverse_position += (realized - nearest) * (
                        (segment[1][1] - segment[0][1])
                        / (segment[1][0] - segment[0][0])
                    )
                if abs(inverse_position - coordinate) > cfg.position_tolerance:
                    raise CompileError(
                        "DDS endpoint quantization exceeds position tolerance"
                    )
                tones[core] = {
                    "core": core,
                    "channel": getattr(cfg, f"{axis}_channel"),
                    "frequency_hz": frequency,
                    "amplitude": amplitude,
                }
        return [tones[core] for core in sorted(tones)]

    def _check_paths(self, final):
        cfg = self.config
        for axis in ("x", "y"):
            before, after = {}, {}
            for uid, binding in cfg.bindings.items():
                core = binding[f"{axis}_core"]
                before[core] = getattr(self.positions[uid], axis)
                after[core] = getattr(final[uid], axis)
            for a, b in combinations(before, 2):
                start, end = before[a] - before[b], after[a] - after[b]
                if (
                    abs(start) <= cfg.position_tolerance
                    or abs(end) <= cfg.position_tolerance
                    or start * end < 0
                ):
                    raise CompileError(
                        f"Distinct {axis} DDS tones cannot coincide or cross"
                    )
        self._check_separation(self.positions, final)

    def _check_separation(self, initial, final):
        cfg = self.config
        relevant = {
            uid: p
            for uid, p in initial.items()
            if p.coordinate_system == cfg.coordinate_system
        }
        for a, b in combinations(relevant, 2):
            start = [
                getattr(initial[a], axis) - getattr(initial[b], axis)
                for axis in ("x", "y", "z")
            ]
            end = [
                getattr(final[a], axis) - getattr(final[b], axis)
                for axis in ("x", "y", "z")
            ]
            delta = [y - x for x, y in zip(start, end)]
            norm = sum(v * v for v in delta)
            progress = (
                min(1, max(0, -sum(x * y for x, y in zip(start, delta)) / norm))
                if norm
                else 0
            )
            separation = math.sqrt(
                sum((x + progress * y) ** 2 for x, y in zip(start, delta))
            )
            if separation < cfg.min_separation + 2 * cfg.position_tolerance:
                raise CompileError(
                    f"Transport paths violate minimum separation: {a}, {b}"
                )

    def _check_frequency_paths(self, knots):
        """Check the actual piecewise-linear frequency path through each lookup.

        Split wherever an inverse calibration changes slope. Atom trajectories
        are then linear on each interval, allowing an exact closest approach.
        The clearance includes the declared endpoint/frequency error margin.
        """
        cfg = self.config
        for first, last in zip(knots, knots[1:]):
            before = {tone["core"]: tone["frequency_hz"] for tone in first["tones"]}
            after = {tone["core"]: tone["frequency_hz"] for tone in last["tones"]}
            boundaries = {0.0, 1.0}
            for binding in cfg.bindings.values():
                for axis in ("x", "y"):
                    core = binding[f"{axis}_core"]
                    difference = after[core] - before[core]
                    if not difference:
                        continue
                    for _, frequency, _ in getattr(cfg, f"{axis}_calibration"):
                        fraction = (frequency - before[core]) / difference
                        if 0 < fraction < 1:
                            boundaries.add(fraction)
            previous = None
            for fraction in sorted(boundaries):
                positions = dict(self.positions)
                for uid, binding in cfg.bindings.items():
                    xy = []
                    for axis in ("x", "y"):
                        core = binding[f"{axis}_core"]
                        frequency = before[core] + fraction * (
                            after[core] - before[core]
                        )
                        inverse = sorted(
                            (row[1], row[0])
                            for row in getattr(cfg, f"{axis}_calibration")
                        )
                        frequency = min(max(frequency, inverse[0][0]), inverse[-1][0])
                        xy.append(_interpolate(inverse, frequency, 1))
                    positions[uid] = Position(
                        *xy, self.positions[uid].z, cfg.coordinate_system
                    )
                if previous is not None:
                    self._check_separation(previous, positions)
                previous = positions

    def move(self, node):
        from ..dsl.values import resolve

        cfg, scheduler = self.config, self.scheduler
        p = node.params
        unknown = set(p["qubits"]) - cfg.bindings.keys()
        if unknown:
            raise CompileError(f"Unbound transport qubits: {sorted(unknown)}")
        final = dict(self.positions)
        for uid, target in zip(p["qubits"], p["destinations"]):
            if isinstance(target, SiteRef):
                if self.zones.get(target.zone.uid) != target.zone:
                    raise CompileError(
                        "Move destination IZ differs from transport snapshot"
                    )
                target = target.position
            origin = self.positions[uid]
            if target.coordinate_system != cfg.coordinate_system:
                raise CompileError(
                    "Move destination uses a different coordinate system"
                )
            if abs(target.z - origin.z) > cfg.position_tolerance:
                raise CompileError("Crossed X/Y AOD transport cannot change z")
            final[uid] = target
        # Unlisted atoms remain in final. _tones then detects any attempted
        # unilateral change of a shared axis; no spectator moves implicitly.
        final_tones = self._tones(final)
        self._check_paths(final)
        duration = scheduler.ticks(
            resolve(p["duration"], scheduler.coordinates, "time"),
            node.source,
            grid_mu=32,
        )
        settle = scheduler.ticks(
            resolve(p["settle"], scheduler.coordinates, "time"), node.source, grid_mu=32
        )
        if duration <= 0:
            raise CompileError("Move duration must be positive")
        profile = p["profile"]
        times = {u for u, _ in profile.samples}
        for uid in p["qubits"]:
            for axis in ("x", "y"):
                start, end = (
                    getattr(self.positions[uid], axis),
                    getattr(final[uid], axis),
                )
                if start == end:
                    continue
                for coordinate, _, _ in getattr(cfg, f"{axis}_calibration"):
                    progress = (coordinate - start) / (end - start)
                    if 0 < progress < 1:
                        times.add(profile.time_at(progress))
        requested_offsets = [u * duration for u in sorted(times)]
        offsets = [round(requested / 32) * 32 for requested in requested_offsets]
        if len(set(offsets)) != len(offsets):
            raise CompileError("Move profile knots collapse on the DDS timer grid")
        timing_quantization = [
            {
                "requested_offset_mu": requested,
                "offset_mu": actual,
                "error_mu": actual - requested,
                "error_s": float((actual - requested) * scheduler.tick),
            }
            for requested, actual in zip(requested_offsets, offsets)
        ]
        knots = []
        for offset in offsets:
            progress = profile.progress(offset / duration)
            positions = dict(self.positions)
            for uid in p["qubits"]:
                start, end = self.positions[uid], final[uid]
                positions[uid] = Position(
                    *(
                        getattr(start, a)
                        + progress * (getattr(end, a) - getattr(start, a))
                        for a in ("x", "y", "z")
                    ),
                    cfg.coordinate_system,
                )
            knots.append({"offset_mu": offset, "tones": self._tones(positions)})
        knots[0]["tones"], knots[-1]["tones"] = copy.deepcopy(self.tones), final_tones
        self._check_frequency_paths(knots)
        event = ScheduledEvent(
            "move",
            cfg.card,
            0,
            duration + settle,
            {
                "card": cfg.card,
                "qubits": list(p["qubits"]),
                "operation_qubits": list(p["qubits"]),
                "initial_tones": copy.deepcopy(self.tones),
                "knots": knots,
                "timing_quantization": timing_quantization,
                "max_timing_quantization_error_mu": max(
                    abs(item["error_mu"]) for item in timing_quantization
                ),
                "max_timing_quantization_error_s": max(
                    abs(item["error_s"]) for item in timing_quantization
                ),
                "trajectory_model": "piecewise_linear_frequency",
                "motion_duration_mu": duration,
                "settle_mu": settle,
                "initial_layout": _layout(self.positions),
                "final_layout": _layout(final),
                "profile": profile.to_dict(),
                "frequency_tolerance_hz": self.frequency_tolerance_hz,
                "position_tolerance_m": cfg.position_tolerance,
            },
            node.source,
            self.resources,
        )
        self.positions, self.tones, self.moved = final, final_tones, True
        return event

    def apply_coverage(self, node, events):
        signal = node.params["signal"]
        outputs = {b.device for b in self.scheduler.mapping.bindings(signal)}
        coverage = getattr(self.scheduler.setup, "zone_coverage", {})
        covered = outputs & coverage.keys()
        if not covered:
            return
        if len(outputs) != 1:
            raise CompileError(
                "Zone-wide pulse must select exactly one physical output"
            )
        source, role = self.scheduler.signal_qubit(signal)
        if source is None:
            raise CompileError("Zone-wide pulse requires a Qubit calibration source")
        sites = [
            site for z in coverage[next(iter(covered))] for site in self.zones[z].sites
        ]
        missing = set(self.scheduler.exp.qubits) - self.positions.keys()
        if missing:
            raise CompileError(
                f"Zone inference needs registered positions for {sorted(missing)}"
            )
        targets = sorted(
            uid
            for uid, pos in self.positions.items()
            if any(
                pos.coordinate_system == site.coordinate_system
                and _distance(pos, site) <= self.config.position_tolerance
                for site in sites
            )
        )
        if source.uid not in targets:
            raise CompileError(
                "Zone pulse calibration source is outside illuminated sites"
            )
        signals = []
        for uid in targets:
            q = self.scheduler.exp.qubits[uid]
            if role not in q.drive_names:
                raise CompileError(f"Illuminated atom {uid} lacks the {role} drive")
            member = q.signal(role)
            if {b.device for b in self.scheduler.mapping.bindings(member)} != outputs:
                raise CompileError(
                    "Zone occupants must share the declared physical output"
                )
            signals.append(member)
        declaration = node.params.get("global_operation")
        if declaration is not None and set(declaration["targets"]) != set(targets):
            raise CompileError("Explicit targets differ from current zone occupancy")
        for event in events:
            event.params.update(
                operation_qubits=targets,
                global_operation={
                    "targets": targets,
                    "signals": sorted(signals),
                    "calibration": source.uid,
                    "drive": role,
                },
                zone_coverage=list(coverage[next(iter(covered))]),
                spatial_layout=_layout(self.positions),
            )

    def finish(self):
        if self.moved and any(
            _distance(self.initial[q], self.positions[q])
            > self.config.position_tolerance
            for q in self.config.bindings
        ):
            raise CompileError(
                "Transport shot must return every bound atom to its initial position"
            )
