"""Pulse parameter references must match their declared scan, even if inactive."""

import pytest

from taiyidsl import (
    CompileError,
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    play,
    reference_lab,
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
