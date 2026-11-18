"""Offline Hahn echo: X(pi/2), tau, Y(pi), tau, X(pi/2).

All pulses occupy 200 us: illustrative pi amplitude 0.4 and pi/2 amplitude 0.2,
at 30 kHz. The swept half_wait is DARK TIME, so total dark time is 2*half_wait;
finite pulse durations remain explicit. No T2 result or calibrated gate is implied.
Convention: R_phi(theta)=exp[-i theta(cos(phi)X+sin(phi)Y)/2], radians.
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


def build_experiment(*, half_wait_us=(0, 50, 500, 5000), averages=2):
    q = Qubit("q")
    exp = Experiment(
        "hybrid_nuclear_spin_echo",
        [q],
        metadata={
            "offline_only": True,
            "hardware_verified": False,
            "calibrated_rotations": False,
            "sequence": "X90 - tau - Y180 - tau - X90",
            "half_wait_definition": "dark time excluding the finite RF pulses",
            "initial_logical_frames": {"H_rad": 0, "V_rad": 0},
            "theoretical_reference_only": "ideal resonant sequence ends in |1>",
        },
    )
    half_wait = SweepParameter("half_wait", half_wait_us, "us")
    pulse = Square(200 * us, amplitude=0.4, frequency=30_000)

    with exp.average(averages, mode="cyclic"):
        with exp.sweep(half_wait):
            with exp.realtime():
                play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
                with exp.triggered_sequence(group="qubit_awg"):
                    play(q.rf, pulse, amplitude=0.2, phase=0)
                    delay(half_wait)
                    play(q.rf, pulse, amplitude=0.4, phase=math.pi / 2)
                    delay(half_wait)
                    play(q.rf, pulse, amplitude=0.2, phase=0)
                play("readout", Square(10 * us))
    return exp


def main(argv=None):
    from taiyidsl.reporting.experiment_report import export_experiment_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/spin_echo")
    )
    args = parser.parse_args(argv)
    lab = LabConfig.load(Path(__file__).with_name("lab_simulated.json"))
    compiled = lab.compile(build_experiment())
    report = export_experiment_report(compiled, args.output, source_path=__file__)
    print(f"Offline hybrid spin-echo report: {report['index'].resolve()}")
    return report


if __name__ == "__main__":
    main()
