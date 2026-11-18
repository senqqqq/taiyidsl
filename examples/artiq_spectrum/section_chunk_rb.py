"""Two-qubit RB Sections on two Spectrum cards with chunked host averaging.

All defaults are offline. The TTL preparation marker is an illustrative stage,
not an implementation of MOT loading. Pulse areas, output timing and two-card
phase alignment require laboratory calibration; no result or hardware is read.
"""

import argparse
from pathlib import Path

from taiyidsl import (
    ControlBinding,
    ControlRecipe,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    SweepParameter,
    acquire,
    ns,
    section,
    trig,
    us,
)
from taiyidsl.operations import rb_body
from taiyidsl.templates import rb_sequences


def build_lab():
    """Declare both EXT0 trigger sources separately from analog channel routing."""
    group = SpectrumGroup(
        cards={
            "A": SpectrumCard("SIM_RB_A", (0,)),
            "B": SpectrumCard("SIM_RB_B", (0,)),
        },
        outputs={
            "rf_q0": SpectrumOutput("A", 0),
            "rf_q1": SpectrumOutput("B", 0),
        },
        trigger_devices={"A": "awg_start_a", "B": "awg_start_b"},
        trigger_signal="qubit.start",
        calibration={"status": "unmeasured", "revision": "offline-section-rb"},
    )
    setup = DeviceSetup(
        {
            "rf_q0": Device("rf_q0", "spectrum"),
            "rf_q1": Device("rf_q1", "spectrum"),
            **{
                name: Device(name, "ttl", grid_mu=1)
                for name in ("awg_start_a", "awg_start_b", "prepare_ttl", "camera_ttl")
            },
        },
        spectrum=SpectrumConfig({"qubit_awg": group}),
    )
    signals = SignalMap(
        {
            "q0.rf": "rf_q0",
            "q1.rf": "rf_q1",
            "prepare.marker": "prepare_ttl",
            "camera.trigger": "camera_ttl",
            # This logical name confirms the complete configured trigger set. Its
            # two members are emitted at the same RTIO start and end timestamps.
            "qubit.start": ControlRecipe(
                (ControlBinding("awg_start_a"), ControlBinding("awg_start_b"))
            ),
        }
    )
    return LabConfig(setup, signals, backend="artiq_spectrum")


def build_experiment(*, seed=101, shots=2, chunks=2):
    """Generate random gate words once, then reuse their frozen Section bodies.

    The two qubits use independent seeds. Each word includes an exact inverse
    Clifford recovery under the ideal gate model. ASAP overlaps the two complete
    words; it does not interleave or change either word's gate order.
    """
    lengths = (1, 2, 3, 4)
    words = {
        "q0": rb_sequences(lengths, sequence_count=1, seed=seed),
        "q1": rb_sequences(lengths, sequence_count=1, seed=seed + 1),
    }
    q0, q1 = Qubit("q0"), Qubit("q1")
    exp = Experiment(
        "section_chunk_rb",
        [q0, q1],
        metadata={
            "offline_only": True,
            "hardware_verified": False,
            "calibrated_rotations": False,
            "seeds": {"q0": seed, "q1": seed + 1},
            "rb_sequences": words,
            "preparation": "20 us TTL marker only, not a MOT sequence",
            "readout": "shared camera trigger and acquisition plan, no camera model",
        },
    )
    length = SweepParameter("length", lengths, "dimensionless")
    # These durations illustrate an ideal pi/2:pi area ratio only. Supply real
    # calibrated pulses through the same rb_body interface in a laboratory.
    half = {
        q0.uid: Square(400 * ns, amplitude=0.3, frequency=11e6),
        q1.uid: Square(400 * ns, amplitude=0.3, frequency=13e6),
    }
    full = {
        q0.uid: Square(800 * ns, amplitude=0.3, frequency=11e6),
        q1.uid: Square(800 * ns, amplitude=0.3, frequency=13e6),
    }
    with exp.average(shots, mode="cyclic", scope="chunk"):
        with exp.sweep(length, chunks=chunks), exp.realtime():
            trig("prepare.marker", width=20 * us)
            with exp.triggered_sequence(group="qubit_awg"):
                for index, value in enumerate(lengths):
                    with exp.case(length, value):
                        with section(f"rb_length_{value}", schedule="asap"):
                            rb_body(
                                q0, words[q0.uid][index], half[q0.uid], full[q0.uid]
                            )
                            rb_body(
                                q1, words[q1.uid][index], half[q1.uid], full[q1.uid]
                            )
            # The enclosing serial timeline joins both complete gate words.
            acquire(
                "camera.trigger",
                handle="image",
                duration=64 * ns,
                qubits=[q0, q1],
                metadata={"offline_only": True},
            )
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=101)
    parser.add_argument("--shots", type=int, default=2)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/section_chunk_rb")
    )
    args = parser.parse_args()
    compiled = build_lab().compile(build_experiment(seed=args.seed, shots=args.shots))
    result = compiled.export_report(args.output, source_path=__file__)
    print(f"Offline Section/chunk RB report: {result['index'].resolve()}")


if __name__ == "__main__":
    main()
