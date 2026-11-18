"""Sections preserve structure, binding and phase without sharing mutable nodes."""

from pathlib import Path

import pytest

from taiyidsl import (
    CompileError,
    Experiment,
    FlatTopGaussian,
    NativeKernelPackage,
    PolynomialPhase,
    Qubit,
    Section,
    Square,
    SweepParameter,
    add,
    average,
    barrier,
    delay,
    parallel,
    play,
    realtime,
    reference_lab,
    repeat,
    rz,
    section,
    serial,
    stage,
    sweep,
    triggered_sequence,
    trigplay,
    us,
)
from taiyidsl.dsl import _active, json_value


def _body(exp):
    return exp.root.children[0].children[0].children


def _events(compiled):
    return [
        (
            e.kind,
            e.signal,
            e.start_mu,
            e.duration_mu,
            {key: value for key, value in e.params.items() if key != "operation_path"},
        )
        for e in compiled.points[0].logical_events
    ]


def test_detached_section_insertion_copies_structure_and_accessors():
    q = Qubit("q")
    with section("pair") as pair:
        play(q.rf, Square(4 * us))
        delay(q.rf, 2 * us)
    assert isinstance(pair, Section)
    assert pair.name == "pair" and pair.schedule == "serial"
    assert _active.get() is None
    copied = pair.to_node()
    copied.children.clear()
    assert len(pair.to_node().children) == 2
    with pytest.raises(AttributeError):
        pair.name = "changed"

    exp = Experiment("copies", [q])
    with exp.average(1), exp.realtime():
        exp.add(pair)
        add(pair)
    first, second = _body(exp)
    assert first is not second
    first.children[0].params["pulse"] = Square(8 * us)
    assert second.children[0].params["pulse"].duration == 4 * us
    assert pair.to_node().children[0].params["pulse"].duration == 4 * us

    other = Experiment("other", [q])
    with other.average(1), other.realtime():
        add(pair)
    assert _body(other)[0] is not second


def test_active_section_attaches_once_and_routes_exp_scopes_to_collector():
    q = Qubit("q")
    exp = Experiment("mixed_style", [q])
    with exp.average(1), exp.realtime():
        with section("outer") as outer:
            with exp.serial():
                play(q.rf, Square(4 * us))
            with exp.parallel():
                with exp.stage("one branch"):
                    delay(q.rf, 2 * us)
                with section("inner"):
                    with exp.repeat(2):
                        delay(q.raman, 1 * us)
        play(q.rf, Square(4 * us))
    assert len(_body(exp)) == 2
    assert len(outer.to_node().children) == 2
    compiled = reference_lab([q]).compile(exp)
    assert compiled.points[0].duration_mu == 10000
    assert outer.to_node().source.startswith(str(Path(__file__)))


def test_section_grouping_preserves_serial_and_parallel_events_and_frames():
    q0, q1 = Qubit("q0"), Qubit("q1")

    def emit():
        with parallel():
            with serial():
                play(q0.rf, Square(4 * us))
                delay(q0.rf, 2 * us)
            play(q1.rf, Square(8 * us))
        rz(q0, 0.4)
        play(q0.rf, Square(4 * us))

    plain, wrapped = (Experiment(name, [q0, q1]) for name in ("plain", "wrapped"))
    with plain.average(1), plain.realtime():
        emit()
        play(q0.rf, Square(4 * us))
    with section("gates") as gates:
        emit()
    with wrapped.average(1), wrapped.realtime():
        add(gates)
        play(q0.rf, Square(4 * us))
    lab = reference_lab([q0, q1])
    first, second = lab.compile(plain), lab.compile(wrapped)
    assert _events(first) == _events(second)
    assert second.virtual_z_frames() == first.virtual_z_frames()


def test_pending_failed_and_foreign_contexts_never_write_wrong_parent():
    q = Qubit("q")
    exp, foreign = Experiment("parent", [q]), Experiment("foreign", [q])
    with exp.average(1), exp.realtime():
        with pytest.raises(RuntimeError, match="failed"):
            with section("failed") as failed:
                play(q.rf, Square(4 * us))
                raise RuntimeError("failed")
        assert _active.get() is exp
        assert not failed.finalized
        with section("valid") as pending:
            with pytest.raises(ValueError, match="not completed"):
                add(pending)
            with pytest.raises(ValueError, match="different experiments"):
                with foreign.serial():
                    pass
            with pytest.raises(ValueError, match="active block"):
                foreign.add(failed)
            with pytest.raises(RuntimeError):
                with exp.serial():
                    play(q.rf, Square(4 * us))
                    raise RuntimeError("rollback inner scope")
            play(q.rf, Square(8 * us))
    assert len(_body(exp)) == 1
    assert len(_body(exp)[0].children) == 1
    assert not foreign.root.children
    assert _active.get() is None


