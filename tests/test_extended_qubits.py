"""Named routes, immutable calibrations and explicit control-phase ownership."""

import json
import math

import pytest
from test_phase_reset_artiq import _lab, _pulse
from test_section_scheduling import lab_for

from taiyidsl import (
    DriveParameters,
    Experiment,
    PairCalibration,
    Position,
    Qubit,
    QubitOperations,
    QubitParameters,
    QubitStore,
    SignalMap,
    Square,
    TransitionParameters,
    add,
    delay,
    ns,
    play,
    qubit_operation,
    reset_phase,
    rz,
    section,
    shift_phase,
    um,
)
from taiyidsl.artiq import verify_equivalence


def clock_qubit(frame=None):
    return Qubit(
        "q0",
        QubitParameters(
            rf=DriveParameters(
                frequency=1e6,
                pi_duration=800e-9,
                pi2_duration=400e-9,
                pi_amplitude=0.3,
                pi2_amplitude=0.17,
            ),
            drives={"clock": DriveParameters(frequency=2e6, pi_duration=800e-9)},
            drive_frames={"clock": frame},
            transitions={"clock": TransitionParameters("g_up", "e_up", 5e14)},
            drive_transitions={"clock": "clock"},
            position=Position(1 * um, 2 * um, 0, "trap_plane"),
        ),
    )


def test_old_snapshot_and_store_v1_remain_readable(tmp_path):
    q = Qubit("q")
    assert set(q.params.to_dict()) == {"rf", "raman", "readout", "extensions"}
    assert set(q.params.rf.to_dict()) == {
        "frequency",
        "amplitude",
        "pi_duration",
        "pi2_duration",
    }
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({"schema_version": 1, "qubits": [q.to_dict()], "history": []})
    )
    before = path.read_bytes()
    store = QubitStore.load(path)
    assert store["q"].fingerprint == q.fingerprint
    assert path.read_bytes() == before
    revised = store.update("q", {"rf.pi2_amplitude": 0.0})
    assert revised.params.rf.rotation_amplitude("pi2") == 0
    assert q.params.rf.rotation_amplitude("pi2") == 0.5


def test_named_parameters_roundtrip_units_and_updates():
    q = clock_qubit("clock_control")
    assert q.snapshot().fingerprint == q.fingerprint
    assert q.params.position.x == pytest.approx(1e-6)
    assert q.drive("clock").frequency == 2e6
    assert q.params.transitions["clock"].resonance_frequency == 5e14
    assert q.signal("clock") == "q0.clock"
    with pytest.raises(TypeError):
        q.params.drives["clock"] = DriveParameters()
    store = QubitStore([q])
    updated = store.update(
        "q0", {"drives.clock.pi2_amplitude": 0.25, "position.x": 3 * um}
    )
    assert updated.drive("clock").rotation_amplitude("pi2") == 0.25
    assert updated.params.position.x == pytest.approx(3e-6)
    with pytest.raises(ValueError, match="namespace"):
        store.update("q0", {"drives.missing.pi_duration": 1e-6})


