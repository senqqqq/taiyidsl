"""Exact one/two-qubit Clifford RB circuits, generated once on the host.

Tableaus represent ``U P U†`` for X0, Z0, X1, Z1. Qubit zero is the
leftmost tensor factor. Chronological gates multiply on the left. Global phase
is intentionally absent. The 2q catalogue is complete (11520 elements), not a
random walk over primitive gates. No quantum SDK or hardware is imported.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from ._single_qubit_clifford import clifford_group, rb_sequences

CATALOG_VERSION = "taiyi-clifford-tableau-v1"
_SINGLE_GATES = ("x90", "xm90", "y90", "ym90", "x180", "y180")


@dataclass(frozen=True)
class RBGate:
    name: str
    targets: tuple[int, ...]

    def __post_init__(self):
        object.__setattr__(self, "targets", tuple(self.targets))
        arity = 2 if self.name == "cz" else 1
        if self.name not in (*_SINGLE_GATES, "cz"):
            raise ValueError(f"Unknown RB primitive {self.name!r}")
        if len(self.targets) != arity or len(set(self.targets)) != arity:
            raise ValueError("RB primitive targets have the wrong arity")
        if any(type(q) is not int or q not in (0, 1) for q in self.targets):
            raise ValueError("RB primitive targets must be qubit indices 0 or 1")

    def to_dict(self):
        return {"name": self.name, "targets": list(self.targets)}


def _pack(x, z, phase, n):
    # Pauli = i**phase X**x Z**z, with X factors before Z factors.
    return x | (z << n) | ((phase % 4) << (2 * n))


def _parts(code, n):
    mask = (1 << n) - 1
    return code & mask, (code >> n) & mask, code >> (2 * n)


def _multiply(a, b, n):
    x, z, phase = _parts(a, n)
    xx, zz, pp = _parts(b, n)
    return _pack(x ^ xx, z ^ zz, phase + pp + 2 * (z & xx).bit_count(), n)


def _identity(n):
    return tuple(value for q in range(n) for value in (1 << q, 1 << (n + q)))


def _apply(tableau, code, n):
    x, z, phase = _parts(code, n)
    result = _pack(0, 0, phase, n)
    for q in range(n):
        if x & (1 << q):
            result = _multiply(result, tableau[2 * q], n)
    for q in range(n):
        if z & (1 << q):
            result = _multiply(result, tableau[2 * q + 1], n)
    return result


def _compose(after, before, n):
    return tuple(_apply(after, code, n) for code in before)


def _inverse(tableau, n):
    # Each signed Hermitian Pauli has a unique signed preimage. This uses only
    # integer Pauli products, never nearest-matrix matching or a tolerance.
    preimages = {}
    for x in range(1 << n):
        for z in range(1 << n):
            for sign in (0, 2):
                code = _pack(x, z, (x & z).bit_count() + sign, n)
                preimages[_apply(tableau, code, n)] = code
    return tuple(preimages[code] for code in _identity(n))


def _gate_tableau(gate, n):
    if max(gate.targets) >= n:
        raise ValueError("Gate target lies outside this Clifford register")
    images = list(_identity(n))
    q = gate.targets[0]
    bit = 1 << q
    x, z = _pack(bit, 0, 0, n), _pack(0, bit, 0, n)
    y, minus_y = _pack(bit, bit, 1, n), _pack(bit, bit, 3, n)
    minus_x, minus_z = _pack(bit, 0, 2, n), _pack(0, bit, 2, n)
    local = {
        "x90": (x, minus_y),
        "xm90": (x, y),
        "y90": (minus_z, x),
        "ym90": (z, minus_x),
        "x180": (x, minus_z),
        "y180": (minus_x, minus_z),
    }
    if gate.name == "cz":
        other = gate.targets[1]
        images[2 * q] = _pack(bit, 1 << other, 0, n)
        images[2 * other] = _pack(1 << other, bit, 0, n)
    else:
        images[2 * q], images[2 * q + 1] = local[gate.name]
    return tuple(images)


def sequence_tableau(gates, *, num_qubits):
    """Exact chronological conjugation action, including recovery if supplied."""
    _validate_qubit_count(num_qubits)
    result = _identity(num_qubits)
    for gate in gates:
        result = _compose(_gate_tableau(gate, num_qubits), result, num_qubits)
    return result


def _validate_qubit_count(n):
    if type(n) is not int or n not in (1, 2):
        raise ValueError("RB supports exactly one or two qubits")


@dataclass(frozen=True)
class Clifford:
    tableau: tuple[int, ...]
    gates: tuple[RBGate, ...]


@lru_cache(maxsize=2)
def clifford_catalog(num_qubits=2):
    """Return immutable exact Clifford records with deterministic decompositions.

    The 1q order/words preserve ``templates.clifford_group``. The 2q catalogue
    is sorted by tableau; decompositions minimize (#CZ, #single-qubit pulses)
    lexicographically. Generator order breaks ties deterministically. This is a
    chosen compilation cost, not a statement about calibrated gate fidelity.
    """
    _validate_qubit_count(num_qubits)
    if num_qubits == 1:
        return tuple(
            Clifford(sequence_tableau(gates, num_qubits=1), gates)
            for _, word in clifford_group()
            for gates in (tuple(RBGate(name, (0,)) for name in word),)
        )
    generators = tuple(RBGate(name, (q,)) for q in (0, 1) for name in _SINGLE_GATES) + (
        RBGate("cz", (0, 1)),
    )
    # Precompute generator actions on the 64 signed/complex Pauli codes.
    actions = [
        tuple(_apply(_gate_tableau(gate, 2), code, 2) for code in range(64))
        for gate in generators
    ]
    identity = _identity(2)
    costs = {identity: (0, 0)}
    previous = {}
    queue, serial = [(0, 0, 0, identity)], 0
    while queue:
        entangling, local, _, current = heapq.heappop(queue)
        if costs[current] != (entangling, local):
            continue
        for gate, action in zip(generators, actions):
            candidate = tuple(action[code] for code in current)
            cost = (entangling + (gate.name == "cz"), local + (gate.name != "cz"))
            if candidate not in costs or cost < costs[candidate]:
                costs[candidate] = cost
                previous[candidate] = (current, gate)
                serial += 1
                heapq.heappush(queue, (*cost, serial, candidate))
    if len(costs) != 11520:
        raise RuntimeError("Clifford generators did not produce 11520 elements")
    catalogue = []
    for tableau in sorted(costs):
        gates, current = [], tableau
        while current != identity:
            current, gate = previous[current]
            gates.append(gate)
        catalogue.append(Clifford(tableau, tuple(reversed(gates))))
    return tuple(catalogue)


@dataclass(frozen=True)
class RBSequence:
    num_qubits: int
    seed: int
    length: int
    sequence: int
    variant: str
    clifford_indices: tuple[int, ...]
    recovery_index: int
    gates: tuple[RBGate, ...]

    def __post_init__(self):
        _validate_qubit_count(self.num_qubits)
        object.__setattr__(self, "clifford_indices", tuple(self.clifford_indices))
        object.__setattr__(self, "gates", tuple(self.gates))
        for value in (self.seed, self.length, self.sequence):
            if type(value) is not int or value < 0:
                raise ValueError("RB coordinates and seed must be nonnegative integers")
        if self.variant not in {"reference", "interleaved"} or (
            self.variant == "interleaved" and self.num_qubits != 2
        ):
            raise ValueError("Invalid RB variant for this register")
        size = 24 if self.num_qubits == 1 else 11520
        if len(self.clifford_indices) != self.length or any(
            type(i) is not int or not 0 <= i < size
            for i in (*self.clifford_indices, self.recovery_index)
        ):
            raise ValueError("RB Clifford indices do not match the declared catalogue")
        if any(
            not isinstance(gate, RBGate) or max(gate.targets) >= self.num_qubits
            for gate in self.gates
        ):
            raise ValueError("RB primitive acts outside the declared register")

    def to_dict(self):
        return {
            "catalog_version": CATALOG_VERSION,
            "num_qubits": self.num_qubits,
            "seed": self.seed,
            "length": self.length,
            "sequence": self.sequence,
            "variant": self.variant,
            "clifford_indices": list(self.clifford_indices),
            "recovery_index": self.recovery_index,
            "gates": [gate.to_dict() for gate in self.gates],
            "primitive_count": len(self.gates),
            "cz_count": sum(g.name == "cz" for g in self.gates),
            "interleaved_cz_count": self.length if self.variant == "interleaved" else 0,
            "length_definition": "random Cliffords, excluding recovery",
        }


def generate_rb_sequences(
    lengths, *, sequence_count, seed, num_qubits=1, interleaved=False
):
    """Freeze reference circuits and optionally their paired CZ-interleaved circuits.

    Each pair shares exactly the same uniformly sampled Clifford indices. Each
    variant gets its own exact recovery. Random draws depend only on seed,
    length and sequence ID, not chunking, shot number or execution order.
    """
    _validate_qubit_count(num_qubits)
    if type(interleaved) is not bool or (interleaved and num_qubits != 2):
        raise ValueError("CZ interleaved RB requires two qubits")
    lengths = tuple(lengths)
    if num_qubits == 1:
        # Preserve the established 1q catalogue, seeds and gate words exactly.
        conditions = rb_sequences(lengths, sequence_count=sequence_count, seed=seed)
    else:
        if (
            not lengths
            or any(
                isinstance(m, (bool, np.bool_))
                or not isinstance(m, (int, np.integer))
                or m < 0
                for m in lengths
            )
            or len(set(lengths)) != len(lengths)
        ):
            raise ValueError("RB lengths must be unique nonnegative integers")
        if type(sequence_count) is not int or sequence_count <= 0:
            raise ValueError("sequence_count must be a positive integer")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        conditions = (
            {"length": int(length), "sequence": sequence}
            for length in lengths
            for sequence in range(sequence_count)
        )
    catalogue = clifford_catalog(num_qubits)
    lookup = {entry.tableau: i for i, entry in enumerate(catalogue)}
    cz = RBGate("cz", (0, 1))
    output = []
    for old in conditions:
        length, sequence = old["length"], old["sequence"]
        if num_qubits == 1:
            indices = tuple(old["clifford_indices"])
        else:
            rng = np.random.default_rng(
                np.random.SeedSequence([seed, length, sequence])
            )
            indices = tuple(
                int(i) for i in rng.integers(0, len(catalogue), size=length)
            )
        for variant in ("reference", "interleaved") if interleaved else ("reference",):
            gates, total = [], _identity(num_qubits)
            for index in indices:
                entry = catalogue[index]
                total = _compose(entry.tableau, total, num_qubits)
                gates.extend(entry.gates)
                if variant == "interleaved":
                    gates.append(cz)
                    total = _compose(_gate_tableau(cz, 2), total, 2)
            recovery = lookup[_inverse(total, num_qubits)]
            gates.extend(catalogue[recovery].gates)
            if sequence_tableau(gates, num_qubits=num_qubits) != _identity(num_qubits):
                raise RuntimeError(
                    "RB recovery failed exact ideal identity verification"
                )
            output.append(
                RBSequence(
                    num_qubits,
                    seed,
                    length,
                    sequence,
                    variant,
                    indices,
                    recovery,
                    tuple(gates),
                )
            )
    return tuple(output)
