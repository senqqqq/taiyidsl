"""Unified role parameters, legacy snapshots and explicit public-field gates."""

import json
from dataclasses import replace

import pytest
from test_section_scheduling import lab_for

from taiyidsl import (
    CalibrationProposal,
    Experiment,
    Qubit,
    QubitOperations,
    QubitStore,
    SignalMap,
    add,
    delay,
    ns,
    reset_phase,
    section,
    shift_phase,
    um,
    us,
)


def role_qubit(uid="q0"):
    return Qubit(
        uid,
        signals={"rf": f"drive/{uid}", "clock": f"optical/{uid}"},
        parameters={
            "rf": {
                "frequency": 1e6,
                "pi2_duration": 400 * ns,
                "pi2_amplitude": 0.17,
                "pi_amplitude": 0.3,
            },
            "clock": {
                "frequency": 2e6,
                "pi_duration": 800 * ns,
                "pi_amplitude": 0.25,
                "frame": "clock_control",
                "resonance_frequency": 5e14,
            },
        },
        position={"x": 1 * um, "y": 0, "z": 0, "coordinate_system": "traps"},
    )


def test_uniform_roles_are_explicit_and_updates_remain_transactional(tmp_path):
    q = role_qubit()
    assert q.rf == "drive/q0" and q.clock == "optical/q0"
    assert q.parameters.clock.frequency == 2e6
    assert q.parameters.clock.resonance_frequency == 5e14
    assert q.parameters.rf.pi_duration is None
    assert q.position.x == pytest.approx(1e-6)
    assert q.snapshot().fingerprint == q.fingerprint
    assert replace(q, revision=2).signals == q.signals
    with pytest.raises((AttributeError, TypeError)):
        q.snapshot().parameters.rf = {}
    with pytest.raises(TypeError):
        q.signals["rf"] = "other"
    with pytest.raises(ValueError, match="pi_duration"):
        QubitOperations().x180(q)
    empty = Qubit("empty", signals={"rf": "empty.rf"}, parameters={"rf": {}})
    with pytest.raises(ValueError, match="frequency"):
        QubitOperations().x90(empty)
    path = tmp_path / "qubits.json"
    store = QubitStore([q], path=path)
    store.save()
    assert json.loads(path.read_text())["schema_version"] == 3
    loaded = QubitStore.load(path)
    proposal = CalibrationProposal(
        q.uid,
        q.revision,
        q.fingerprint,
        {"rf.pi2_amplitude": 0.19, "clock.frequency": 3e6},
        {"rf.pi2_amplitude": "1", "clock.frequency": "Hz"},
        {"r_squared": 0.99},
        "role_calibration",
    )
    revised = loaded.apply(proposal)
    assert revised.revision == 1
    assert revised.parameters.rf.pi2_amplitude == 0.19
    assert q.parameters.rf.pi2_amplitude == 0.17
    assert revised.signals == q.signals
    assert revised.position == q.position
    with pytest.raises(ValueError, match="Stale"):
        loaded.apply(proposal)
    assert loaded.update("q0", {"position.x": 3 * um}).position.x == pytest.approx(3e-6)


def test_new_extension_namespaces_preserve_snapshots_and_update_guards(tmp_path):
    original = role_qubit()
    store = QubitStore([original], path=tmp_path / "qubits.json")
    stale = CalibrationProposal(
        original.uid,
        original.revision,
        original.fingerprint,
        {"rf.pi2_amplitude": 0.2},
        {"rf.pi2_amplitude": "1"},
        {},
        "demonstration",
    )
    old_snapshot = store[original.uid]
    revised = store.update(
        original.uid,
        {
            "parameters.rf.pi2_amplitude": 0.22,
            "parameters.extensions.lab.clock_detuning_hz": 500.0,
            "extensions.lab.notes.description": "demo, not measured",
        },
        persist=True,
    )
    assert revised.revision == old_snapshot.revision + 1
    assert revised.parameters.rf.pi2_amplitude == 0.22
    assert revised.parameters.extensions["lab"]["clock_detuning_hz"] == 500
    assert revised.parameters.extensions["lab"]["notes"]["description"] == (
        "demo, not measured"
    )
    assert not original.parameters.extensions and not old_snapshot.parameters.extensions
    assert old_snapshot.parameters.rf.pi2_amplitude == 0.17
    assert QubitStore.load(store.path)[original.uid].fingerprint == revised.fingerprint
    assert store.history[-1]["updates"]["parameters.rf.pi2_amplitude"] == 0.22
    with pytest.raises(TypeError):
        revised.parameters.extensions["lab"]["clock_detuning_hz"] = 700

    for changes, message in (
        ({"rf.pi2_amplitdue": 0.3}, "Unknown calibration parameter path"),
        ({"undeclared.frequency": 2e6}, "Parameter namespace does not exist"),
        ({"extensions.lab.clock_detuning_hz.value": 700}, "Parameter namespace"),
    ):
        with pytest.raises(ValueError, match=message):
            store.update(original.uid, changes)
        assert store[original.uid] is revised
    with pytest.raises(ValueError, match="Stale calibration proposal"):
        store.apply(stale, persist=False)
    assert len(store.history) == 1


