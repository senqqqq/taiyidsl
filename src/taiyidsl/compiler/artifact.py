"""Compiled experiment snapshots and their public export/report facade."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..backends import Backend, get_backend
from ..config.hardware import DeviceSetup, SignalMap
from ..dsl._serialization import json_value
from ..host_plan import execution_order
from ..ir import CompiledPoint, CompileError, acquisition_targets
from ..sample_assets import pack_program_assets, point_programs


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
    parameters: dict = field(default_factory=dict, kw_only=True)
    host_execution_plan: dict = field(default_factory=dict, kw_only=True)
    _backend: Backend | None = field(default=None, repr=False, compare=False)
    _integrity_hash: str = field(init=False, repr=False)

    def __post_init__(self):
        if not self.host_execution_plan:
            self.host_execution_plan = {
                "schema_version": 1,
                "shots": self.shots,
                "averaging_mode": self.averaging_mode,
                "averaging_scope": "global",
                "chunk_axis": None,
                "point_count": len(self.points),
                "chunks": [
                    {"index": p.index, "points": [p.index]} for p in self.points
                ],
            }
        if self._backend is None:
            name = self.provenance.get("backend", {}).get("name", "artiq")
            self._backend = copy.deepcopy(get_backend(name))
        self._integrity_hash = self._checksum()

    def _checksum(self):
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    def assert_integrity(self):
        descriptor = self.provenance.get("backend")
        if descriptor is not None and self._backend.describe() != descriptor:
            raise CompileError(
                "Backend changed after compilation; rebuild the experiment"
            )
        if self._checksum() != self._integrity_hash:
            raise CompileError(
                "Compiled artifact was modified after compilation; "
                "rebuild it from the experiment"
            )

    @property
    def sha256(self):
        """Verified hash of this complete compiled artifact, for external records."""
        self.assert_integrity()
        return self._integrity_hash

    def execution_order(self):
        self.assert_integrity()
        return execution_order(self.host_execution_plan)

    def acquisition_plan(self, point=0):
        """Describe expected detector observations, without accessing a detector.

        Durations describe scheduled trigger/protocol windows, not camera exposure
        or transfer completion. Explicit camera settings belong in metadata.
        """
        self.assert_integrity()
        if type(point) is not int or not 0 <= point < len(self.points):
            raise IndexError("Acquisition point is outside the compiled scan")
        return [
            {
                "handle": event.params["handle"],
                "qubits": list(acquisition_targets(event)),
                "signal": event.signal,
                "start_s": event.start_mu * self.setup.tick_seconds,
                "duration_s": event.duration_mu * self.setup.tick_seconds,
                "metadata": copy.deepcopy(event.params.get("metadata", {})),
                "protocol": event.params.get("protocol"),
            }
            for event in self.points[point].logical_events
            if acquisition_targets(event)
        ]

    def virtual_z_frames(self, point=0):
        """Return Rz contributions since the last frame/phase reset, in radians.

        This is provenance, not a second drive frame. Use frame_phases() for the
        effective persistent offset, which also includes shift_phase updates.
        """
        self.assert_integrity()
        if type(point) is not int or not 0 <= point < len(self.points):
            raise IndexError("Virtual-Z point is outside the compiled scan")
        phases = dict.fromkeys(self.qubits, 0.0)
        for event in sorted(
            self.points[point].logical_events, key=lambda event: event.start_mu
        ):
            if event.kind == "rz":
                phases[event.signal] = event.params["accumulated_virtual_z"]
            elif event.kind in {"reset_frame", "reset_phase"}:
                phases[event.signal] = 0.0
        return phases

    def frame_phases(self, point=0):
        """Return the final unified drive-frame offsets F, in radians."""
        self.assert_integrity()
        if type(point) is not int or not 0 <= point < len(self.points):
            raise IndexError("Frame point is outside the compiled scan")
        phases = dict.fromkeys(self.qubits, 0.0)
        for event in sorted(
            self.points[point].logical_events, key=lambda event: event.start_mu
        ):
            if event.kind in {"rz", "shift_phase", "reset_frame", "reset_phase"}:
                phases[event.signal] = event.params["phase"]
        return phases

    def to_dict(self):
        points = [asdict(p) for p in self.points]
        assets = getattr(self.setup, "_sample_assets", {})
        result = {
            "schema_version": 2,
            "name": self.name,
            "shots": self.shots,
            "averaging_mode": self.averaging_mode,
            "host_execution_plan": copy.deepcopy(self.host_execution_plan),
            "axes": self.axes,
            "points": points,
            "qubits": {k: q.to_dict() for k, q in self.qubits.items()},
            "setup": self.setup.to_dict(),
            "signal_map": self.signal_map.to_dict(),
            "metadata": json_value(self.metadata),
            "parameters": copy.deepcopy(self.parameters),
            "provenance": self.provenance,
            "diagnostics": self.diagnostics,
            "ir": self.ir,
        }
        if assets:
            table = {key: dict(record) for key, record in assets.items()}
            for point in points:
                pack_program_assets(point_programs(point), table)
            result["sample_assets"] = table
        return result

    def save(self, path):
        self.assert_integrity()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return path

    def visualize(self, path=None, *, waveform_path=None):
        self.assert_integrity()
        from ..reporting.visualization import visualize

        return visualize(self, path, waveform_path=waveform_path)

    def simulate_output(
        self, point=0, start_s=0.0, duration_s=1e-6, *, max_samples=100000
    ):
        """Decode a short window of final hybrid DAC output, relative to shot T0."""
        from ..reporting.output_simulator import simulate_output

        return simulate_output(
            self, point, start_s, duration_s, max_samples=max_samples
        )

    def waveform_simulator(self, path=None, *, max_embedded_bytes=64 * 1024**2):
        """Create a standalone, offline viewer for final hybrid DAC samples."""
        from ..reporting.output_simulator import write_output_simulator

        return write_output_simulator(self, path, max_embedded_bytes=max_embedded_bytes)

    def export_report(self, output_dir, *, source_path=None):
        """Export the hybrid DSL, pulse sheet, DAC viewer and hardware tables."""
        from ..reporting.experiment_report import export_experiment_report

        return export_experiment_report(self, output_dir, source_path=source_path)

    def export(self, output_dir, **options):
        """Export with the backend that compiled this plan, without executing it."""
        self.assert_integrity()
        return self._backend.export(self, output_dir, **options)

    def export_artiq(self, output_dir, *, mode="auto"):
        self.assert_integrity()
        from taiyidsl.backends.artiq.export import export_artiq

        return export_artiq(self, output_dir, mode=mode)


def assemble_artifact(
    experiment, inputs, points, native_points, axes, diagnostics, host_execution_plan
):
    """Snapshot metadata after all points pass, preserving the artifact identity."""
    root, setup, mapping = inputs.root, inputs.setup, inputs.mapping
    target, avg, native_contract = inputs.target, inputs.average, inputs.native_contract
    ir = json_value(root)
    snapshots = {k: q.snapshot() for k, q in experiment.qubits.items()}
    metadata = copy.deepcopy(experiment.metadata)
    if getattr(setup, "_transport", None) is not None:
        metadata["transport"] = setup._transport.to_dict()
    if native_contract is not None:
        metadata["native_artiq"] = {
            key: value
            for key, value in native_contract.items()
            if key not in {"prefix", "timed", "suffix"}
        }
        metadata["native_artiq"]["points"] = native_points
        metadata["native_artiq"]["time_reference"] = "control_epoch_after_native_prefix"
        metadata["native_artiq"]["shot_duration"] = (
            "runtime_prefix_plus_control_and_suffix"
        )
    parameters = json_value(experiment.parameters.to_dict())
    identity = {
        "ir": ir,
        "qubits": {k: q.to_dict() for k, q in snapshots.items()},
        "setup": setup.to_dict(),
        "mapping": mapping.to_dict(),
        "metadata": json_value(metadata),
        "parameters": parameters,
        "backend": target.describe(),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    provenance = {
        "compiler_version": "0.1.0",
        **target.provenance(),
        "experiment_ir_hash": hashlib.sha256(
            json.dumps(ir, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
        "content_hash": digest,
        "time_reference": (
            "logical signal reference; physical device output model; "
            "not atom-field calibration"
        ),
        "qubits": {k: q.to_dict() for k, q in snapshots.items()},
        "host_period": "unconstrained",
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
        parameters=parameters,
        host_execution_plan=host_execution_plan,
        _backend=target,
    )
