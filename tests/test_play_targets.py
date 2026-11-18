"""Explicit public-field targets reserve all atoms while emitting one pulse."""

import pytest
from test_device_descriptor import databases

from taiyidsl import (
    CompileError,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    Square,
    add,
    ns,
    play,
    section,
)


def test_plain_public_play_captures_targets_once_and_checks_wiring_and_snapshots():
    descriptor, device_db, spec_db = databases()
    descriptor["signals"] = {
        "q0.rf": "nuclear_rf",
        "q1.rf": "nuclear_rf",
        "q1.clock": "clock",
    }
    descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
    a = Qubit("q0", signals={"rf": "q0.rf"}, parameters={"rf": {"frequency": 1e6}})
    b = Qubit(
        "q1",
        signals={"rf": "q1.rf", "clock": "q1.clock"},
        parameters={
            "rf": {"frequency": 3e6},
            "clock": {"frequency": 2e6},
        },
    )
    lab = LabConfig(
        DeviceSetup.from_descriptor(
            descriptor,
            device_db=device_db,
            spec_db=spec_db,
        ),
        qubits=[a, b],
    )
    pulse = Square(400 * ns, amplitude=0.2)
    with section("public") as segment:
        play(a.rf, pulse, targets=(a, b))
    assert segment.qubits == ("q0", "q1")
    exp = Experiment("public_then_clock", [a, b])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        with section(schedule="asap"):
            add(segment)
            play(b.clock, pulse)
    compiled = lab.compile(exp)
    first, clock = [e for e in compiled.points[0].logical_events if e.kind == "play"]
    assert first.params["frequency"] == 1e6
    assert first.params["global_operation"] == {
        "targets": ["q0", "q1"],
        "signals": ["q0.rf", "q1.rf"],
        "calibration": "q0",
        "drive": "rf",
    }
    assert first.params["operation_qubits"] == ["q0", "q1"]
    assert clock.start_mu == first.end_mu  # Different output, same affected atom.
    program = next(
        e for e in compiled.points[0].physical_events if e.kind == "spectrum_program"
    )
    assert len(program.params["cards"]["A"]["program"]["pulses"]) == 1

    incomplete = Experiment("incomplete", [a, b])
    with (
        incomplete.average(1),
        incomplete.realtime(),
        incomplete.triggered_sequence(group="qubit_awg"),
    ):
        play(a.rf, pulse, targets=(a,))
        for targets in ((a, a), (b,)):
            with pytest.raises(ValueError):
                play(a.rf, pulse, targets=targets)
        edited = Qubit.from_dict(b.to_dict())
        edited.parameters.rf.frequency = 5e6
        with pytest.raises(ValueError, match="snapshot"):
            play(a.rf, pulse, targets=(a, edited))
    with pytest.raises(CompileError, match="every configured public-field"):
        lab.compile(incomplete)

    # targets must describe real wiring, even without a sharing declaration.
    descriptor["channels"]["nuclear_rf"].pop("sharing")
    descriptor["signals"]["q1.rf"] = "clock"
    descriptor["signals"].pop("q1.clock")
    independent_b = Qubit(
        "q1", signals={"rf": "q1.rf"}, parameters={"rf": {"frequency": 3e6}}
    )
    independent = LabConfig(
        DeviceSetup.from_descriptor(
            descriptor,
            device_db=device_db,
            spec_db=spec_db,
        ),
        qubits=[a, independent_b],
    )
    wrong = Experiment("independent", [a, independent_b])
    with (
        wrong.average(1),
        wrong.realtime(),
        wrong.triggered_sequence(group="qubit_awg"),
    ):
        play(a.rf, pulse, targets=(a, independent_b))
    with pytest.raises(CompileError, match="share one physical output"):
        independent.compile(wrong)


def test_descriptor_infers_public_targets_from_lab_without_mutating_experiment():
    import copy

    descriptor, device_db, spec_db = databases()
    descriptor["channels"]["rydberg"] = {"instrument": "B", "channel": 1}
    descriptor["signals"] = {
        "q0.rf": "nuclear_rf",
        "q1.rf": "nuclear_rf",
        "q1.clock": "clock",
        "q2.rf": "rydberg",
    }
    descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
    a = Qubit("q0", signals={"rf": "q0.rf"}, parameters={"rf": {"frequency": 1e6}})
    b = Qubit(
        "q1",
        signals={"rf": "q1.rf", "clock": "q1.clock"},
        parameters={"rf": {}, "clock": {"frequency": 2e6}},
    )
    c = Qubit("q2", signals={"rf": "q2.rf"}, parameters={"rf": {}})
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    lab = LabConfig(setup, qubits=[a, b, c])
    a.parameters.rf.frequency = 1.5e6  # Experiment's current calibration wins.
    pulse = Square(400 * ns, amplitude=0.2)
    exp = Experiment("inferred", [a])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        with section(schedule="asap"):
            play(a.rf, pulse)
            play(b.clock, pulse)
    before = copy.deepcopy(exp.root)
    compiled = lab.compile(exp)
    assert set(exp.qubits) == {"q0"} and exp.root == before
    assert set(compiled.qubits) == {"q0", "q1"}  # q2 is unrelated.
    first, second = [e for e in compiled.points[0].logical_events if e.kind == "play"]
    assert first.params["frequency"] == 1.5e6
    assert first.params["global_operation"]["targets"] == ["q0", "q1"]
    assert second.start_mu == first.end_mu
    card = next(
        e for e in compiled.points[0].physical_events if e.kind == "spectrum_program"
    )
    assert len(card.params["cards"]["A"]["program"]["pulses"]) == 1
    with pytest.raises(CompileError, match="registered Qubit"):
        LabConfig(setup, qubits=[a]).compile(exp)
