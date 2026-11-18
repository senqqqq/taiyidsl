"""Frame resets remain composable across reused sections and final scheduling."""

import copy

import numpy as np
import pytest
from test_spectrum_phase_modes import phase_lab, pulse_program, samples

from taiyidsl import (
    Experiment,
    Qubit,
    Square,
    add,
    delay,
    ns,
    play,
    reset_frame,
    reset_phase,
    rz,
    section,
    shift_phase,
    triggered_sequence,
)
from taiyidsl.scheduling import resolve_frames


def test_reused_reset_section_uses_each_final_insertion_time():
    q = Qubit("q")
    with section() as gate:
        reset_phase(q.frame)
        shift_phase(q.frame, 0.2)
        play(q.rf, Square(400 * ns, frequency=11e6, phase=0.3))
    exp = Experiment("reused_reset", [q])
    with exp.average(1), exp.realtime(), triggered_sequence(group="g"):
        delay(80 * ns)
        add(gate)
        delay(120 * ns)
        rz(q, 0.9)
        add(gate)
    compiled = phase_lab("continuous").compile(exp)
    logical = compiled.points[0].logical_events
    commands = [event for event in logical if event.kind == "reset_phase"]
    assert [event.start_mu for event in commands] == [400, 3000]
    assert [event.params["carrier_reference_mu"] for event in commands] == [400, 3000]
    assert compiled.frame_phases() == pytest.approx({"q": 0.2})
    assert compiled.virtual_z_frames() == pytest.approx({"q": 0})
    before = copy.deepcopy(logical)
    resolve_frames(logical)
    assert logical == before
    program = pulse_program(phase_lab("continuous"), exp)
    first, second = program["pulses"]
    np.testing.assert_array_equal(samples(program, first), samples(program, second))


def test_reset_retains_only_subsequent_rz_provenance():
    q = Qubit("q")
    exp = Experiment("terminal_reset", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        shift_phase(q.frame, 0.3)
        rz(q, 0.7)
        reset_frame(q.frame)
        shift_phase(q.frame, 0.2)
        play(q.rf, Square(400 * ns, frequency=11e6))
        rz(q, 0.9)
    compiled = phase_lab().compile(exp)
    assert compiled.frame_phases() == pytest.approx({"q": -0.7})
    assert compiled.virtual_z_frames() == pytest.approx({"q": 0.9})
    snapshot = compiled.frame_phases()
    snapshot["q"] = 123
    assert compiled.frame_phases() == pytest.approx({"q": -0.7})
    with pytest.raises(IndexError):
        compiled.frame_phases(1)


def test_sequence_reference_validation_and_functional_api():
    q = Qubit("q")
    exp = Experiment("reference_api", [q])
    with exp.average(1), exp.realtime():
        with pytest.raises(ValueError, match="phase_reference"):
            exp.triggered_sequence(group="g", phase_reference="segment")
        delay("camera", 80 * ns)
        with triggered_sequence(group="g", phase_reference="shot"):
            play(q.rf, Square(400 * ns, frequency=11e6))
    program = pulse_program(phase_lab(), exp)
    assert program["pulses"][0]["phase_rad"] != 0


@pytest.mark.parametrize("mode", ["epoch", "continuous"])
@pytest.mark.parametrize("reference", ["sequence", "shot"])
def test_preparation_length_only_changes_samples_with_shot_reference(mode, reference):
    outputs = []
    for preparation_ns in (80, 120):
        q = Qubit("q")
        exp = Experiment("preparation_independence", [q])
        with exp.average(1), exp.realtime():
            delay("camera", preparation_ns * ns)
            rz(q, 0.3)
            with exp.triggered_sequence(group="g", phase_reference=reference):
                play(q.rf, Square(400 * ns, frequency=11e6, amplitude=0.5))
                delay(80 * ns)
                play(q.rf, Square(400 * ns, frequency=13e6, amplitude=0.5))
        program = pulse_program(phase_lab(mode), exp)
        outputs.append(np.concatenate([samples(program, p) for p in program["pulses"]]))
    if reference == "sequence":
        np.testing.assert_array_equal(*outputs)
    else:
        assert not np.array_equal(*outputs)