def test_detached_sections_use_free_scopes_and_keep_sweep_contracts():
    q = Qubit("q")
    axis = SweepParameter("amplitude", [0.2, 0.3], "")
    with section("swept") as segment:
        with stage("pulse"):
            with repeat(2):
                play(q.rf, Square(4 * us, amplitude=axis))
    for values, valid in (([0.2, 0.3], True), ([0.4, 0.5], False)):
        exp = Experiment("bind", [q])
        declared = SweepParameter("amplitude", values, "")
        with exp.average(1), exp.sweep(declared), exp.realtime():
            add(segment)
        if valid:
            compiled = reference_lab([q]).compile(exp)
            assert len(compiled.points) == 2
        else:
            with pytest.raises(CompileError, match="conflicting parameter"):
                reference_lab([q]).compile(exp)


def test_section_rejects_host_epoch_and_native_boundaries(tmp_path):
    q = Qubit("q")
    exp = Experiment("parent", [q])
    axis = SweepParameter("value", [1], "")
    source = tmp_path / "native_stage.py"
    source.write_text("class Stage: pass\n", encoding="utf-8")
    native = NativeKernelPackage(
        module="native_stage",
        class_name="Stage",
        sources={"native_stage": source},
        resources=("ttl0",),
    ).kernel("prepare", timing="prefix")
    with section("restricted"):
        for factory in (
            lambda: average(1),
            lambda: exp.average(1),
            lambda: realtime(),
            lambda: exp.realtime(),
            lambda: triggered_sequence(group="g"),
            lambda: exp.triggered_sequence(group="g"),
        ):
            with pytest.raises(ValueError, match="Section"):
                factory()
        for factory in (lambda: sweep(axis), lambda: exp.sweep(axis)):
            with pytest.raises(ValueError, match="Section"):
                with factory():
                    pass
        with pytest.raises(ValueError, match="Native kernel calls"):
            native()
    assert not exp.root.children
    with exp.average(1):
        with pytest.raises(ValueError, match="inside realtime"):
            with section("misplaced"):
                pass


def test_section_delay_barrier_and_asap_keep_explicit_nodes():
    with pytest.raises(ValueError, match="Section"):
        barrier()
    with section("scheduled", schedule="asap") as planned:
        delay(2 * us)
        barrier()
    node = planned.to_node()
    assert node.kind == "serial" and node.params["schedule"] == "asap"
    assert [child.kind for child in node.children] == ["section_delay", "barrier"]
    with pytest.raises(ValueError, match="schedule"):
        with section(schedule="tetris"):
            pass
    with pytest.raises(TypeError, match="Build a Section"):
        Section()


def test_sweep_chunk_and_average_scope_arguments_are_recorded_and_validated():
    q = Qubit("q")
    axis = SweepParameter("value", [1, 2, 3], "")
    exp = Experiment("chunked", [q])
    with exp.average(2, scope="chunk"), sweep(axis, chunk_size=2), realtime():
        play(q.rf, Square(4 * us))
    avg = exp.root.children[0]
    assert avg.params["scope"] == "chunk"
    assert avg.children[0].params["chunk_size"] == 2
    with pytest.raises(ValueError, match="scope"):
        Experiment("bad", [q]).average(1, scope="point")
    for options in (
        {"chunks": 0},
        {"chunk_size": True},
        {"chunks": 2, "chunk_size": 2},
    ):
        with pytest.raises(ValueError):
            with Experiment("bad", [q]).sweep(axis, **options):
                pass


def test_phase_modulation_mode_serialization_and_per_call_override():
    q = Qubit("q")
    square = Square(4 * us, phase_modulation=PolynomialPhase((0, 1)))
    gaussian = FlatTopGaussian(4 * us, 1 * us, phase_modulation_mode="carry")
    assert "phase_modulation_mode" not in json_value(square)
    assert json_value(gaussian)["phase_modulation_mode"] == "carry"
    with section("modes") as modes:
        play(q.rf, square, phase_modulation_mode="carry")
        trigplay(q.rf, gaussian, phase_modulation_mode="local")
    first, second = modes.to_node().children
    assert first.params["pulse"].phase_modulation_mode == "carry"
    assert second.params["pulse"].phase_modulation_mode == "local"
    assert square.phase_modulation_mode == "local"
    assert gaussian.phase_modulation_mode == "carry"
    assert "phase_modulation_mode" not in json_value(second.params["pulse"])
    with pytest.raises(ValueError, match="phase_modulation_mode"):
        Square(4 * us, phase_modulation_mode="invalid")
