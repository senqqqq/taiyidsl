"""Small, inspectable tune-up experiment builders and seeded Clifford RB."""

from __future__ import annotations

import math
from itertools import groupby

from ..dsl.experiment import Experiment
from ..dsl.instructions import delay, measure, play, prepare, reset_frame
from ..dsl.pulses import Square
from ..dsl.values import SweepParameter
from ..units import as_fraction
from ._single_qubit_clifford import _RX as _RX
from ._single_qubit_clifford import _RY as _RY
from ._single_qubit_clifford import GATE_ROTATIONS as GATE_ROTATIONS
from ._single_qubit_clifford import _rotation_key as _rotation_key
from ._single_qubit_clifford import clifford_group as clifford_group
from ._single_qubit_clifford import rb_sequences as rb_sequences
from ._single_qubit_clifford import sequence_rotation as sequence_rotation


def rabi(qubit, durations, *, shots=100, mode="cyclic", amplitude=None):
    """Scan one pulse's duration; per-call overrides leave its template intact."""
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
    pulse = Square(
        q.params.raman.pi_duration,
        amplitude=q.params.raman.amplitude,
        frequency=q.params.raman.frequency,
    )
    duration = SweepParameter("duration", values=durations, unit="s")
    with exp.average(shots=shots, mode=mode, execution="host"):
        with exp.sweep(duration) as duration:
            prepare(q, protocol="standard")
            with exp.realtime(timing="strict"):
                reset_frame(q.frame)
                play(
                    q.raman,
                    pulse,
                    duration=duration,
                    amplitude=chosen_amplitude,
                )
                measure(q, protocol="state_readout", handle="population")
    return exp


def ramsey(qubit, delays, *, shots=100, mode="cyclic", detuning=50e3):
    """Reuse a calibrated pi/2 pulse with the same detuning on both calls."""
    detuning = float(as_fraction(detuning, "frequency"))
    if not math.isfinite(float(detuning)):
        raise ValueError("detuning must be finite")
    exp = Experiment(
        "ramsey",
        qubits=[qubit],
        metadata={"experiment_type": "ramsey", "detuning": float(detuning)},
    )
    q = exp.qubits[qubit.uid]
    pulse = Square(
        q.params.raman.pi2_duration,
        amplitude=q.params.raman.amplitude,
        frequency=q.params.raman.frequency,
    )
    frequency = q.params.raman.frequency + detuning
    tau = SweepParameter("tau", values=delays, unit="s")
    with exp.average(shots=shots, mode=mode, execution="host"):
        with exp.sweep(tau) as tau:
            prepare(q, protocol="standard")
            with exp.realtime(timing="strict"):
                reset_frame(q.frame)
                play(q.raman, pulse, frequency=frequency)
                delay(q.raman, tau)
                play(q.raman, pulse, frequency=frequency)
                measure(q, protocol="state_readout", handle="population")
    return exp


_PHASES = {
    "x90": 0.0,
    "xm90": math.pi,
    "y90": math.pi / 2,
    "ym90": -math.pi / 2,
    "x180": 0.0,
    "y180": math.pi / 2,
}


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
    Cases select genuinely different gate sequences; pulse parameters alone
    cannot replace their variable-length structure.
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
    pulse = Square(
        q.params.raman.pi2_duration,
        amplitude=q.params.raman.amplitude,
        frequency=q.params.raman.frequency,
    )
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
                    for length, items in groupby(sequences, key=lambda s: s["length"]):
                        with exp.case(length_param, length):
                            for item in items:
                                with exp.case(sequence_param, item["sequence"]):
                                    for i, gate in enumerate(item["gates"]):
                                        duration = (
                                            q.params.raman.pi_duration
                                            if gate.endswith("180")
                                            else q.params.raman.pi2_duration
                                        )
                                        play(
                                            q.raman,
                                            pulse,
                                            duration=duration,
                                            phase=_PHASES[gate],
                                        )
                                        if i + 1 < len(item["gates"]):
                                            delay(q.raman, inter_gate_delay)
                    measure(q, protocol="state_readout", handle="population")
    return exp
