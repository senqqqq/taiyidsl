"""Source provenance remains at the experiment call sites across DSL modules."""

import sys

from taiyidsl import Experiment, Qubit, Square, play, us


def test_direct_experiment_scopes_and_instruction_keep_exact_source_lines():
    q = Qubit("q")
    pulse = Square(4 * us)
    line = sys._getframe().f_lineno + 1
    experiment = Experiment("source", [q])
    assert experiment.root.source == f"{__file__}:{line}"
    average_line = sys._getframe().f_lineno + 1
    with experiment.average(1):
        realtime_line = sys._getframe().f_lineno + 1
        with experiment.realtime():
            play_line = sys._getframe().f_lineno + 1
            play(q.rf, pulse)
    average = experiment.root.children[0]
    realtime = average.children[0]
    assert average.source == f"{__file__}:{average_line}"
    assert realtime.source == f"{__file__}:{realtime_line}"
    assert realtime.children[0].source == f"{__file__}:{play_line}"


def test_ordinary_helper_body_retains_its_own_instruction_source():
    q = Qubit("q")
    pulse = Square(4 * us)
    line = None

    def helper():
        nonlocal line
        line = sys._getframe().f_lineno + 1
        play(q.rf, pulse)

    experiment = Experiment("helper_source", [q])
    with experiment.average(1), experiment.realtime():
        helper()
    call = experiment.root.children[0].children[0].children[0]
    assert call.source == f"{__file__}:{line}"
