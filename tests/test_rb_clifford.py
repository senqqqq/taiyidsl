"""Independent ideal-matrix checks for exact Clifford sampling and recovery."""

from collections import Counter

import numpy as np
import pytest

from taiyidsl.experiments._single_qubit_clifford import rb_sequences
from taiyidsl.experiments.rb_sequences import (
    RBGate,
    _apply,
    _compose,
    _gate_tableau,
    _identity,
    _inverse,
    _parts,
    clifford_catalog,
    generate_rb_sequences,
    sequence_tableau,
)

X = np.array([[0, 1], [1, 0]], dtype=complex)
Y = np.array([[0, -1j], [1j, 0]], dtype=complex)
Z = np.diag([1, -1]).astype(complex)
IDENTITY = np.eye(2, dtype=complex)


def primitive_matrix(gate, n):
    if gate.name == "cz":
        return np.diag([1, 1, 1, -1]).astype(complex)
    axis = X if gate.name.startswith("x") else Y
    angle = (
        -np.pi / 2 if "m90" in gate.name else np.pi / 2 if "90" in gate.name else np.pi
    )
    matrix = np.cos(angle / 2) * IDENTITY - 1j * np.sin(angle / 2) * axis
    if n == 1:
        return matrix
    return (
        np.kron(matrix, IDENTITY) if gate.targets == (0,) else np.kron(IDENTITY, matrix)
    )


def sequence_matrix(gates, n):
    value = np.eye(2**n, dtype=complex)
    for gate in gates:
        value = primitive_matrix(gate, n) @ value
    return value


def pauli_matrix(code, n):
    x, z, phase = _parts(code, n)
    value = np.array([[1]], dtype=complex)
    for q in range(n):
        value = np.kron(
            value, (X if x & (1 << q) else IDENTITY) @ (Z if z & (1 << q) else IDENTITY)
        )
    return 1j**phase * value


def test_full_two_qubit_group_size_closure_inverse_and_entangling_cost():
    group = clifford_catalog(2)
    lookup = {entry.tableau for entry in group}
    assert len(group) == len(lookup) == 11520
    counts = Counter(sum(g.name == "cz" for g in entry.gates) for entry in group)
    assert counts == {0: 576, 1: 5184, 2: 5184, 3: 576}
    generators = [RBGate(name, (q,)) for q in (0, 1) for name in ("x90", "y90")] + [
        RBGate("cz", (0, 1))
    ]
    actions = [
        tuple(_apply(_gate_tableau(gate, 2), p, 2) for p in range(64))
        for gate in generators
    ]
    for entry in group:
        inverse = _inverse(entry.tableau, 2)
        assert inverse in lookup
        assert _compose(inverse, entry.tableau, 2) == _identity(2)
        assert all(
            tuple(action[p] for p in entry.tableau) in lookup for action in actions
        )


def test_every_two_qubit_decomposition_matches_independent_unitary_conjugation():
    generators = [
        np.kron(X, IDENTITY),
        np.kron(Z, IDENTITY),
        np.kron(IDENTITY, X),
        np.kron(IDENTITY, Z),
    ]
    for entry in clifford_catalog(2):
        matrix = sequence_matrix(entry.gates, 2)
        for base, image in zip(generators, entry.tableau):
            np.testing.assert_allclose(
                matrix @ base @ matrix.conj().T, pauli_matrix(image, 2), atol=1e-12
            )


def test_single_qubit_catalog_and_seeds_keep_existing_contract():
    old = rb_sequences([0, 1, 5], sequence_count=3, seed=71)
    new = generate_rb_sequences([0, 1, 5], sequence_count=3, seed=71)
    assert len(clifford_catalog(1)) == 24
    for before, after in zip(old, new):
        assert after.clifford_indices == tuple(before["clifford_indices"])
        assert after.recovery_index == before["recovery_index"]
        assert tuple(g.name for g in after.gates) == tuple(before["gates"])


def test_full_catalog_uniform_draws_paired_recovery_and_length_zero():
    descriptors = generate_rb_sequences(
        [0, 1, 3, 20], sequence_count=4, seed=103, num_qubits=2, interleaved=True
    )
    for ref, interleaved in zip(descriptors[::2], descriptors[1::2]):
        assert ref.clifford_indices == interleaved.clifford_indices
        rng = np.random.default_rng(
            np.random.SeedSequence([103, ref.length, ref.sequence])
        )
        assert ref.clifford_indices == tuple(rng.integers(0, 11520, size=ref.length))
        for sequence in (ref, interleaved):
            assert sequence_tableau(sequence.gates, num_qubits=2) == _identity(2)
            matrix = sequence_matrix(sequence.gates, 2)
            np.testing.assert_allclose(matrix, matrix[0, 0] * np.eye(4), atol=1e-12)
        assert interleaved.to_dict()["interleaved_cz_count"] == ref.length
        if ref.length == 0:
            assert ref.gates == interleaved.gates == ()
    reordered = generate_rb_sequences(
        [20, 3], sequence_count=4, seed=103, num_qubits=2, interleaved=True
    )
    assert {(d.length, d.sequence, d.variant): d for d in reordered} == {
        (d.length, d.sequence, d.variant): d for d in descriptors if d.length in (20, 3)
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"num_qubits": 3},
        {"num_qubits": 1, "interleaved": True},
        {"seed": -1},
        {"sequence_count": 0},
    ],
)
def test_rb_rejects_unsupported_or_ambiguous_inputs(kwargs):
    options = {"sequence_count": 1, "seed": 0, **kwargs}
    with pytest.raises(ValueError):
        generate_rb_sequences([1], **options)
