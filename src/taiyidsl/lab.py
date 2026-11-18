"""Central laboratory profiles: target, wiring, protocols, and calibration.

The built-in profiles are offline examples, not measured laboratory setups or
ARTIQ device databases. Supply a lab-specific profile before using instruments.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from types import MappingProxyType

from .backends import Backend, get_backend
from .config.hardware import (
    ControlBinding,
    ControlRecipe,
    Device,
    DeviceCalibration,
    DeviceSetup,
    Protocol,
    ProtocolStep,
    SignalMap,
    TriggerSequence,
    TriggerStep,
)
from .units import as_fraction, us


def _spectrum_config(value):
    if value is None:
        return None
    from .config.spectrum import SpectrumConfig

    return SpectrumConfig.from_dict(value)


def _keys(data, *, allowed, required=(), name):
    if not isinstance(data, Mapping):
        raise TypeError(f"{name} must be an object")
    unknown, missing = set(data) - set(allowed), set(required) - set(data)
    if unknown or missing:
        raise ValueError(
            f"Invalid {name}: unknown={sorted(unknown)}, missing={sorted(missing)}"
        )


def _seconds(value, name, *, positive=False, optional=False):
    if value is None and optional:
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be an SI numeric duration")
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"Invalid {name}")
    return value


def _logical_name(value, *, qubit_id=None, deferred=False):
    if not isinstance(value, str) or not value:
        raise ValueError("Logical signal names must be nonempty strings")
    remainder = value.replace("{qubit}", "")
    if "{" in remainder or "}" in remainder:
        raise ValueError("Only the {qubit} signal placeholder is supported")
    if "{qubit}" in value and not deferred:
        if qubit_id is None:
            raise ValueError("This profile requires an explicit qubit_id")
        return value.replace("{qubit}", qubit_id)
    return value


def _typed_fields(data, *, text=(), nullable=(), optional_numeric=()):
    """Check JSON field types before passing values to model constructors."""
    for name, value in data.items():
        if name in optional_numeric and value is None:
            continue
        if name in text or name in nullable:
            if value is None and name in nullable:
                continue
            if not isinstance(value, str) or not value:
                raise TypeError(f"{name} must be nonempty text")
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be numeric")
        elif not math.isfinite(value):
            raise ValueError(f"{name} must be finite")


@dataclass
class LabConfig:
    """Keep target configuration separate from reusable experiment builders.

    Profiles can be edited between compilations. Each compiled experiment keeps
    its own setup and mapping snapshots, so earlier plans remain unchanged.
    """

    setup: DeviceSetup
    signal_map: SignalMap | None = None
    backend: str | Backend | None = None
    qubits: tuple = field(default=(), kw_only=True)
    emulation: bool = field(default=True, kw_only=True)
    artiq_python: str | None = field(default=None, kw_only=True)
    transport: object | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if not isinstance(self.setup, DeviceSetup):
            raise TypeError("setup must be a DeviceSetup")
        if type(self.emulation) is not bool:
            raise TypeError("emulation must be bool")
        if self.signal_map is None:
            mapping = getattr(self.setup, "_signal_map", None)
            if mapping is None:
                raise ValueError("Supply signal_map or use DeviceSetup.from_descriptor")
            self.signal_map = copy.deepcopy(mapping)
        elif hasattr(self.setup, "_descriptor") and (
            self.signal_map.to_dict() != self.setup._signal_map.to_dict()
        ):
            raise ValueError(
                "Descriptor owns signal mapping; do not supply a second mapping"
            )
        self.backend = self.backend or getattr(self.setup, "_backend", "artiq")
        if self.backend == "artiq-spectrum":
            self.backend = "artiq_spectrum"
        if hasattr(self.setup, "_backend") and self.backend != self.setup._backend:
            raise ValueError("Backend differs from DeviceSetup descriptor")
        get_backend(self.backend)
        members = (
            self.qubits.values() if isinstance(self.qubits, Mapping) else self.qubits
        )
        self.qubits = tuple(q.snapshot() for q in members)
        if len({q.uid for q in self.qubits}) != len(self.qubits):
            raise ValueError("LabConfig qubit IDs must be unique")
        from .config.transport import normalize_transport

        self.transport = normalize_transport(self.transport)
        if self.transport is not None:
            self.setup = copy.deepcopy(self.setup)
            self.setup._transport = self.transport.snapshot()
        self.setup = self._setup_for_qubits(self.qubits)

    def _setup_for_qubits(self, qubits, *, require_connections=True):
        """Derive frame associations without editing the registered setup."""
        if not qubits or getattr(self.setup, "_descriptor", None) is None:
            return self.setup
        setup = copy.deepcopy(self.setup)
        frames = {}
        for qubit in qubits:
            for role in qubit.drive_names:
                signal = qubit.signal(role)
                if signal not in self.signal_map:
                    if require_connections:
                        raise ValueError(
                            f"Qubit signal has no setup connection: {signal}"
                        )
                    # An unregistered legacy Qubit can carry unused roles.
                    # Actual use of an unbound signal is checked by the compiler.
                    continue
                frames[signal] = qubit.drive_frame(role)
        if setup.spectrum is not None:
            setup.spectrum = replace(setup.spectrum, signal_frames=frames)
        return setup

    def connect(self, *, clock_session_ids=None):
        """Open an explicit hardware session; compilation always stays offline.

        The returned context owns a separate ARTIQ process. No worker or SDK is
        started in emulation mode. Detector data require a separate adapter;
        camera trigger kernels alone yield execution receipts, not measurements.
        """
        if self.emulation:
            raise RuntimeError("emulation=True permits offline compilation only")
        from .runtime.session import HardwareSession

        return HardwareSession(self, clock_session_ids=clock_session_ids).open()

    def describe(self):
        """Inspect the offline authoring configuration without opening hardware.

        Includes database-derived setup and Qubits as registered with this lab.
        A compiled experiment records its own calibration; these reference
        values do not override it. to_dict retains the legacy profile schema.
        """
        backend = self.backend if isinstance(self.backend, str) else self.backend.name
        return {
            "backend": backend,
            "emulation": self.emulation,
            "artiq_python": self.artiq_python,
            "setup": self.setup.describe(),
            "qubits": {qubit.uid: qubit.to_dict() for qubit in self.qubits},
            "signal_map": self.signal_map.to_dict(),
            **({"transport": self.transport.to_dict()} if self.transport else {}),
        }

    @classmethod
    def load(cls, path, *, qubit_id=None):
        """Load a schema-versioned JSON profile without accessing instruments.

        qubit_id substitutes {qubit} in mapping keys only. Trigger sequences
        retain their placeholder for expansion by prepare/measure on a target.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, qubit_id=qubit_id)

    @classmethod
    def from_dict(cls, data, *, qubit_id=None):
        """Construct the same profile from strict, JSON-shaped arguments."""
        _keys(
            data,
            allowed={"schema_version", "backend", "setup", "signals", "transport"},
            required={"schema_version", "backend", "setup", "signals"},
            name="lab profile",
        )
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported lab schema_version")
        if not isinstance(data["backend"], str) or not data["backend"]:
            raise ValueError("backend must be a registered backend name")
        get_backend(data["backend"])
        if qubit_id is not None:
            from .config.models import Qubit

            Qubit(qubit_id)  # Reuse identifier validation, not its calibration.
        setup = data["setup"]
        _keys(
            setup,
            allowed={
                "name",
                "tick_seconds",
                "devices",
                "calibration",
                "trigger_sequences",
                "timing_verified",
                "rtio_coarse_mu",
                "rtio_lane_count",
                "spectrum",
                "zone_coverage",
            },
            required={"name", "tick_seconds", "devices"},
            name="setup",
        )
        if not isinstance(setup["name"], str) or not setup["name"]:
            raise ValueError("setup.name must be nonempty text")
        _seconds(setup["tick_seconds"], "tick_seconds", positive=True)
        if not isinstance(setup.get("timing_verified", False), bool):
            raise TypeError("timing_verified must be a bool")
        devices = {}
        if not isinstance(setup["devices"], Mapping):
            raise TypeError("devices must be a name-to-device object")
        for name, item in setup["devices"].items():
            _keys(
                item,
                allowed={f.name for f in fields(Device)},
                required={"name", "kind"},
                name=f"device {name}",
            )
            _typed_fields(
                item,
                text={
                    "name",
                    "kind",
                    "nco_mode",
                    "ram_destination",
                    "ram_profile_control",
                },
                nullable={"bus", "update_group"},
                optional_numeric={
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
                    "urukul_proto_rev",
                },
            )
            devices[name] = Device(**item)
        calibration = {}
        if not isinstance(setup.get("calibration", {}), Mapping):
            raise TypeError("calibration must be a name-to-calibration object")
        for name, item in setup.get("calibration", {}).items():
            _keys(
                item,
                allowed={f.name for f in fields(DeviceCalibration)},
                name=f"calibration {name}",
            )
            _typed_fields(item, text={"revision"})
            calibration[name] = DeviceCalibration(**item)
        mapping = SignalMap()
        if not isinstance(data["signals"], Mapping):
            raise TypeError("signals must be a logical-name-to-bindings object")
        for signal, outputs in data["signals"].items():
            signal = _logical_name(signal, qubit_id=qubit_id)
            if signal in mapping:
                raise ValueError(f"Duplicate resolved signal: {signal}")
            if not isinstance(outputs, list) or not outputs:
                raise ValueError("Each signal needs a nonempty list of bindings")
            bindings = []
            for output in outputs:
                _keys(
                    output,
                    allowed={f.name for f in fields(ControlBinding)},
                    required={"device"},
                    name=f"binding for {signal}",
                )
                _typed_fields(output, text={"device"})
                binding = ControlBinding(**output)
                if binding.device not in devices:
                    raise ValueError(f"Unknown mapped device: {binding.device}")
                bindings.append(binding)
            mapping[signal] = ControlRecipe(tuple(bindings))
        sequences = {}
        if not isinstance(setup.get("trigger_sequences", {}), Mapping):
            raise TypeError("trigger_sequences must be a name-to-sequence object")
        for name, item in setup.get("trigger_sequences", {}).items():
            _keys(
                item,
                allowed={"description", "steps"},
                required={"steps"},
                name=f"trigger sequence {name}",
            )
            if not isinstance(name, str) or not name:
                raise ValueError("Trigger sequence names must be nonempty")
            if not isinstance(item.get("description", ""), str):
                raise TypeError("Trigger descriptions must be text")
            if not isinstance(item["steps"], list):
                raise TypeError("Trigger steps must be a list")
            steps = []
            for step in item["steps"]:
                _keys(
                    step,
                    allowed={"signal", "duration_s", "offset_s"},
                    required={"signal", "duration_s"},
                    name="trigger step",
                )
                signal = _logical_name(step["signal"], deferred=True)
                if "{qubit}" not in signal or qubit_id is not None:
                    resolved = _logical_name(signal, qubit_id=qubit_id)
                    if resolved not in mapping:
                        raise ValueError(f"Unmapped trigger signal: {resolved}")
                duration = _seconds(
                    step["duration_s"], "duration_s", positive=True, optional=True
                )
                offset = _seconds(step.get("offset_s", 0), "offset_s")
                steps.append(TriggerStep(signal, duration, offset))
            sequences[name] = TriggerSequence(tuple(steps), item.get("description", ""))
        restored_setup = DeviceSetup(
            devices=devices,
            tick_seconds=setup["tick_seconds"],
            protocols=sequences,
            calibration=calibration,
            name=setup["name"],
            timing_verified=setup.get("timing_verified", False),
            rtio_coarse_mu=setup.get("rtio_coarse_mu", 8),
            rtio_lane_count=setup.get("rtio_lane_count", 8),
            spectrum=_spectrum_config(setup.get("spectrum")),
        )
        coverage = setup.get("zone_coverage", {})
        if not isinstance(coverage, Mapping) or any(
            name not in devices
            or not isinstance(zones, (list, tuple))
            or not zones
            or any(not isinstance(z, str) or not z for z in zones)
            for name, zones in coverage.items()
        ):
            raise ValueError("Invalid zone_coverage")
        restored_setup._zone_coverage = {k: tuple(v) for k, v in coverage.items()}
        return cls(
            restored_setup,
            mapping,
            backend=data["backend"],
            transport=data.get("transport"),
        )

    def to_dict(self):
        """Return schema-v1 JSON data; custom backend objects stay Python-only."""
        if not isinstance(self.backend, str):
            raise TypeError("Serialize a backend by registered name, not an object")
        return {
            "schema_version": 1,
            "backend": self.backend,
            **({"transport": self.transport.to_dict()} if self.transport else {}),
            "setup": {
                **(
                    {
                        "zone_coverage": {
                            k: list(v) for k, v in self.setup.zone_coverage.items()
                        }
                    }
                    if getattr(self.setup, "zone_coverage", {})
                    else {}
                ),
                **(
                    {"spectrum": self.setup.spectrum.to_dict()}
                    if self.setup.spectrum is not None
                    else {}
                ),
                "name": self.setup.name,
                "tick_seconds": self.setup.tick_seconds,
                "timing_verified": self.setup.timing_verified,
                "rtio_coarse_mu": self.setup.rtio_coarse_mu,
                "rtio_lane_count": self.setup.rtio_lane_count,
                "devices": {
                    name: device.to_dict()
                    for name, device in self.setup.devices.items()
                },
                "calibration": {
                    name: asdict(calibration)
                    for name, calibration in self.setup.calibration.items()
                },
                "trigger_sequences": {
                    name: {
                        "description": sequence.description,
                        "steps": [
                            {
                                "signal": step.signal,
                                "duration_s": None
                                if step.duration is None
                                else float(as_fraction(step.duration, "time")),
                                "offset_s": float(as_fraction(step.offset, "time")),
                            }
                            for step in sequence.steps
                        ],
                    }
                    for name, sequence in self.setup.protocols.items()
                },
            },
            "signals": self.signal_map.to_dict(),
        }

    def _compilation_inputs(self, experiment):
        """Use the experiment's calibration while retaining lab wiring checks."""
        members = {qubit.uid: qubit for qubit in self.qubits}
        for qubit in self.qubits:
            selected = experiment.qubits.get(qubit.uid)
            if selected is not None and dict(selected.signals) != dict(qubit.signals):
                raise ValueError(
                    f"Experiment qubit {qubit.uid} has different signal topology "
                    "from LabConfig; update the laboratory wiring explicitly"
                )
        members.update(experiment.qubits)
        from .compiler.shared_fields import shared_field_members

        affected = shared_field_members(
            experiment.root, self.setup, self.signal_map, members
        )
        from .config.transport import normalize_transport

        transport = normalize_transport(self.transport)
        if transport is not None:
            missing = set(transport.bindings) - members.keys()
            if missing:
                raise ValueError(
                    f"Register all transport-bound atoms: {sorted(missing)}"
                )
            affected.update({uid: members[uid] for uid in transport.bindings})
            # Every registered position can be illuminated or obstruct a path;
            # omission from the experiment must not hide a spectator.
            affected.update(
                {uid: q for uid, q in members.items() if q.position is not None}
            )
        if getattr(self.setup, "zone_coverage", {}):
            affected.update(members)
        if affected.keys() - experiment.qubits.keys():
            experiment = copy.copy(experiment)
            experiment.qubits = MappingProxyType(
                {
                    **{uid: q.snapshot() for uid, q in affected.items()},
                    **experiment.qubits,
                }
            )
        setup = self._setup_for_qubits(
            tuple(members.values()), require_connections=False
        )
        if transport is not None:
            setup = copy.deepcopy(setup)
            setup._transport = transport
        if hasattr(self.setup, "_descriptor"):
            setup_metadata = {
                "descriptor": copy.deepcopy(self.setup._descriptor),
                "databases": copy.deepcopy(self.setup._database_sources),
                "connections": self.setup.connections(),
                "trigger_connections": self.setup.trigger_connections(),
            }
            experiment = copy.copy(experiment)
            experiment.metadata = {
                **experiment.metadata,
                "device_setup": setup_metadata,
            }
        if transport is not None:
            experiment = copy.copy(experiment)
            experiment.metadata = {
                **experiment.metadata,
                "transport": transport.to_dict(),
            }
        return experiment, setup

    def compile(self, experiment, **options):
        """Compile using the experiment's captured Qubit calibration values."""
        from .compiler.pipeline import compile_experiment

        if "backend" in options:
            raise TypeError("Set backend on LabConfig, not in compile options")
        experiment, setup = self._compilation_inputs(experiment)
        return compile_experiment(
            experiment, setup, self.signal_map, backend=self.backend, **options
        )

    def compile_stream(self, experiment, *, directory, **options):
        """Compile complete disk-backed chunks without retaining all point IR."""
        from .compiler.streaming import compile_stream

        if "backend" in options:
            raise TypeError("Set backend on LabConfig, not in compile options")
        experiment, setup = self._compilation_inputs(experiment)
        return compile_stream(
            experiment,
            setup,
            self.signal_map,
            backend=self.backend,
            directory=directory,
            **options,
        )