def test_editable_parameters_validate_assignments_and_isolate_experiment_snapshots():
    q = role_qubit()
    original = q.to_dict()
    exp = Experiment("before_edit", [q])
    pulse_operation = QubitOperations().x90(q)
    rf = q.parameters.rf
    q.parameters.rf.pi2_amplitude = 0.22
    q.parameters.rf.pi2_duration = 2 * us
    assert rf.pi2_amplitude == 0.22  # Retained references stay live.
    assert rf.pi2_duration == pytest.approx(2e-6)
    assert q.revision == original["revision"] + 2
    assert q.parameters.to_dict() == q.params.to_dict()
    assert q.parameters.rf.to_dict() == q.params.rf.to_dict()
    assert exp.qubits[q.uid].to_dict() == original
    assert pulse_operation.to_node().params["calibration"]["fingerprint"] == (
        exp.qubits[q.uid].fingerprint
    )
    assert Qubit.from_dict(q.to_dict()).fingerprint == q.fingerprint
    assert replace(q).fingerprint == q.fingerprint
    for field, value in (
        ("pi2_amplitude", 1.1),
        ("pi2_duration", -1 * us),
        ("frequency", 1 * us),
        ("pi2_amplitdue", 0.3),
    ):
        before = q.to_dict()
        with pytest.raises((ValueError, TypeError, AttributeError)):
            setattr(rf, field, value)
        assert q.to_dict() == before
    with pytest.raises((AttributeError, TypeError)):
        exp.qubits[q.uid].parameters.rf.pi2_amplitude = 0.8
    # Replacing a declared role also validates, without creating new roles.
    q.parameters.clock = {"frequency": 3e6, "frame": "clock_control"}
    assert q.parameters.clock.frequency == 3e6
    with pytest.raises((ValueError, TypeError, AttributeError)):
        q.parameters.microwave = {"frequency": 4e6}


def test_editable_extensions_track_nested_changes_and_store_stays_read_only(tmp_path):
    q = role_qubit()
    q.parameters.extensions["lab"] = {"detuning_hz": 500.0, "labels": ["demo"]}
    before = q.snapshot()
    extensions = q.parameters.extensions["lab"]
    extensions["detuning_hz"] = 750.0
    extensions["labels"].append("unmeasured")
    assert q.revision == before.revision + 2
    assert before.parameters.extensions["lab"]["detuning_hz"] == 500.0
    assert before.parameters.extensions["lab"]["labels"] == ("demo",)
    assert q.parameters.extensions["lab"]["labels"] == ["demo", "unmeasured"]
    current = q.to_dict()
    for key, value in (("bad", float("nan")), ("quantity", 1 * us)):
        with pytest.raises((ValueError, TypeError)):
            extensions[key] = value
        assert q.to_dict() == current
    stale = CalibrationProposal(
        q.uid,
        before.revision,
        before.fingerprint,
        {"rf.pi2_amplitude": 0.2},
        {"rf.pi2_amplitude": "1"},
        {},
        "old_fit",
    )
    store = QubitStore([q], path=tmp_path / "qubits.json")
    with pytest.raises(ValueError, match="Stale"):
        store.apply(stale, persist=False)
    stored = store.update(q.uid, {"rf.pi2_amplitude": 0.25}, persist=True)
    loaded = QubitStore.load(store.path)[q.uid]
    for snapshot in (stored, loaded):
        with pytest.raises((AttributeError, TypeError)):
            snapshot.parameters.rf.pi2_amplitude = 0.5
        with pytest.raises(TypeError):
            snapshot.parameters.extensions["lab"]["detuning_hz"] = 1000
    assert loaded.fingerprint == stored.fingerprint
    assert q.parameters.rf.pi2_amplitude == 0.17


