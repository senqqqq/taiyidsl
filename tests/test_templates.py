import numpy as np
import pytest

from taiyidsl.config.models import Qubit
from taiyidsl.experiments._single_qubit_clifford import (
    clifford_group,
    rb_sequences,
    sequence_rotation,
)
from taiyidsl.experiments.templates import rabi, ramsey, randomized_benchmarking


def test_clifford_group_and_seeded_recovery():
    group = clifford_group()
    assert len(group) == 24
    assert len({tuple(matrix.flat) for matrix, _ in group}) == 24
    a = rb_sequences([0, 1, 5, 20], sequence_count=4, seed=42)
    b = rb_sequences([0, 1, 5, 20], sequence_count=4, seed=42)
    assert a == b
    for sequence in a:
        assert len(sequence["clifford_indices"]) == sequence["length"]
        np.testing.assert_array_equal(
            sequence_rotation(sequence["gates"]), np.eye(3, dtype=int)
        )


def test_rb_randomness_is_stable_when_adding_lengths():
    a = rb_sequences([3], sequence_count=2, seed=7)
    b = rb_sequences([1, 3, 5], sequence_count=2, seed=7)
    assert a == [row for row in b if row["length"] == 3]


def test_templates_preserve_condition_sequences_across_average_modes():
    q = Qubit("q0")
    a = randomized_benchmarking(
        q, [1, 3], sequence_count=2, shots=2, seed=8, inter_gate_delay=2e-6
    )
    b = randomized_benchmarking(
        q,
        [1, 3],
        sequence_count=2,
        shots=2,
        seed=8,
        inter_gate_delay=2e-6,
        mode="sequential",
    )
    assert a.metadata["rb_sequences"] == b.metadata["rb_sequences"]
    assert a.metadata["inter_gate_delay"] == 2e-6


def test_rabi_and_ramsey_build_from_qubit_snapshot():
    q = Qubit("q0")
    a = rabi(q, [0, 5e-6, 10e-6], shots=2)
    b = ramsey(q, [0, 5e-6, 10e-6], shots=2, detuning=-25e3)
    assert a.qubits["q0"].params == q.params
    assert b.metadata["detuning"] == -25e3


@pytest.mark.parametrize("lengths", [[], [-1], [1.5], [True], [1, 1]])
def test_invalid_rb_lengths(lengths):
    with pytest.raises(ValueError):
        rb_sequences(lengths, sequence_count=2, seed=0)


def test_rb_requires_explicit_gap():
    with pytest.raises(TypeError, match="inter_gate_delay"):
        randomized_benchmarking(Qubit("q0"), [1, 2])


def test_template_compile_synthetic_calibration_rebuild(tmp_path):
    from taiyidsl.compiler import compile_experiment
    from taiyidsl.config.models import QubitStore
    from taiyidsl.lab import reference_setup
    from taiyidsl.results import synthesize
    from taiyidsl.results.analysis import analyze_rabi, analyze_ramsey, analyze_rb

    q = Qubit("q0")
    store = QubitStore([q], path=tmp_path / "qubits.json")
    store.save()
    setup, mapping = reference_setup([q])
    exp = rabi(q, [i * 1.5e-6 for i in range(49)], shots=400)
    compiled = compile_experiment(exp, setup, mapping)
    results = synthesize(compiled, seed=10, model_parameters={"pi_duration": 12e-6})
    proposal = analyze_rabi(results, experiment=exp)
    updated = store.apply(proposal)
    assert updated.params.raman.pi_duration == pytest.approx(12e-6, rel=0.015)
    assert exp.qubits["q0"].params.raman.pi_duration == 10e-6
    assert compiled.qubits["q0"].revision == 0
    assert QubitStore.load(tmp_path / "qubits.json")["q0"].revision == 1

    ramsey_exp = ramsey(
        updated, [i * 3e-6 for i in range(41)], shots=500, detuning=45e3
    )
    ramsey_compiled = compile_experiment(
        ramsey_exp, setup, mapping, allow_quantization=True
    )
    assert ramsey_compiled.qubits["q0"].revision == 1
    ramsey_data = synthesize(
        ramsey_compiled, seed=11, model_parameters={"t2star": 90e-6}
    )
    ramsey_fit = analyze_ramsey(ramsey_data)
    assert ramsey_fit.parameters["fringe_frequency_abs"] == pytest.approx(
        45e3, rel=0.02
    )
    assert ramsey_fit.parameters["t2star"] == pytest.approx(90e-6, rel=0.25)

    rb_exp = randomized_benchmarking(
        updated,
        [0, 1, 2, 4, 8, 16, 32],
        sequence_count=3,
        shots=300,
        seed=99,
        inter_gate_delay=2e-6,
    )
    rb_compiled = compile_experiment(rb_exp, setup, mapping, allow_quantization=True)
    rb_data = synthesize(rb_compiled, seed=12, model_parameters={"p": 0.96})
    rb_fit = analyze_rb(rb_data)
    assert rb_fit.parameters["p"] == pytest.approx(0.96, abs=0.015)
    assert len(rb_compiled.points) == 21


def test_real_compiler_averaging_has_same_observations():
    from taiyidsl.compiler import compile_experiment
    from taiyidsl.lab import reference_setup
    from taiyidsl.results import synthesize

    q = Qubit("q0")
    setup, mapping = reference_setup([q])
    cyclic = compile_experiment(rabi(q, [0, 5e-6, 10e-6], shots=4), setup, mapping)
    sequential = compile_experiment(
        rabi(q, [0, 5e-6, 10e-6], shots=4, mode="sequential"), setup, mapping
    )
    a, b = synthesize(cyclic, seed=5), synthesize(sequential, seed=5)
    np.testing.assert_array_equal(a.data["population"], b.data["population"])
    assert a.execution_order != b.execution_order


def test_readout_duration_comes_from_qubit_snapshot():
    from taiyidsl.compiler import compile_experiment
    from taiyidsl.config.models import QubitStore
    from taiyidsl.lab import reference_setup

    store = QubitStore([Qubit("q0")])
    q = store.update("q0", {"readout.duration": 25e-6})
    setup, mapping = reference_setup([q])
    compiled = compile_experiment(rabi(q, [0], shots=1), setup, mapping)
    measurement = next(
        e for e in compiled.points[0].logical_events if e.kind == "measure"
    )
    assert measurement.duration_mu == 25000
