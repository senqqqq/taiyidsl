import pytest

from examples.schedule_pulse_pair import example_wiring, scheduled_pair
from taiyidsl import CompileError, Experiment, Square, ns, play
from taiyidsl.backends.artiq.backend import ArtiqBackend


def test_two_pulses_on_the_device_grid():
    events, duration = scheduled_pair()
    assert [(e.kind, e.start_mu, e.duration_mu) for e in events] == [
        ("play", 0, 4000),
        ("delay", 4000, 12000),
        ("play", 16000, 4000),
    ]
    assert duration == 20000
    assert [e.params["amplitude"] for e in (events[0], events[2])] == [0.25, 0.25]
    assert events[2].params["phase"] == 0.5


def test_inexact_grid_is_rejected():
    exp = Experiment("subgrid", [])
    with exp.average(1), exp.realtime():
        play("q0.rf", Square(3 * ns))
    setup, mapping = example_wiring()
    with pytest.raises(CompileError, match="4 mu timing grid"):
        ArtiqBackend().schedule(
            exp, exp.root.children[0].children, setup, mapping, {}, False, [], 10
        )
