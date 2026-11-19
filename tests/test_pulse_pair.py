import pytest

from examples.pulse_pair import pulse_pair
from taiyidsl import Experiment, MHz, Qubit, Square, ns, play, us


def test_source_tree_records_pulses_in_order():
    experiment = pulse_pair()
    (average,) = experiment.root.children
    (realtime,) = average.children
    first, wait, second = realtime.children
    assert (first.kind, wait.kind, second.kind) == ("play", "delay", "play")
    assert first.params["pulse"].duration == 4000 * ns
    assert wait.params["duration"] == 12 * us
    assert second.params["pulse"].phase == 0.5
    assert first.params["pulse"].phase == 0
    assert "pulse_pair.py:" in first.source


def test_experiment_owns_a_snapshot_of_calibration_and_parameters():
    q = Qubit("q0", signals={"rf": "q0.rf"}, parameters={"rf": {"amplitude": 0.25}})
    params = {"limits": [1]}
    experiment = Experiment("snapshot", [q], parameters=params)
    q.parameters.rf.amplitude = 0.75
    params["limits"][0] = 2
    assert experiment.qubits["q0"].parameters.rf.amplitude == 0.25
    assert experiment.parameters.to_dict() == {"limits": [1]}


def test_dimension_scope_and_failed_block():
    with pytest.raises(ValueError, match="time"):
        Square(2 * MHz)
    with pytest.raises(RuntimeError, match="active"):
        play("rf", Square(2 * us))
    experiment = Experiment("rollback", [])
    with (
        pytest.raises(ValueError, match="time"),
        experiment.average(1),
        experiment.realtime(),
    ):
        play("rf", Square(2 * MHz))
    assert experiment.root.children == []
    with experiment.average(1), experiment.realtime():
        play("rf", Square(2 * us))
