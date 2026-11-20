"""Prepare once, compile points in host-axis order, then assemble an artifact."""

from itertools import product

from .artifact import assemble_artifact
from .points import PointSink, compile_point
from .preparation import prepare_compilation, sweep_axes


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
    _point_sink: PointSink | None = None,
):
    inputs = prepare_compilation(
        experiment, setup, signal_map, backend, calibration, max_points
    )
    host_execution_plan = inputs.host_execution_plan
    if _point_sink is not None:
        _point_sink.prepare(host_execution_plan)
    axes = sweep_axes(inputs.sweep_groups)
    points, native_points, diagnostics = [], [], []
    acquisition_signature = None
    indices = product(*(range(len(group[0].values)) for group in inputs.sweep_groups))
    for index, axis_indices in enumerate(indices):
        result = compile_point(
            inputs,
            experiment,
            index,
            axis_indices,
            acquisition_signature,
            diagnostics,
            allow_quantization=allow_quantization,
            max_events_per_point=max_events_per_point,
        )
        acquisition_signature = result.acquisition_signature
        if _point_sink is None:
            if result.native is not None:
                native_points.append(result.native)
            points.append(result.point)
        else:
            _point_sink.add(result.point, result.native, diagnostics)
            diagnostics.clear()
    if _point_sink is None and host_execution_plan.get("chunk_policy") == "auto":
        from .chunking import resolve_auto_chunks

        host_execution_plan = resolve_auto_chunks(points, host_execution_plan)
    if (
        inputs.target.name == "artiq_spectrum"
        and host_execution_plan["chunk_axis"] is not None
        and _point_sink is None
    ):
        from taiyidsl.backends.artiq_spectrum.banks import validate_chunk_capacity

        validate_chunk_capacity(points, host_execution_plan)
    return assemble_artifact(
        experiment,
        inputs,
        points,
        native_points,
        axes,
        diagnostics,
        host_execution_plan,
    )
