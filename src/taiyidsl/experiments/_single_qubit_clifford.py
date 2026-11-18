"""Build a single-qubit Clifford catalogue and reproducible ideal RB sequences.

The workflow is: define elementary rotations -> enumerate the 24 Cliffords and
their gate decompositions -> sample Clifford indices -> append the inverse of
the entire sampled circuit. This replaces a hand-written 24-entry decomposition
table, but implements the same Clifford RB construction.

Matrices here act on the Bloch vector ``r = (x, y, z)``, not on the two-component
state vector. For ``rho = (I + r . sigma) / 2``, applying a 2x2 unitary ``U`` gives
``U rho U.dagger()`` with Bloch vector ``R @ r``. Thus ``R`` is 3x3, with entries
``R[i, j] = trace(sigma[i] @ U @ sigma[j] @ U.dagger()) / 2``. Its columns are the
images of the X, Y and Z axes. A global phase on U cancels in this expression.
Single-qubit Cliffords permute these axes with signs, so their rotations contain
only -1, 0 and 1. Integer arithmetic gives exact equality and inverse lookup;
there is no floating-point tolerance or search over global phases.

Gate tuples are in execution order: ``("x90", "y90")`` means X90 first, then
Y90, and has matrix ``R_y90 @ R_x90``. A "word" is just such a tuple of gate
names. Names describe ideal rotations, not calibrated pulse objects. Pulse
durations, amplitudes, virtual-Z implementations and readout belong to callers.
These algorithms use only NumPy and Python values and never build an experiment.
Both template builders and general RB descriptors share this catalogue.

The generator insertion order below determines catalogue indices and breaks
ties between equally short words. Keep it stable to preserve seeded circuits.
To implement the same logical gates using another native pulse basis, supply
an operation library to ``rb_sequence_section``; changing this dictionary is
not a supported per-experiment basis option. A new catalogue/decomposition
policy also needs coordinated descriptor, gate-validation and version changes.
"""

from collections import deque

import numpy as np

# Active +pi/2 rotations: Rx maps (x, y, z) to (x, -z, y),
# and Ry maps (x, y, z) to (z, y, -x). Orthogonality gives R(-theta) = R(theta).T.
_RX = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=int)

_RY = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], dtype=int)

# Each entry costs one step in the breadth-first search, including pi pulses.
GATE_ROTATIONS = {
    "x90": _RX,
    "xm90": _RX.T,
    "y90": _RY,
    "ym90": _RY.T,
    "x180": _RX @ _RX,
    "y180": _RY @ _RY,
}


def _rotation_key(matrix):
    """Encode an exact 3x3 integer rotation as a hashable nine-integer tuple.

    NumPy arrays cannot be dictionary keys or set members. Flattening in row
    order makes equal rotations share a key, regardless of which gate word
    produced them. For example, the identity becomes
    ``(1, 0, 0, 0, 1, 0, 0, 0, 1)``. ``clifford_group`` uses this for deduplication;
    ``rb_sequences`` uses it to find the index of the recovery rotation.

    The input is assumed to be an exact integer rotation from this module.
    ``int(v)`` normalizes NumPy scalar types; it does not round approximate
    rotations, check the matrix shape, or assign a Clifford index by itself.
    """
    return tuple(int(v) for v in matrix.flat)


def clifford_group():
    """Return all 24 single-qubit Clifford rotations and shortest gate words.

    Start from the identity with an empty word, then use a FIFO queue to explore
    every one-gate extension before longer extensions (breadth-first search).
    If ``matrix`` represents the current word, appending ``name`` in execution
    order gives ``gate @ matrix`` and ``gates + (name,)``. A visited set keeps
    only the first word reaching each rotation; for example, X90 followed by
    X90 is already represented by the one-step word ``("x180",)``.

    The generators close on the orientation-preserving signed permutations of
    the three Bloch axes: six choices for the image of X, four perpendicular
    choices for Y, and then a unique right-handed image of Z, giving 6 * 4 = 24.
    These are the single-qubit Cliffords modulo global phase. The size check
    below guards against an incomplete or accidentally changed generator set.

    "Shortest" means the fewest entries from ``GATE_ROTATIONS``, all with equal
    cost. It does not mean shortest calibrated duration or lowest physical error.
    Dictionary insertion order breaks ties and determines catalogue numbering;
    these indices need not match a manually ordered Clifford table.

    Returns:
        Tuple of ``(matrix, gates)`` pairs. ``matrix`` is an independent 3x3
        integer array; ``gates`` is a tuple of names in execution order. Entry
        zero is ``(identity, ())``: identity requires no pulse or inserted wait.
        Other entries are ordered by first discovery in the search.
    """
    identity = np.eye(3, dtype=int)
    queue = deque([(identity, ())])
    visited = {_rotation_key(identity)}
    group = []
    while queue:
        matrix, gates = queue.popleft()
        group.append((matrix.copy(), gates))
        for name, gate in GATE_ROTATIONS.items():
            candidate = gate @ matrix
            key = _rotation_key(candidate)
            if key not in visited:
                visited.add(key)
                queue.append((candidate, gates + (name,)))
    if len(group) != 24:
        raise RuntimeError("Clifford generator did not produce the 24-element group")
    return tuple(group)


