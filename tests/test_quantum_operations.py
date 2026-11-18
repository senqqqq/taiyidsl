"""Operation factories bind targets explicitly and append each invocation once."""

import inspect
from pathlib import Path

import pytest

from taiyidsl import (
    Experiment,
    Qubit,
    Section,
    Square,
    SweepParameter,
    add,
    delay,
    echo_body,
    play,
    qubit_operation,
    rabi_body,
    ramsey_body,
    rb_body,
    reference_lab,
    section,
    us,
)
from taiyidsl.dsl import _active
from taiyidsl.templates import rb_sequences


@qubit_operation
def x90(q, *, duration=4 * us):
    """An illustrative pulse operation, without claiming calibrated rotations."""
    play(q.rf, Square(duration, amplitude=0.2))


def test_operation_detached_active_and_nested_calls_append_exactly_once():
    q = Qubit("q")

    @qubit_operation
    def pair(q, gap=2 * us):
        x90(q)
        delay(q.rf, gap)
        x90(q)

    saved = pair(q)
    assert isinstance(saved, Section) and saved.qubits == ("q",)
    assert len(saved.to_node().children) == 3
    exp = Experiment("composed", [q])
    with exp.average(1), exp.realtime():
        active = pair(q)
        add(saved)
    body = exp.root.children[0].children[0].children
    assert len(body) == 2
    assert active is not saved
    assert len(body[0].children) == 3
    compiled = reference_lab([q]).compile(exp)
    plays = [
        event for event in compiled.points[0].logical_events if event.kind == "play"
    ]
    assert len(plays) == 4
    assert compiled.points[0].duration_mu == 20000
    assert _active.get() is None


def test_method_keyword_targets_and_parameters_have_stable_metadata():
    q0, q1 = Qubit("q0"), Qubit("q1")
    axis = SweepParameter("duration", [4, 8], "us")

    class Operations:
        @qubit_operation
        def pair(self, first, second, *, duration=4 * us):
            play(first.rf, Square(duration))
            play(second.rf, Square(duration))

    operations = Operations()
    result = operations.pair(second=q1, first=q0, duration=axis)
    assert result.qubits == ("q0", "q1")
    node = result.to_node()
    assert node.params["operation_qubits"] == ["q0", "q1"]
    assert result.parameters["first"] == {"qubit": "q0", "revision": 0}
    assert "self" not in result.parameters
    assert result.parameters["duration"]["parameter"] == "duration"
    mutated = result.parameters
    mutated["first"]["qubit"] = "other"
    assert result.parameters["first"]["qubit"] == "q0"
    assert node.source.startswith(str(Path(__file__)))
    assert inspect.signature(x90) == inspect.signature(x90.__wrapped__)
    assert x90.__doc__.startswith("An illustrative")


def test_qubit_snapshot_mismatch_and_absent_target_reject_without_appending():
    q = Qubit("q")
    stored = x90(q)
    for replacement, message in (
        (Qubit("other"), "absent"),
        (Qubit("q", revision=1), "different parameter"),
    ):
        exp = Experiment("wrong_binding", [replacement])
        with exp.average(1), exp.realtime():
            with pytest.raises(ValueError, match=message):
                add(stored)
            with pytest.raises(ValueError, match=message):
                x90(q)
        assert not exp.root.children[0].children[0].children


def test_invalid_bodies_and_targets_fail_without_leaking_context():
    q = Qubit("q")

    async def asynchronous(q):
        pass

    def generator(q):
        yield None

    def native(q):
        raise AssertionError("must not execute")

    native.artiq_embedded = object()
    for function in (asynchronous, generator, native):
        with pytest.raises(TypeError, match="synchronous DSL"):
            qubit_operation(function)

    def variable(*targets):
        pass

    with pytest.raises(TypeError, match="explicit named"):
        qubit_operation(variable)
    with pytest.raises(TypeError, match="Qubit"):
        x90([q])
    with pytest.raises(TypeError, match="Qubit"):
        x90("q")

    @qubit_operation
    def wrong(q):
        play(q.rf, Square(4 * us))
        return 1

    exp = Experiment("rollback", [q])
    with exp.average(1), exp.realtime():
        with pytest.raises(TypeError, match="return None"):
            wrong(q)
        assert _active.get() is exp
        x90(q)
    assert len(exp.root.children[0].children[0].children) == 1
    assert _active.get() is None


