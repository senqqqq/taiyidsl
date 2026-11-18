"""Explicit parallel branches, reusable operations and a shared-duration scan.

Run: uv run --locked python examples/parallel_experiments.py
Builders return ordinary Experiment objects independently of lab configuration.
The executable demo uses an illustrative board per qubit and a shared camera
trigger. It exports offline artifacts; it never runs kernels or accesses devices.
Preparation, illumination, camera exposure and transfer belong to the lab recipe.
"""

from pathlib import Path

from taiyidsl import (
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    acquire,
    delay,
    play,
    reference_lab,
    reset_frame,
    us,
)


def pulse_pair(exp, qubit, pulse, *, gap=2 * us, second_phase=1):
    """One serial branch: pulse -> explicit gap -> phase-overridden pulse.

    Wrapping the operation in serial makes the whole function call a branch
    when used inside parallel. The immutable pulse template is not modified.
    The gap is an experiment choice; the compiler checks the lab's constraints.
    """
    with exp.serial():
        play(qubit.raman, pulse)
        delay(qubit.raman, gap)
        play(qubit.raman, pulse, phase=second_phase)


def build_precise_offsets(
    qubits, *, shots=1, readout_signal=None, trigger_duration=10 * us
):
    """q0: 0..4 and 6..10 us; q1: 2..8 us; shared trigger at 10 us."""
    exp = Experiment("precise_parallel_offsets", qubits)
    if len(exp.qubits) != 2:
        raise ValueError("The precise-offset example needs exactly two qubits")
    q0, q1 = exp.qubits.values()
    signal = q0.readout if readout_signal is None else readout_signal
    pulse = Square(4 * us)
    with exp.average(shots):
        with exp.realtime():
            with exp.parallel():
                pulse_pair(exp, q0, pulse)
                with exp.serial():
                    delay(q1.raman, 2 * us)
                    play(q1.raman, pulse, duration=6 * us)
            acquire(
                signal,
                duration=trigger_duration,
                handle="population",
                qubits=tuple(exp.qubits),
            )
    return exp


def build_parallel_rabi(
    qubits,
    durations,
    *,
    shots=100,
    mode="cyclic",
    readout_signal=None,
    trigger_duration=10 * us,
):
    """Scan one common duration, retaining each qubit's drive calibration.

    Bare durations use seconds; unit-bearing quantities are also accepted.
    This is a construction scaffold, not a multi-qubit calibration fitter.
    The camera trigger width is separate from exposure and transfer waits.
    """
    exp = Experiment(
        "parallel_rabi",
        qubits,
        metadata={"experiment_type": "rabi", "scan_policy": "shared_duration"},
    )
    if not exp.qubits:
        raise ValueError("Parallel Rabi requires at least one qubit")
    first = next(iter(exp.qubits.values()))
    signal = first.readout if readout_signal is None else readout_signal
    duration = SweepParameter("duration", durations, unit="s")
    pulse = Square(first.params.raman.pi_duration)
    with exp.average(shots, mode=mode):
        with exp.sweep(duration):
            with exp.realtime():
                with exp.parallel():
                    for q in exp.qubits.values():
                        with exp.serial():
                            reset_frame(q.frame)
                            play(q.raman, pulse, duration=duration)
                acquire(
                    signal,
                    duration=trigger_duration,
                    handle="population",
                    qubits=tuple(exp.qubits),
                )
    return exp


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "output" / "parallel_experiments"
    qubits = [Qubit("q0"), Qubit("q1")]
    # Replace this illustrative profile with the laboratory's LabConfig.
    lab = reference_lab(qubits)
    experiments = (
        build_precise_offsets(qubits),
        build_parallel_rabi(qubits, (0 * us, 4 * us, 8 * us), shots=2),
    )
    for exp in experiments:
        compiled = lab.compile(exp)
        directory = output / exp.name
        compiled.save(directory / "compiled.json")
        compiled.visualize(directory / "timeline.html")
        compiled.export_artiq(directory / "artiq")
        print(f"{exp.name}: {len(compiled.points)} points; output: {directory}")


if __name__ == "__main__":
    main()