def sequence_rotation(gates):
    """Compose named elementary gates in execution order into one 3x3 rotation.

    For ``gates = (g0, g1, ..., gn)``, return ``R_gn @ ... @ R_g1 @ R_g0``.
    The empty sequence returns identity. Names must be in ``GATE_ROTATIONS``;
    this function consumes elementary names, not Clifford catalogue indices.
    It checks ideal algebra only, with no pulse or hardware error model.
    """
    total = np.eye(3, dtype=int)
    for gate in gates:
        total = GATE_ROTATIONS[gate] @ total
    return total


def rb_sequences(lengths, *, sequence_count, seed):
    """Sample uniform Clifford RB circuits and append exact inverse recoveries.

    For each length m and sequence ID s, seed a separate NumPy generator with
    ``SeedSequence([seed, m, s])`` and draw m indices uniformly with replacement
    from all 24 catalogue entries. Sampling Cliffords uniformly is different
    from randomly choosing elementary X/Y pulses. Repeated indices, identity
    Cliffords, and coincident circuits for different IDs are allowed.

    Expand each sampled Clifford into its elementary word while accumulating
    ``total = C[m-1] @ ... @ C[0]``. Orthogonal rotations obey
    ``inverse(total) = total.T``. Look up that rotation in the same catalogue
    and append its word, so the final rotation is ``total.T @ total = I``.
    Finally recompute the flattened elementary sequence to verify exact ideal
    identity (equivalently, identity up to global phase in the 2x2 picture).

    With the same catalogue and NumPy RNG implementation, a (seed, m, s) triple
    reproduces the same circuit. Reordering lengths or adding more sequence IDs
    does not change existing triples. Different lengths use different streams:
    a shorter circuit is not constructed as a prefix of a longer one. Shots
    repeat the frozen circuit; this function does not redraw gates per shot.

    Args:
        lengths: Nonempty iterable of distinct nonnegative integer Clifford
            counts. Each count excludes the final recovery Clifford and is not
            the number of elementary pulses. Booleans are rejected.
        sequence_count: Positive Python int giving IDs 0 through count - 1 at
            each length. This is the number of random circuits, not shots.
        seed: Nonnegative Python int controlling reproducible sampling.

    Returns:
        List of dictionaries, ordered first by the supplied lengths, then by
        ascending sequence ID. Each contains ``length``, ``sequence``,
        ``clifford_indices`` (m sampled indices, excluding recovery),
        ``recovery_index`` (one catalogue index), and ``gates`` (a flat list of
        elementary names including recovery, in execution order). At m = 0,
        indices and gates are empty and recovery_index is zero. Identity words
        emit no pulses, so there need not be m + 1 elementary gates.

    Raises:
        ValueError: Invalid lengths, sequence_count or seed.
        RuntimeError: The catalogue size or exact recovery check fails.
    """
    lengths = tuple(lengths)
    if not lengths or any(
        isinstance(m, (bool, np.bool_)) or not isinstance(m, (int, np.integer)) or m < 0
        for m in lengths
    ):
        raise ValueError("RB lengths must be nonnegative integers")
    if len(set(lengths)) != len(lengths):
        raise ValueError("RB lengths must be unique")
    if (
        isinstance(sequence_count, bool)
        or not isinstance(sequence_count, int)
        or sequence_count <= 0
    ):
        raise ValueError("sequence_count must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    group = clifford_group()
    lookup = {_rotation_key(matrix): i for i, (matrix, _) in enumerate(group)}
    output = []
    for length in lengths:
        for sequence in range(sequence_count):
            rng = np.random.default_rng(
                np.random.SeedSequence([seed, int(length), sequence])
            )
            indices = [int(i) for i in rng.integers(0, len(group), size=int(length))]
            total = np.eye(3, dtype=int)
            gates = []
            for i in indices:
                matrix, word = group[i]
                total = matrix @ total
                gates.extend(word)
            # One inverse of the whole circuit, not an inverse after each Clifford.
            recovery = lookup[_rotation_key(total.T)]
            gates.extend(group[recovery][1])
            if not np.array_equal(sequence_rotation(gates), np.eye(3, dtype=int)):
                raise RuntimeError("RB recovery failed ideal identity verification")
            output.append(
                {
                    "length": int(length),
                    "sequence": sequence,
                    "clifford_indices": indices,
                    "recovery_index": recovery,
                    "gates": gates,
                }
            )
    return output
