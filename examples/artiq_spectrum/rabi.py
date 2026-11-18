"""Offline nuclear-spin Rabi amplitude/length scan on the hybrid backend.

The reference pi rotation is only a naming convention: 200 us, amplitude 0.4,
30 kHz. At that fixed duration, amplitude 0.2 denotes an illustrative pi/2.
Actual pulse areas, resonance, state preparation and readout need calibration.
This script builds and exports a program; it never opens hardware or fits data.
"""

import argparse
from pathlib import Path

from taiyidsl import (
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    play,
    us,
)


def build_experiment(
    *, amplitudes=(0, 0.1, 0.2, 0.3, 0.4, 0.5), durations_us=(100, 200, 300), averages=2
):
    q = Qubit("q")
    exp = Experiment(
        "hybrid_nuclear_rabi",
        [q],
        metadata={
            "offline_only": True,
            "hardware_verified": False,
            "calibrated_rotations": False,
            "reference_pi": {"duration_us": 200, "amplitude": 0.4},
            "carrier_hz": 30_000,
            "initial_logical_frames": {"H_rad": 0, "V_rad": 0},
            "readout": "placeholder TTL only; no measured population",
        },
    )
    amplitude = SweepParameter("amplitude", amplitudes, "")
    duration = SweepParameter("duration", durations_us, "us")
    pulse = Square(200 * us, amplitude=0.4, frequency=30_000, phase=0)

    with exp.average(averages, mode="cyclic"):
        with exp.sweep(amplitude):
            with exp.sweep(duration):
                with exp.realtime():
                    # Replace these native ARTIQ placeholders with lab preparation.
                    play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
                    with exp.triggered_sequence(group="qubit_awg"):
                        play(q.rf, pulse, amplitude=amplitude, duration=duration)
                    play("readout", Square(10 * us))
    return exp


def main(argv=None):
    from taiyidsl.reporting.experiment_report import export_experiment_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/rabi")
    )
    args = parser.parse_args(argv)
    lab = LabConfig.load(Path(__file__).with_name("lab_simulated.json"))
    compiled = lab.compile(build_experiment())
    report = export_experiment_report(compiled, args.output, source_path=__file__)
    print(f"Offline hybrid Rabi report: {report['index'].resolve()}")
    return report


if __name__ == "__main__":
    main()
