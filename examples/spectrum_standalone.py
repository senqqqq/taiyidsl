"""Compile offline, check an idle Spectrum card (--check), or play the scan (--run)."""

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


def single_card_lab(mode, *, serial_number="OFFLINE", address=None, emulation=True):
    spec_db = {
        "A": {
            "serial_number": serial_number,
            "address": address,
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
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check", action="store_true", help="Connect to an idle card without playback"
    )
    action.add_argument(
        "--run", action="store_true", help="Connect and play all shots on the card"
    )
    parser.add_argument(
        "--serial-number", type=int, help="Physical card S/N (required to connect)"
    )
    parser.add_argument("--address", help="Optional device path, distinct from the S/N")
    parser.add_argument(
        "--output", type=Path, default=Path("output/spectrum_standalone")
    )
    args = parser.parse_args()
    hardware = args.check or args.run
    if hardware and args.serial_number is None:
        parser.error("--check/--run requires --serial-number (use the S/N on the card)")
    if args.serial_number is not None and args.serial_number <= 0:
        parser.error("--serial-number must be a positive integer")

    lab = single_card_lab(
        args.mode,
        serial_number=str(args.serial_number) if args.serial_number else "OFFLINE",
        address=args.address,
        emulation=not hardware,
    )
    if args.check:
        # No upload or run; the SDK closes the card on exit, so check an idle card.
        with lab.connect() as session:
            card = session.observed
            print(
                f"Connected: {card['model']} S/N {card['serial_number']} ({args.mode})"
            )
        return

    q0 = Qubit("q0")
    pulse = Square(640 * ns, amplitude=0.2)
    exp = two_pulses(q0, pulse, [10, 11, 12], shots=20)
    compiled = lab.compile(exp)

    destination = args.output / args.mode
    compiled.export(destination)
    print(f"Exported {len(compiled.points)} scan points to {destination}")

    if not args.run:
        print("Compile only: no card opened. Use --run --serial-number SN to play.")
        return

    # The session owns the card and releases it on normal exit or error.
    with lab.connect() as session:
        receipt = session.run(compiled)
        completed = len(receipt["completed_shots"])
        print(f"Completed {completed} shots on S/N {args.serial_number}")
        print(f"Session records: {session.directory}")


if __name__ == "__main__":
    main()
