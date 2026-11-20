"""Host sweep binding, point order and pulse parameter validation."""

import numpy as np
import pytest

from taiyidsl import (
    CompileError,
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    play,
    reference_lab,
    sweep,
    us,
)


@pytest.mark.parametrize("inactive", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
def test_pulse_sweep_reference_is_declared_and_identical(inactive, conflict):
    q = Qubit("q0")
    declared = SweepParameter("amp", [0.1, 0.2], "dimensionless")
    reference = SweepParameter(
        "amp" if conflict else "missing", [0.3, 0.4], "dimensionless"
    )
    exp = Experiment("bad_pulse_reference", [q])
    with exp.average(1):
        with exp.sweep(declared):
            with exp.realtime():
                with exp.repeat(0 if inactive else 1):
                    play(q.raman, Square(1 * us, amplitude=reference))
    with pytest.raises(CompileError, match="[Pp]arameter|[Ss]weep"):
        reference_lab([q]).compile(exp)


def test_equal_parameter_reference_need_not_have_same_identity():
    q = Qubit("q0")
    declared = SweepParameter("amp", [0.1, 0.2], "dimensionless")
    reference = SweepParameter("amp", [0.1, 0.2], "dimensionless")
    exp = Experiment("same_definition", [q])
    with exp.average(1):
        with exp.sweep(declared):
            with exp.realtime():
                play(q.raman, Square(1 * us, amplitude=reference))
    compiled = reference_lab([q]).compile(exp)
    assert [p.logical_events[0].params["amplitude"] for p in compiled.points] == [
        0.1,
        0.2,
    ]


def test_paired_pulse_values_share_one_index_across_an_inner_sweep():
    q = Qubit("q0")
    exp = Experiment("paired_pulse", [q])
    amp = SweepParameter("amp", np.array([0.2, 0.2, 0.7]), unit="")
    duration = SweepParameter("duration", np.array([4, 8, 12]), unit="us")
    phase = SweepParameter("phase", [0, 0.5], unit="rad")
    with exp.average(1):
        with sweep([amp, duration], chunk_size=2) as (amplitude, width):
            with exp.sweep(phase), exp.realtime():
                play(q.raman, Square(width, amplitude=amplitude, phase=phase))

    lab = reference_lab([q])
    compiled = lab.compile(exp, max_points=6)
    assert [axis["name"] for axis in compiled.axes] == ["amp", "phase"]
    assert compiled.axes[0]["paired_parameters"] == [
        {
            "name": "duration",
            "values": [4e-6, 8e-6, 12e-6],
            "unit": "us",
            "values_unit": "s",
        }
    ]
    assert compiled.host_execution_plan["chunk_axis"] == "amp"
    assert [c["points"] for c in compiled.host_execution_plan["chunks"]] == [
        [0, 1, 2, 3],
        [4, 5],
    ]
    expected = [
        (i, j, a, d_us, phi)
        for i, (a, d_us) in enumerate([(0.2, 4), (0.2, 8), (0.7, 12)])
        for j, phi in enumerate([0, 0.5])
    ]
    for point, (i, j, a, d_us, phi) in zip(compiled.points, expected, strict=True):
        assert point.axis_indices == (i, j)
        assert point.coordinates == {"amp": a, "duration": d_us / 1e6, "phase": phi}
        (event,) = point.logical_events
        assert event.params["amplitude"] == a
        assert event.duration_mu == d_us * 1000
        assert event.params["phase"] == phi
    with pytest.raises(CompileError, match="max_points"):
        lab.compile(exp, max_points=5)


@pytest.mark.parametrize(
    ("parameters", "error"),
    [
        ([], ValueError),
        ([SweepParameter("amp", [0.2]), [4]], TypeError),
        ([SweepParameter("amp", [0.2, 0.4]), SweepParameter("time", [4])], ValueError),
        ([SweepParameter("x", [1]), SweepParameter("x", [2])], ValueError),
    ],
)
def test_invalid_paired_sweep_is_rejected(parameters, error):
    exp = Experiment("invalid", [])
    with pytest.raises(error), exp.average(1), exp.sweep(parameters):
        pass


def test_paired_parameter_name_must_be_unique_across_nested_axes():
    q = Qubit("q0")
    exp = Experiment("duplicate", [q])
    amp = SweepParameter("amp", [0.2], unit="")
    width = SweepParameter("width", [4], unit="us")
    with exp.average(1), exp.sweep((amp, width)), exp.sweep(width), exp.realtime():
        play(q.raman, Square(width, amplitude=amp))
    with pytest.raises(CompileError, match="unique"):
        reference_lab([q]).compile(exp)
