"""Function-style builders retain the explicit DSL's events and isolation."""

import inspect
from pathlib import Path

import pytest
from test_spectrum_backend import lab as spectrum_lab
from trace_fingerprint import fingerprint

from taiyidsl import (
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    compile_experiment,
    delay,
    parallel,
    play,
    reference_setup,
    serial,
    trigplay,
    us,
)
from taiyidsl.authoring import (
    average,
    case,
    experiment,
    realtime,
    repeat,
    stage,
    sweep,
    triggered_sequence,
)
from taiyidsl.dsl import _active, json_value
from taiyidsl.spectrum_waveforms import reconstruct_window


def _structure(node):
    value = json_value(node)

    def strip(item):
        item.pop("source", None)
        for child in item["children"]:
            strip(child)

    strip(value)
    return value


def test_factory_matches_explicit_ir_physical_events_and_execution():
    q = Qubit("q")
    amplitude = SweepParameter("amplitude", [0.1, 0.3], "dimensionless")

    def gate_train():
        with repeat(2):
            play(q.rf, Square(4 * us, amplitude=amplitude))
            delay(q.rf, 2 * us)
        with case(amplitude, 0.3):
            with stage("tail", exact_duration=4 * us):
                play(q.rf, Square(4 * us))

    @experiment(qubits=[q], name="comparison")
    def build(*, shots=2):
        with average(shots, mode="sequential"):
            with sweep(amplitude) as axis:
                assert axis is amplitude
                with realtime():
                    gate_train()

    explicit = Experiment("comparison", [q])
    with explicit.average(2, mode="sequential"):
        with explicit.sweep(amplitude):
            with explicit.realtime():
                gate_train()
    decorated = build()
    assert _structure(decorated.root) == _structure(explicit.root)
    setup, mapping = reference_setup([q])
    assert fingerprint(compile_experiment(decorated, setup, mapping)) == fingerprint(
        compile_experiment(explicit, setup, mapping)
    )
    assert decorated.root.children[0].source.startswith(str(Path(__file__)))
    assert "authoring.py" not in decorated.root.children[0].source


def test_each_call_is_fresh_and_captures_current_parameter_values():
    q = Qubit("q")
    parameters = {"preparation": {"duration": 1 * us}}
    metadata = {"notes": ["first"]}

    @experiment(qubits=[q], parameters=parameters, metadata=metadata)
    def sample(duration=4 * us, *, shots=1):
        """A reusable builder."""
        with average(shots):
            with realtime():
                play(q.rf, Square(duration))

    first = sample()
    parameters["preparation"]["duration"] = 2 * us
    metadata["notes"].append("second")
    second = sample(8 * us, shots=3)
    assert first is not second
    assert first.root is not second.root
    assert first.qubits["q"] is not second.qubits["q"]
    assert first.parameters["preparation"]["duration"] == 1 * us
    assert second.parameters["preparation"]["duration"] == 2 * us
    assert first.metadata["notes"] == ["first"]
    assert second.metadata["notes"] == ["first", "second"]
    assert first.root.children[0].params["shots"] == 1
    assert second.root.children[0].params["shots"] == 3
    assert sample.__name__ == first.name == "sample"
    assert sample.__doc__ == "A reusable builder."
    assert inspect.signature(sample) == inspect.signature(sample.__wrapped__)
    assert _active.get() is None


def test_builder_exception_and_bad_arguments_restore_context():
    q = Qubit("q")

    @experiment(qubits=[q])
    def build(*, fail):
        with average(1):
            with realtime():
                play(q.rf, Square(4 * us))
                if fail:
                    raise RuntimeError("builder failed")

    with pytest.raises(RuntimeError, match="builder failed"):
        build(fail=True)
    assert _active.get() is None
    with pytest.raises(TypeError):
        build()
    assert _active.get() is None
    assert len(build(fail=False).root.children) == 1
    with pytest.raises(RuntimeError, match="active Experiment"):
        realtime()


def test_nested_factory_is_rejected_without_losing_outer_context():
    q = Qubit("q")

    @experiment(qubits=[q])
    def child():
        with average(1):
            with realtime():
                play(q.rf, Square(4 * us))

    outer = Experiment("outer", [q])
    with outer.average(1):
        with outer.realtime():
            with pytest.raises(ValueError, match="active builder"):
                child()
            assert _active.get() is outer
            play(q.rf, Square(8 * us))
    assert len(outer.root.children[0].children[0].children) == 1
    assert _active.get() is None


def test_invalid_deferred_functions_and_return_values_are_not_silently_ignored():
    async def asynchronous():
        pass

    def generator():
        yield None

    async def async_generator():
        yield None

    def kernel():
        raise AssertionError("native kernel must not run")

    kernel.artiq_embedded = object()
    for function in (asynchronous, generator, async_generator, kernel):
        with pytest.raises(TypeError, match="synchronous DSL builder"):
            experiment(qubits=[])(function)

    @experiment(qubits=[])
    def returns_value():
        return "not an experiment"

    with pytest.raises(TypeError, match="must return None"):
        returns_value()
    assert _active.get() is None


def test_free_wrappers_keep_existing_scope_validation_and_mixed_style():
    q = Qubit("q")
    explicit = Experiment("mixed", [q])
    with explicit.average(1):
        with realtime():
            with parallel():
                with serial():
                    with stage("explicit operation", exact_duration=4 * us):
                        play(q.rf, Square(4 * us))
                delay(q.raman, 8 * us)
            with pytest.raises(ValueError, match="cannot be nested"):
                with realtime():
                    pass
    setup, mapping = reference_setup([q])
    compiled = compile_experiment(explicit, setup, mapping)
    assert compiled.points[0].duration_mu == 8000


def test_hybrid_factory_preserves_trigger_timing_and_packed_waveforms():
    q = Qubit("q")
    pulse = Square(4 * us, frequency=30_000, amplitude=0.2)

    def gates():
        trigplay(q.rf, pulse)
        delay(2 * us)
        trigplay(q.rf, pulse)

    @experiment(qubits=[q])
    def build():
        with average(1):
            with realtime():
                play("camera", Square(1 * us, amplitude=1, frequency=0))
                with triggered_sequence(group="g"):
                    gates()

    explicit = Experiment("build", [q])
    with explicit.average(1):
        with explicit.realtime():
            play("camera", Square(1 * us, amplitude=1, frequency=0))
            with explicit.triggered_sequence(group="g"):
                gates()
    decorated = build()
    assert _structure(decorated.root) == _structure(explicit.root)
    lab = spectrum_lab()
    plans = [lab.compile(exp) for exp in (explicit, decorated)]
    timing = [
        [
            (e.kind, e.signal, e.start_mu, e.duration_mu)
            for e in p.points[0].physical_events
        ]
        for p in plans
    ]
    assert timing[0] == timing[1]
    programs = [
        next(e for e in p.points[0].physical_events if e.kind == "spectrum_program")
        for p in plans
    ]
    for card in ("A", "B"):
        first, second = (e.params["cards"][card]["program"] for e in programs)
        assert first["steps"] == second["steps"]
        end = first["physical_duration_samples"]
        assert (
            reconstruct_window(first, 0, end) == reconstruct_window(second, 0, end)
        ).all()
