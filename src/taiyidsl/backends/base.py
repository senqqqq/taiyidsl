"""Contracts for offline target compilation; no instrument access is implied."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..dsl.pulses import FlatTopGaussian, SampledPulse, Square
from ..ir import CompileError, PhysicalPlan


@dataclass(frozen=True)
class BackendCapabilities:
    """Accepted subset of the current DSL, not a promise of new DSL features."""

    device_kinds: frozenset[str]
    pulse_shapes: frozenset[str] = frozenset({"square"})
    operations: frozenset[str] = frozenset(
        {
            "play",
            "delay",
            "reserve",
            "shift_phase",
            "reset_frame",
            "reset_phase",
            "rz",
            "prepare",
            "measure",
            "acquire",
        }
    )
    average_execution: frozenset[str] = frozenset({"host"})
    sweep_execution: frozenset[str] = frozenset({"host"})

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, frozenset(getattr(self, name)))

    def to_dict(self):
        return {name: sorted(getattr(self, name)) for name in self.__dataclass_fields__}

    @property
    def supports_phase_modulation(self):
        return "trigplay" in self.operations

    def validate(self, root, setup):
        for device in setup.devices.values():
            if device.kind not in self.device_kinds:
                raise CompileError(
                    f"Backend does not support device kind {device.kind}"
                )

        def visit(node):
            if node.kind in {"play", "trigplay"}:
                pulse = node.params["pulse"]
                shape = "square" if isinstance(pulse, Square) else type(pulse).__name__
                if isinstance(pulse, FlatTopGaussian):
                    shape = "flat_top_gaussian"
                elif isinstance(pulse, SampledPulse):
                    shape = "sampled"
                if shape not in self.pulse_shapes:
                    if shape == "sampled":
                        raise CompileError(
                            f"Sampled pulses require a Spectrum AWG at {node.source}"
                        )
                    raise CompileError(f"Backend does not support pulse shape {shape}")
                if (
                    getattr(pulse, "phase_modulation", None) is not None
                    and not self.supports_phase_modulation
                ):
                    raise CompileError("This backend does not support phase modulation")
            if node.kind in {"average", "sweep"}:
                execution = node.params.get("execution", "host")
                if execution not in getattr(self, f"{node.kind}_execution"):
                    raise CompileError(
                        f"Backend does not support {node.kind} execution {execution}"
                    )
            structural = {
                "experiment",
                "average",
                "sweep",
                "serial",
                "parallel",
                "realtime",
                "repeat",
                "case",
                "barrier",
                "section_delay",
            }
            if node.kind not in structural and node.kind not in self.operations:
                raise CompileError(f"Backend does not support operation {node.kind}")
            for child in node.children:
                visit(child)

        visit(root)


class Backend(ABC):
    """A stateless compilation policy; target configuration belongs in setup.

    Logical scheduled events use the selected target's integer tick. They are
    NOT a portable source IR. A backend may override scheduling for a different
    timing model. Lowering owns physical resources, calibration, preparation,
    register quantization and phase realization. Export must remain offline.
    """

    name: str
    version: str
    target: str
    phase_policy: str
    capabilities: BackendCapabilities

    def describe(self):
        return {
            "name": self.name,
            "version": self.version,
            "target": self.target,
            "phase_policy": self.phase_policy,
            "capabilities": self.capabilities.to_dict(),
        }

    def validate_setup(self, setup, mapping):
        """Override for target-specific device/connection contracts."""

    def signal_grid(self, signal, setup, mapping):
        if signal not in mapping:
            return 1
        devices = [
            setup.devices[b.device]
            for b in mapping.bindings(signal)
            if b.device in setup.devices
        ]
        return math.lcm(*(d.grid_mu for d in devices)) if devices else 1

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
        from ..compiler.scheduling import Scheduler, resolve_frames

        scheduler = Scheduler(
            experiment,
            setup,
            coordinates,
            allow_quantization,
            diagnostics,
            max_events,
            mapping,
            self,
        )
        logical, duration = scheduler.serial(
            body, {uid: 0 for uid in experiment.qubits}
        )
        return resolve_frames(logical), duration

    @abstractmethod
    def lower(self, logical_events, setup, mapping, diagnostics) -> PhysicalPlan:
        """Produce physical events and pre-roll; may annotate logical resources."""

    @abstractmethod
    def export(self, compiled, output_dir, **options):
        """Write offline target artifacts; never submit or run an experiment."""

    def provenance(self):
        return {
            "backend": self.describe(),
            "target": self.target,
            "target_compile": "not_verified",
            "hardware": "not_verified",
            "phase_policy": self.phase_policy,
        }