def test_nested_binding_failure_and_scope_rollback_leave_no_target_residue():
    q = Qubit("q")
    with section("outer") as outer:
        with pytest.raises(RuntimeError):
            from taiyidsl import serial

            with serial():
                x90(q)
                raise RuntimeError("discard")
        x90(Qubit("q", revision=1))
    assert outer.qubits == ("q",)
    exp = Experiment("correct", [Qubit("q", revision=1)])
    with exp.average(1), exp.realtime():
        add(outer)
    assert len(outer.to_node().children) == 1


def test_public_gate_bodies_keep_explicit_pulses_delays_and_terminal_frame():
    q = Qubit("q")
    pi2 = Square(4 * us, amplitude=0.2, frequency=30_000, phase=0.3)
    pi = Square(8 * us, amplitude=0.2, frequency=30_000, phase=1.2)
    single = rabi_body(q, pi2)
    assert single.to_node().children[0].params["pulse"] == pi2
    ramsey = ramsey_body(q, pi2, 2 * us, virtual_z=0.4, final_phase=0.8)
    nodes = ramsey.to_node().children
    assert [node.kind for node in nodes] == ["play", "delay", "rz", "play"]
    assert nodes[1].params["duration"] == 2 * us
    assert nodes[3].params["pulse"].phase == 0.8
    assert pi2.phase == 0.3
    echo = echo_body(q, pi2, pi, 3 * us)
    nodes = echo.to_node().children
    assert [node.kind for node in nodes] == ["play", "delay", "play", "delay", "play"]
    assert nodes[2].params["pulse"] == pi
    exp = Experiment("frame_inheritance", [q])
    with exp.average(1), exp.realtime():
        add(ramsey)
        rabi_body(q, pi2)
    compiled = reference_lab([q]).compile(exp)
    assert compiled.virtual_z_frames()["q"] == pytest.approx(0.4)
    plays = [e for e in compiled.points[0].logical_events if e.kind == "play"]
    assert plays[-1].params["phase"] == pytest.approx(-0.1)


def test_rb_body_reuses_fixed_sequence_and_pulse_shapes_without_random_redraw():
    q = Qubit("q")
    pi2 = Square(4 * us, amplitude=0.2, frequency=30_000)
    pi = Square(8 * us, amplitude=0.4, frequency=30_000)
    descriptors = rb_sequences([0, 3], sequence_count=2, seed=42)
    item = descriptors[-1]
    first = rb_body(q, item, pi2, pi, inter_gate_delay=2 * us)
    second = rb_body(
        q=q, sequence=item, pi2_pulse=pi2, pi_pulse=pi, inter_gate_delay=2 * us
    )
    first_nodes, second_nodes = first.to_node().children, second.to_node().children
    assert [(n.kind, n.params) for n in first_nodes] == [
        (n.kind, n.params) for n in second_nodes
    ]
    pulses = [n.params["pulse"] for n in first_nodes if n.kind == "play"]
    assert len(pulses) == len(item["gates"])
    for gate, pulse in zip(item["gates"], pulses, strict=True):
        assert pulse.duration == (8 * us if gate.endswith("180") else 4 * us)
    assert first.parameters["sequence"] == item
    item["gates"].clear()
    assert first.parameters["sequence"]["gates"]
    assert rb_body(q, descriptors[0], pi2, pi).to_node().children == []
    with pytest.raises(ValueError, match="unsupported gate"):
        rb_body(q, {"gates": ["unknown"]}, pi2, pi)
