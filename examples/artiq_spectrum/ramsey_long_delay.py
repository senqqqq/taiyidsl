"""Complete hybrid Ramsey DSL; defaults only compile/export, never run hardware.

The two equal pulses illustrate timing and phase, not calibrated pi/2 gates.
The native cooling pulse and readout TTL are placeholders for laboratory recipes.
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
    rz,
    us,
)


def build_experiment(delays_ms=(0.01, 1, 100)):
    q = Qubit("q")
    exp = Experiment(
        "hybrid_ramsey_long_delay",
        [q],
        metadata={"offline_only": True, "calibrated_rotations": False},
    )
    dark_time = SweepParameter("dark_time", delays_ms, "ms")
    pulse = Square(200 * us, amplitude=0.2, frequency=30_000, phase=0)

    with exp.average(2, mode="cyclic"):
        with exp.sweep(dark_time):
            with exp.realtime():
                play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))

                with exp.triggered_sequence(group="qubit_awg"):
                    play(q.rf, pulse)
                    delay(dark_time)
                    rz(q, math.pi / 2)
                    play(q.rf, pulse)

                play("readout", Square(10 * us))
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/ramsey_long_delay")
    )
    args = parser.parse_args()
    lab = LabConfig.load(Path(__file__).with_name("lab_simulated.json"))
    assert lab.backend == "artiq_spectrum"
    result = lab.compile(build_experiment()).export_report(
        args.output, source_path=__file__
    )
    print(f"Offline hybrid Ramsey: {result['index'].resolve()}")


if __name__ == "__main__":
    main()