@pytest.mark.parametrize("frame", [None, "clock_control"])
def test_clock_does_not_implicitly_inherit_spin_rz(frame):
    q = clock_qubit(frame)
    lab = lab_for()
    lab.signal_map = SignalMap({q.rf: "rf0", q.signal("clock"): "raman0"})
    exp = Experiment("explicit_frames", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        rz(q, 0.4)
        play(q.rf, Square(400 * ns))
        play(q.signal("clock"), Square(400 * ns))
        if frame:
            shift_phase(q.phase_frame(frame), 0.7)
            play(q.signal("clock"), Square(400 * ns))
            reset_phase(q.phase_frame(frame))
            play(q.signal("clock"), Square(400 * ns))
    result = lab.compile(exp)
    pulses = [e for e in result.points[0].logical_events if e.kind == "play"]
    assert [p.params["phase"] for p in pulses] == pytest.approx(
        [-0.4, 0] + ([0.7, 0] if frame else [])
    )
    assert all(p.params["qubit"] == q.uid for p in pulses)
    assert pulses[1].params["frequency"] == 2e6
    assert result.frame_phases()[q.frame] == pytest.approx(-0.4)


def test_named_frame_entry_state_is_preserved_in_awg_artifact():
    from test_spectrum_backend import group_program

    q = clock_qubit("clock_control")
    lab = lab_for()
    lab.signal_map = SignalMap({q.signal("clock"): "raman0"})
    exp = Experiment("named_entry", [q])
    frame = q.phase_frame("clock_control")
    with exp.average(1), exp.realtime():
        shift_phase(frame, 0.7)
        with exp.triggered_sequence(group="g"):
            play(q.signal("clock"), Square(400 * ns))
    compiled = lab.compile(exp)
    group = group_program(compiled)
    assert group["entry_frames"][frame]["frame_phase"] == pytest.approx(0.7)
    assert group["terminal_frames"][frame]["frame_phase"] == pytest.approx(0.7)


@pytest.mark.parametrize("shape", ["square", "ram", "polar"])
def test_named_frame_reaches_actual_dds_export(shape):
    _, lab = _lab(shape)
    q = clock_qubit("clock_control")
    lab.signal_map = SignalMap({q.signal("clock"): "dds"})
    exp = Experiment("named_dds", [q])
    with exp.average(1), exp.realtime():
        shift_phase(q.phase_frame("clock_control"), 0.31)
        play(q.signal("clock"), _pulse(shape))
        delay(q.signal("clock"), 4000 * ns)
        reset_phase(q.phase_frame("clock_control"))
        play(q.signal("clock"), _pulse(shape), phase=0.2)
    compiled = lab.compile(exp)
    verify_equivalence(compiled)
    pulse = next(e for e in compiled.points[0].logical_events if e.kind == "play")
    assert pulse.params["phase"] == pytest.approx(0.31)


def test_operations_use_independent_amplitudes_and_snapshot():
    q, ops = clock_qubit(), QubitOperations()
    first, second = ops.x90(q), ops.y180(q)
    assert first.finalized and first.qubits == (q.uid,)
    assert first.to_node().children[0].params["pulse"].amplitude == 0.17
    assert second.to_node().children[0].params["pulse"].amplitude == 0.3
    assert second.to_node().children[0].params["pulse"].phase == math.pi / 2
    assert first.to_node().params["calibration"]["fingerprint"] == q.fingerprint
    assert ops.clock(q).to_node().children[0].params["signal"] == "q0.clock"
    with pytest.raises(ValueError, match="declared"):
        ops.clock(Qubit("missing"))


def test_unassociated_clock_delay_still_reserves_its_qubit_in_asap():
    q = clock_qubit()
    lab = lab_for()
    lab.signal_map = SignalMap({q.rf: "rf0", q.signal("clock"): "raman0"})
    exp = Experiment("clock_resource", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section(schedule="asap"):
            delay(q.signal("clock"), 400 * ns)
            play(q.rf, Square(400 * ns))
    compiled = lab.compile(exp)
    pulse = next(e for e in compiled.points[0].logical_events if e.kind == "play")
    assert pulse.start_mu * compiled.setup.tick_seconds == pytest.approx(400e-9)


def test_operation_shape_override_keeps_total_duration_and_snapshot():
    q, ops = clock_qubit(), QubitOperations()
    original = q.fingerprint
    shaped = ops.x90(
        q,
        duration=800 * ns,
        frequency=3e6,
        waveform="flat_top_gaussian",
        waveform_parameters={"sigma": 40 * ns, "sample_interval": 0.8 * ns},
    )
    node = shaped.to_node()
    pulse = node.children[0].params["pulse"]
    assert float(pulse.flat_duration) == pytest.approx(480e-9)
    assert pulse.frequency == 3e6
    assert q.fingerprint == original
    exp = Experiment("shape_override", [q, Qubit("q1")])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        add(shaped)
    compiled = lab_for().compile(exp)
    event = next(e for e in compiled.points[0].logical_events if e.kind == "play")
    assert event.duration_mu * compiled.setup.tick_seconds == pytest.approx(800e-9)
    assert event.params["frequency"] == 3e6
    with pytest.raises(ValueError, match="shorter"):
        ops.x90(
            q,
            duration=100 * ns,
            waveform="flat_top_gaussian",
            waveform_parameters={"sigma": 40 * ns, "sample_interval": 4 * ns},
        )


def test_pair_revision_and_cz_inserts_once(tmp_path):
    q0, q1 = Qubit("q0"), Qubit("q1")
    pair = PairCalibration(
        (q0.uid, q1.uid),
        "laboratory_CZ",
        status="calibrated",
        qubit_revisions={q.uid: q.revision for q in (q0, q1)},
        qubit_fingerprints={q.uid: q.fingerprint for q in (q0, q1)},
    )
    assert (
        PairCalibration.load(pair.save(tmp_path / "pair.json")).fingerprint
        == pair.fingerprint
    )
    with pytest.raises(ValueError, match="Stale"):
        pair.validate(Qubit("q0", revision=1), q1)
    with pytest.raises(ValueError, match="order"):
        pair.validate(q1, q0)

    @qubit_operation
    def implementation(a, b):
        play(a.rf, Square(800 * ns))
        play(b.rf, Square(800 * ns))

    gate = QubitOperations(cz_impl=implementation, pair_calibrations=[pair]).cz(q0, q1)
    assert len(gate.to_node().children) == 1
    exp = Experiment("pair", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        add(gate)
    compiled = lab_for().compile(exp)
    assert sum(e.kind == "play" for e in compiled.points[0].logical_events) == 2

    saved = implementation(q0, q1)

    def prepared_implementation(a, b):
        shift_phase(a.frame, 0.1)
        return saved

    mixed = QubitOperations(cz_impl=prepared_implementation).cz(q0, q1)
    assert [n.kind for n in mixed.to_node().children] == ["shift_phase", "serial"]
    assert len(mixed.to_node().children[1].children) == 2