def reference_lab(qubits):
    """An illustrative board per qubit; triggers are NOT lab preparation."""
    devices = {}
    mapping = SignalMap()
    items = qubits.values() if hasattr(qubits, "values") else qubits
    for index, q in enumerate(items):
        for role, channel in (("raman", 0), ("rf", 1)):
            name = f"urukul{index}_ch{channel}"
            devices[name] = Device(
                name,
                "dds",
                bus=f"spi{index}",
                update_group=f"urukul{index}",
                square_latency_mu=80,
                nco_mode="continuous",
                nco_epoch_latency_mu=80,  # Offline assumption, not a measurement.
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
    return LabConfig(setup, mapping)


def initialization_lab(qubit):
    """Offline two-path optical pumping and camera trigger profile."""
    setup = DeviceSetup(
        name="initialization-offline-only",
        devices={
            "urukul0_ch0": Device(
                "urukul0_ch0",
                "dds",
                bus="spi0",
                update_group="urukul0",
                square_latency_mu=80,
                nco_mode="continuous",
                nco_epoch_latency_mu=80,
            ),
            "urukul0_ch1": Device(
                "urukul0_ch1",
                "dds",
                bus="spi0",
                update_group="urukul0",
                square_latency_mu=80,
                nco_mode="continuous",
                nco_epoch_latency_mu=80,
            ),
            "ttl0": Device("ttl0", "ttl", grid_mu=1, min_duration_mu=1),
        },
        protocols={
            "image": Protocol(
                (ProtocolStep("{qubit}.readout", 10 * us),),
                "Camera trigger only; illumination/data transfer require lab adapters",
            ),
        },
    )
    mapping = SignalMap(
        {
            "optics.pump_556": "urukul0_ch0",
            "optics.pushout_556": "urukul0_ch1",
            qubit.readout: "ttl0",
        }
    )
    return LabConfig(setup, mapping)


def reference_setup(qubits):
    """Backward-compatible tuple API for the centralized offline lab profile."""
    lab = reference_lab(qubits)
    return lab.setup, lab.signal_map
