"""Compile one scan point and enforce its cross-point acquisition contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..ir import CompiledPoint, CompileError, acquisition_targets
from ..native.declarations import (
    bind_native_point,
    validate_native_resources,
    validate_native_timed_window,
)
from .preparation import CompilationInputs


class PointSink(Protocol):
    """Synchronous storage boundary, not an alternate compilation policy.

    The compiler calls prepare once, then add in global point order. Diagnostics
    are borrowed only during add and cleared immediately afterward; a sink that
    retains them must copy them. Publishing/finalizing a disk store belongs to
    its writer, after the compiler has returned the complete artifact header.
    """

    def prepare(self, host_execution_plan: dict) -> None: ...

    def add(
        self, point: CompiledPoint, native: dict | None, diagnostics: list
    ) -> None: ...


@dataclass
class PointCompilation:
    point: CompiledPoint
    native: dict | None
    acquisition_signature: tuple


def compile_point(
    inputs: CompilationInputs,
    experiment,
    index,
    axis_indices,
    previous_acquisition_signature,
    diagnostics,
    *,
    allow_quantization,
    max_events_per_point,
):
    coordinates = {
        parameter.name: parameter.values[value]
        for parameter, value in zip(inputs.parameters, axis_indices)
    }
    logical, duration = inputs.target.schedule(
        experiment,
        inputs.body,
        inputs.setup,
        inputs.mapping,
        coordinates,
        allow_quantization,
        diagnostics,
        max_events_per_point,
    )
    acquisitions = [
        (event.params["handle"], uid)
        for event in logical
        for uid in acquisition_targets(event)
    ]
    if len(acquisitions) != len(set(acquisitions)):
        raise CompileError(
            "Each handle/qubit may be measured once per shot; use distinct handles"
        )
    signature = tuple(sorted(acquisitions))
    if (
        previous_acquisition_signature is not None
        and signature != previous_acquisition_signature
    ):
        raise CompileError(
            "All scan points must share the same acquisition handles/qubits"
        )
    physical = inputs.target.lower(logical, inputs.setup, inputs.mapping, diagnostics)
    native_point = None
    if inputs.native_contract is not None:
        validate_native_resources(inputs.native_contract, inputs.setup, physical)
        for event in physical.events:
            if event.kind == "spectrum_program":
                reference = event.params["phase_reference"]
                reference["logical_time_reference"] = (
                    "control_epoch_after_native_prefix"
                )
                if reference.get("mode", "shot") == "shot":
                    reference["epoch"] = "control_epoch_after_native_prefix"
        native_point = bind_native_point(
            inputs.native_contract, coordinates, index, inputs.setup, logical
        )
        validate_native_timed_window(native_point, inputs.setup, physical)
    point = CompiledPoint(
        index,
        {key: float(value) for key, value in coordinates.items()},
        axis_indices,
        sorted(logical, key=lambda event: event.start_mu),
        physical.events,
        duration,
        physical.origin_mu,
    )
    return PointCompilation(point, native_point, signature)
