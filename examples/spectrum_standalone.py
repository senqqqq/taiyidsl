"""Compile the same two-pulse frequency scan for a single AWG or DDS20 card."""

import argparse
import math
from pathlib import Path

from taiyidsl import (
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    delay,
    ns,
    play,
)


def single_card_lab(mode, *, serial_number="OFFLINE", emulation=True):
    spec_db = {
        "A": {
            "serial_number": serial_number,
            "mode": mode,
            "active_channels": [0],
            "output_range_mv": 500,
        }
    }
    descriptor = {
        "backend": "spectrum",
        "instruments": {"A": {"kind": "spectrum", "device": "A"}},
        "channels": {"rf_out": {"instrument": "A", "channel": 0}},
        "signals": {"q0": {"rf": "rf_out"}},
    }
    setup = DeviceSetup.from_descriptor(descriptor, spec_db=spec_db)
    return LabConfig(setup, emulation=emulation)


def two_pulses(q0, pulse, frequencies_mhz, *, shots):
    frequency = SweepParameter("frequency", frequencies_mhz, "MHz")
    exp = Experiment("two_pulses", [q0])
    with exp.average(shots), exp.sweep(frequency), exp.realtime():
        play(q0.rf, pulse, frequency=frequency)
        delay(1280 * ns)
        play(q0.rf, pulse, frequency=frequency, phase=math.pi / 2)
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("awg", "dds"), default="awg")
    parser.add_argument(
        "--output", type=Path, default=Path("output/spectrum_standalone")
    )
    args = parser.parse_args()
    q0 = Qubit("q0")
    pulse = Square(640 * ns, amplitude=0.2)
    exp = two_pulses(q0, pulse, [10, 11, 12], shots=20)
    compiled = single_card_lab(args.mode).compile(exp)
    destination = args.output / args.mode
    compiled.export(destination)
    print(f"Exported {len(compiled.points)} scan points to {destination}")


if __name__ == "__main__":
    main()