def test_explicit_signal_paths_preserve_frames_and_resource_order():
    q = role_qubit()
    lab = lab_for()
    lab.signal_map = SignalMap({q.rf: "rf0", q.clock: "raman0"})
    exp = Experiment("role_paths", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        reset_phase(q.frame)
        with section(schedule="asap"):
            delay(q.clock, 400 * ns)
            shift_phase(q.frame, 0.3)
            QubitOperations().x90(q)
    compiled = lab.compile(exp)
    pulse = next(e for e in compiled.points[0].logical_events if e.kind == "play")
    assert pulse.params["qubit"] == q.uid
    assert pulse.params["phase"] == pytest.approx(0.3)
    assert pulse.params["frequency"] == 1e6
    assert pulse.start_mu * compiled.setup.tick_seconds == pytest.approx(400e-9)


def test_global_gate_emits_once_using_only_selected_calibration():
    a, b = role_qubit(), role_qubit("q1")
    b = QubitStore([b]).update("q1", {"rf.pi2_amplitude": 0.8})
    before = (a.fingerprint, b.fingerprint)
    gate = QubitOperations().global_x90(a, b, calibration=a, phase=0.2)
    node = gate.to_node()
    assert gate.qubits == ("q0", "q1")
    assert node.params["global_operation"] == {
        "targets": ["q0", "q1"],
        "signals": [a.rf, b.rf],
        "calibration": "q0",
        "drive": "rf",
    }
    assert len(node.children) == 1
    assert node.children[0].params["pulse"].amplitude == 0.17
    assert node.children[0].params["pulse"].phase == 0.2
    assert node.params["calibration"]["fingerprint"] == a.fingerprint
    exp = Experiment("public_field", [a, b])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        add(gate)
    assert len(exp.root.children[0].children[0].children[0].children) == 1
    assert before == (a.fingerprint, b.fingerprint)


def test_new_view_does_not_rewrite_legacy_snapshot(tmp_path):
    q = Qubit("legacy")
    original = q.to_dict()
    fingerprint = q.fingerprint
    assert q.parameters.rf.frequency == q.params.rf.frequency
    assert q.parameters.raman.pi2_duration == q.params.raman.pi2_duration
    assert q.to_dict() == original and q.fingerprint == fingerprint
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({"schema_version": 2, "qubits": [original], "history": []})
    )
    content = path.read_bytes()
    loaded = QubitStore.load(path)
    assert loaded["legacy"].fingerprint == fingerprint
    assert path.read_bytes() == content


def test_descriptor_public_field_rejects_independent_or_incomplete_gate():
    from test_device_descriptor import databases

    from taiyidsl import CompileError, DeviceSetup, LabConfig

    descriptor, device_db, spec_db = databases()
    descriptor["signals"] = {"q0.rf": "nuclear_rf", "q1.rf": "nuclear_rf"}
    descriptor["channels"]["nuclear_rf"]["sharing"] = "global"
    qubits = tuple(
        Qubit(
            f"q{i}",
            signals={"rf": f"q{i}.rf"},
            parameters={
                "rf": {
                    "frequency": 1e6,
                    "pi2_duration": 400 * ns,
                    "pi2_amplitude": amplitude,
                }
            },
        )
        for i, amplitude in enumerate((0.17, 0.8))
    )
    a, b = qubits
    before = tuple(q.to_dict() for q in qubits)
    lab = LabConfig(
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db),
        qubits=qubits,
    )
    ops = QubitOperations()

    def experiment(gate):
        exp = Experiment("public_field_contract", qubits)
        with (
            exp.average(1),
            exp.realtime(),
            exp.triggered_sequence(group="qubit_awg"),
        ):
            add(gate)
        return exp

    inferred = lab.compile(experiment(ops.x90(a)))
    inferred_play = next(
        e for e in inferred.points[0].logical_events if e.kind == "play"
    )
    assert inferred_play.params["global_operation"]["targets"] == ["q0", "q1"]
    with pytest.raises(CompileError, match="every configured public-field"):
        lab.compile(experiment(ops.global_x90(a, calibration=a)))
    compiled = lab.compile(experiment(ops.global_x90(a, b, calibration=a)))
    logical = [e for e in compiled.points[0].logical_events if e.kind == "play"]
    assert len(logical) == 1
    assert logical[0].params["amplitude"] == 0.17
    program = next(
        e for e in compiled.points[0].physical_events if e.kind == "spectrum_program"
    )
    assert (
        sum(len(c["program"]["pulses"]) for c in program.params["cards"].values()) == 1
    )
    assert before == tuple(q.to_dict() for q in qubits)
