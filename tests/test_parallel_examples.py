"""Observable timing and export contracts for the parallel authoring examples."""

import runpy
from pathlib import Path

import pytest

from taiyidsl import (
    DriveParameters,
    Experiment,
    Qubit,
    QubitParameters,
    Square,
    play,
    reference_lab,
    us,
)
from taiyidsl.artiq import verify_equivalence

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "parallel_experiments.py"


@pytest.fixture(scope="module")
def builders():
    return runpy.run_path(str(EXAMPLE))


def test_precise_offsets_and_shared_join(builders):
    qs = [Qubit("q0"), Qubit("q1")]
    compiled = reference_lab(qs).compile(builders["build_precise_offsets"](qs))
    point = compiled.points[0]
    assert [
        (e.signal, e.start_mu, e.duration_mu, e.params["phase"])
        for e in point.logical_events
        if e.kind == "play"
    ] == [
        ("q0.raman", 0, 4000, 0),
        ("q1.raman", 2000, 6000, 0),
        ("q0.raman", 6000, 4000, 1),
    ]
    assert len(compiled.acquisition_plan()) == 1
    acquisition = compiled.acquisition_plan()[0]
    assert acquisition["qubits"] == ["q0", "q1"]
    assert acquisition["start_s"] == pytest.approx(10e-6)
    assert point.duration_mu == 20000
    assert verify_equivalence(compiled)["status"] == "passed"


def test_pulse_pair_is_one_branch_and_does_not_mutate_template(builders):
    qs = [Qubit("q0"), Qubit("q1")]
    pulse = Square(4 * us, phase=0.25)
    exp = Experiment("function_branch", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                builders["pulse_pair"](exp, qs[0], pulse, second_phase=0.75)
                play(qs[1].raman, pulse, duration=8 * us)
    point = reference_lab(qs).compile(exp).points[0]
    plays = [e for e in point.logical_events if e.kind == "play"]
    assert [(e.signal, e.start_mu) for e in plays] == [
        ("q0.raman", 0),
        ("q1.raman", 0),
        ("q0.raman", 6000),
    ]
    assert [e.params["phase"] for e in plays] == [0.25, 0.25, 0.75]
    assert pulse.phase == 0.25
    assert pulse.duration == 4 * us


@pytest.mark.parametrize("mode", ["cyclic", "sequential"])
def test_parallel_rabi_defaults_zero_point_and_export(builders, mode):
    qs = [
        Qubit("q0"),
        Qubit(
            "q1",
            QubitParameters(raman=DriveParameters(frequency=90e6, amplitude=0.25)),
        ),
    ]
    exp = builders["build_parallel_rabi"](
        qs, (0 * us, 4 * us, 8 * us), shots=2, mode=mode
    )
    compiled = reference_lab(qs).compile(exp)
    assert len(compiled.points) == 3
    for point, duration in zip(compiled.points, (0, 4000, 8000)):
        plays = [e for e in point.logical_events if e.kind == "play"]
        assert [(e.start_mu, e.duration_mu) for e in plays] == [
            (0, duration),
            (0, duration),
        ]
        assert [(e.params["frequency"], e.params["amplitude"]) for e in plays] == [
            (80e6, 0.5),
            (90e6, 0.25),
        ]
        plan = compiled.acquisition_plan(point.index)
        assert len(plan) == 1
        assert plan[0]["qubits"] == ["q0", "q1"]
        assert plan[0]["start_s"] == pytest.approx(duration * 1e-9)
        assert len([e for e in point.physical_events if e.kind == "ttl_on"]) == 1
    assert not any(
        e.kind in {"dds_on", "dds_off"} for e in compiled.points[0].physical_events
    )
    assert any(e.kind == "dds_commit" for e in compiled.points[0].physical_events)
    assert verify_equivalence(compiled)["status"] == "passed"


def test_parallel_rabi_camera_mapping_is_a_compile_time_choice(builders):
    qs = [Qubit("q0"), Qubit("q1")]
    exp = builders["build_parallel_rabi"](
        qs, (4 * us,), shots=1, readout_signal="camera.trigger"
    )
    lab = reference_lab(qs)
    lab.signal_map["camera.trigger"] = lab.signal_map[qs[0].readout]
    compiled = lab.compile(exp)
    assert compiled.acquisition_plan()[0]["signal"] == "camera.trigger"
