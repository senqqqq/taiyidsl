"""Detach compilation inputs and validate the static host sweep structure."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass

from ..backends import Backend, get_backend
from ..config.hardware import DeviceSetup, SignalMap
from ..dsl.pulses import SampledPulse
from ..dsl.values import SweepParameter, sweep_parameter_group
from ..ir import CompileError
from ..native.declarations import split_native_body
from .chunking import build_execution_plan
from .shared_fields import validate_shared_fields


@dataclass
class CompilationInputs:
    """Compiler-owned configuration and source tree for all points in one scan."""

    target: Backend
    setup: DeviceSetup
    mapping: SignalMap
    root: object
    average: object
    body: list
    sweep_groups: list[tuple[SweepParameter, ...]]
    native_contract: dict | None
    host_execution_plan: dict


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.children)


def source_sample_pulses(root):
    """Yield one declaration for each distinct source buffer in a scan."""
    seen = set()
    for node in _walk([root]):
        pulse = node.params.get("pulse")
        if isinstance(pulse, SampledPulse) and pulse.sample_digest not in seen:
            seen.add(pulse.sample_digest)
            yield pulse


def _parameter_references(value):
    """Inspect source values before inactive branches or repeats disappear."""
    if isinstance(value, SweepParameter):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _parameter_references(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _parameter_references(item)
    elif is_dataclass(value):
        for item in fields(value):
            yield from _parameter_references(getattr(value, item.name))


def _snapshot_inputs(experiment, setup, signal_map, backend, calibration):
    target = copy.deepcopy(get_backend(backend))
    if (
        len(experiment.root.children) != 1
        or experiment.root.children[0].kind != "average"
    ):
        raise CompileError("Experiment requires exactly one top-level average block")
    if len(experiment._stack) != 1:
        raise CompileError("Cannot compile an experiment while a builder block is open")
    setup = copy.deepcopy(setup)
    prepare_setup = getattr(target, "prepare_setup", None)
    if prepare_setup is not None:
        setup = prepare_setup(setup)
    if calibration is not None:
        setup.calibration = copy.deepcopy(dict(calibration))
        setup.__post_init__()
    mapping = SignalMap(copy.deepcopy(dict(signal_map)))
    root = copy.deepcopy(experiment.root)
    target.capabilities.validate(root, setup)
    target.validate_setup(setup, mapping)
    validate_shared_fields(root, setup, mapping, experiment.qubits)
    setup._sample_assets = {
        pulse.sample_digest: pulse.sample_asset()
        for pulse in source_sample_pulses(root)
    }
    return target, setup, mapping, root


def _parse_sweeps(root):
    average = root.children[0]
    body, groups, sweep_nodes = average.children, [], []
    while len(body) == 1 and body[0].kind == "sweep":
        sweep_nodes.append(body[0])
        groups.append(sweep_parameter_group(body[0].params["parameter"]))
        body = body[0].children
    if any(node.kind in {"average", "sweep"} for node in _walk(body)):
        raise CompileError(
            "Sweeps must form one outer nested chain, before the shot body"
        )
    parameters = [parameter for group in groups for parameter in group]
    axes_by_name = {parameter.name: parameter for parameter in parameters}
    if len(axes_by_name) != len(parameters):
        raise CompileError("Sweep axis names must be unique")
    for node in _walk(body):
        for reference in _parameter_references(node.params):
            if axes_by_name.get(reference.name) != reference:
                raise CompileError(
                    f"Unbound or conflicting parameter {reference.name} "
                    f"with its sweep definition at {node.source}"
                )
    return average, body, groups, sweep_nodes


def prepare_compilation(
    experiment, setup, signal_map, backend, calibration, max_points
):
    target, setup, mapping, root = _snapshot_inputs(
        experiment, setup, signal_map, backend, calibration
    )
    average, body, groups, sweep_nodes = _parse_sweeps(root)
    if "native_artiq" in experiment.metadata:
        raise CompileError("native_artiq metadata is reserved for native declarations")
    body, native_contract = split_native_body(body)
    axis_parameters = [group[0] for group in groups]
    count = math.prod(len(parameter.values) for parameter in axis_parameters)
    if count > max_points:
        raise CompileError("Sweep exceeds max_points")
    plan = build_execution_plan(axis_parameters, sweep_nodes, average)
    if plan.get("chunk_policy") == "auto" and target.name != "artiq_spectrum":
        raise CompileError("Automatic chunk capacity currently requires artiq_spectrum")
    return CompilationInputs(
        target, setup, mapping, root, average, body, groups, native_contract, plan
    )


def _parameter_descriptor(parameter):
    return {
        "name": parameter.name,
        "values": [float(value) for value in parameter.values],
        "unit": parameter.unit,
        "values_unit": {
            "time": "s",
            "frequency": "Hz",
            "phase": "rad",
            "length": "m",
            "dimensionless": "dimensionless",
        }[parameter.dimension],
    }


def sweep_axes(groups):
    axes = []
    for first, *paired in groups:
        axis = _parameter_descriptor(first)
        if paired:
            axis["paired_parameters"] = [_parameter_descriptor(p) for p in paired]
        axes.append(axis)
    return axes
