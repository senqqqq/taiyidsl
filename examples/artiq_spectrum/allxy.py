"""Offline standard 21-pair AllXY, with two equal 200 us gate slots.

X/Y denote pi, x/y denote pi/2, I denotes a 200 us zero-output wait. The
illustrative amplitude convention is pi=0.4, pi/2=0.2, carrier=30 kHz; it does
not assume that halving amplitude gives an accurately calibrated physical gate.
All phases are radians. No measurements or fitted calibration are generated.

Pair order: Reed, Yale PhD thesis (2013), Sec. 5.2.3 / Table 5.1:
https://rsl.yale.edu/sites/default/files/2024-08/2013-RSL-Thesis-Matthew-Reed.pdf
"""

import argparse
import math
from pathlib import Path

from taiyidsl import (
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    delay,
    play,
    us,
)

ALLXY_PAIRS = (
    ("I", "I"),
    ("X", "X"),
    ("Y", "Y"),
    ("X", "Y"),
    ("Y", "X"),
    ("x", "I"),
    ("y", "I"),
    ("x", "y"),
    ("y", "x"),
    ("x", "Y"),
    ("y", "X"),
    ("X", "y"),
    ("Y", "x"),
    ("x", "X"),
    ("X", "x"),
    ("y", "Y"),
    ("Y", "y"),
    ("X", "I"),
    ("Y", "I"),
    ("x", "x"),
    ("y", "y"),
)
GATE_SLOT = 200 * us


def build_experiment(*, averages=2):
    q = Qubit("q")
    exp = Experiment(
        "hybrid_nuclear_allxy",
        [q],
        metadata={
            "offline_only": True,
            "hardware_verified": False,
            "calibrated_rotations": False,
            "allxy_pairs": ALLXY_PAIRS,
            "gate_slot_us": 200,
            "initial_logical_frames": {"H_rad": 0, "V_rad": 0},
            "theoretical_reference_only": {
                "ideal_P1": [0.0] * 5 + [0.5] * 12 + [1.0] * 4,
                "assumptions": "initial |0>, ideal rotations, no decoherence",
            },
        },
    )
    pair_index = SweepParameter("pair_index", range(len(ALLXY_PAIRS)), "")
    pulse = Square(GATE_SLOT, amplitude=0.4, frequency=30_000)

    with exp.average(averages, mode="cyclic"):
        with exp.sweep(pair_index):
            with exp.realtime():
                play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
                with exp.triggered_sequence(group="qubit_awg"):
                    # Static selection: one pair per point, never a runtime branch.
                    for index, pair in enumerate(ALLXY_PAIRS):
                        with exp.case(pair_index, index):
                            for gate in pair:
                                if gate == "I":
                                    delay(GATE_SLOT)
                                else:
                                    play(
                                        q.rf,
                                        pulse,
                                        amplitude=0.4 if gate.isupper() else 0.2,
                                        phase=math.pi / 2 if gate.lower() == "y" else 0,
                                    )
                play("readout", Square(10 * us))
    return exp


def main(argv=None):
    from taiyidsl.reporting.experiment_report import export_experiment_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/allxy")
    )
    args = parser.parse_args(argv)
    lab = LabConfig.load(Path(__file__).with_name("lab_simulated.json"))
    compiled = lab.compile(build_experiment())
    report = export_experiment_report(compiled, args.output, source_path=__file__)
    print(f"Offline hybrid AllXY report: {report['index'].resolve()}")
    return report


if __name__ == "__main__":
    main()
