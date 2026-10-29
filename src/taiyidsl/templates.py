"""Small, inspectable tune-up experiment builders and seeded Clifford RB."""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from .dsl import (
    Experiment,
    Square,
    SweepParameter,
    delay,
    measure,
    play,
    prepare,
    reset_frame,
)
from .units import as_fraction


def rabi(qubit, durations, *, shots=100, mode="cyclic", amplitude=None):
    chosen_amplitude = (
        qubit.params.raman.amplitude
        if amplitude is None
        else float(as_fraction(amplitude, "dimensionless"))
    )
    exp = Experiment(
        "rabi",
        qubits=[qubit],
        metadata={
            "experiment_type": "rabi",
            "model": "square_pulse",
            "calibration_drive": "raman",
            "calibration_amplitude": chosen_amplitude,
            "calibration_frequency": qubit.params.raman.frequency,
        },
    )
    q = exp.qubits[qubit.uid]
    duration = SweepParameter("duration", values=durations, unit="s")
    with exp.average(shots=shots, mode=mode, execution="host"):
        with exp.sweep(duration):
            prepare(q, protocol="standard")
            with exp.realtime(timing="strict"):
                reset_frame(q.frame)
                play(
                    q.raman,
                    Square(
                        duration=duration,
                        amplitude=chosen_amplitude,
                        frequency=q.params.raman.frequency,
                        phase=0.0,
                    ),
                )
                measure(q, protocol="state_readout", handle="population")
    return exp


def ramsey(qubit, delays, *, shots=100, mode="cyclic", detuning=50e3):
    detuning = float(as_fraction(detuning, "frequency"))
    if not math.isfinite(float(detuning)):
        raise ValueError("detuning must be finite")
    exp = Experiment(
        "ramsey",
        qubits=[qubit],
        metadata={"experiment_type": "ramsey", "detuning": float(detuning)},
    )
    q = exp.qubits[qubit.uid]
    tau = SweepParameter("tau", values=delays, unit="s")
    with exp.average(shots=shots, mode=mode, execution="host"):
        with exp.sweep(tau):
            prepare(q, protocol="standard")
            with exp.realtime(timing="strict"):
                reset_frame(q.frame)
                pulse = Square(
                    duration=q.params.raman.pi2_duration,
                    amplitude=q.params.raman.amplitude,
                    frequency=q.params.raman.frequency + float(detuning),
                    phase=0.0,
                )
                play(q.raman, pulse)
                delay(q.raman, tau)
                play(q.raman, pulse)
                measure(q, protocol="state_readout", handle="population")
    return exp


_RX = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=int)
_RY = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], dtype=int)
GATE_ROTATIONS = {
    "x90": _RX,
    "xm90": _RX.T,
    "y90": _RY,
    "ym90": _RY.T,
    "x180": _RX @ _RX,
    "y180": _RY @ _RY,
}
_PHASES = {
    "x90": 0.0,
    "xm90": math.pi,
    "y90": math.pi / 2,
    "ym90": -math.pi / 2,
    "x180": 0.0,
    "y180": math.pi / 2,
}


def _rotation_key(matrix):
    return tuple(int(v) for v in matrix.flat)


def clifford_group():
    """Return all 24 single-qubit Clifford rotations and shortest gate words.

    SO(3) integer rotations avoid numerical/global-phase ambiguity. Chronological
    gates are multiplied on the left. Returned matrices are independent copies.
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
    total = np.eye(3, dtype=int)
    for gate in gates:
        total = GATE_ROTATIONS[gate] @ total
    return total


def rb_sequences(lengths, *, sequence_count, seed):
    """Deterministic Clifford sequences with an exact inverse recovery."""
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


def randomized_benchmarking(
    qubit,
    lengths,
    *,
    sequence_count=5,
    shots=100,
    seed=0,
    inter_gate_delay,
    mode="cyclic",
):
    """Build static RB programs. The update gap is a required experiment choice.

    Different Clifford sequences are a condition axis, not new random draws for
    each shot. The exact waveform program and seed are saved in metadata.
    """
    lengths = tuple(lengths)
    inter_gate_delay = float(as_fraction(inter_gate_delay, "time"))
    if not math.isfinite(float(inter_gate_delay)) or float(inter_gate_delay) < 0:
        raise ValueError("inter_gate_delay must be finite and nonnegative")
    sequences = rb_sequences(lengths, sequence_count=sequence_count, seed=seed)
    exp = Experiment(
        "randomized_benchmarking",
        qubits=[qubit],
        metadata={
            "experiment_type": "rb",
            "seed": seed,
            "rb_sequences": sequences,
            "inter_gate_delay": float(inter_gate_delay),
            "length_definition": "Cliffords excluding recovery",
        },
    )
    q = exp.qubits[qubit.uid]
    length_param = SweepParameter("length", values=lengths, unit="dimensionless")
    sequence_param = SweepParameter(
        "sequence", values=range(sequence_count), unit="dimensionless"
    )
    with exp.average(shots=shots, mode=mode, execution="host"):
        with exp.sweep(length_param):
            with exp.sweep(sequence_param):
                prepare(q, protocol="standard")
                with exp.realtime(timing="strict"):
                    reset_frame(q.frame)
                    for item in sequences:
                        with exp.case(length_param, item["length"]):
                            with exp.case(sequence_param, item["sequence"]):
                                for i, gate in enumerate(item["gates"]):
                                    duration = (
                                        q.params.raman.pi_duration
                                        if gate.endswith("180")
                                        else q.params.raman.pi2_duration
                                    )
                                    play(
                                        q.raman,
                                        Square(
                                            duration=duration,
                                            amplitude=q.params.raman.amplitude,
                                            frequency=q.params.raman.frequency,
                                            phase=_PHASES[gate],
                                        ),
                                    )
                                    if i + 1 < len(item["gates"]):
                                        delay(q.raman, inter_gate_delay)
                    measure(q, protocol="state_readout", handle="population")
    return exp
