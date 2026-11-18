"""Decorator, block trigger and independent falling-edge TTL; offline only.

The RF pulses and cooling DDS are timing examples, not calibrated physical gates
or MOT preparation. No acquisition or instrument execution is performed. Use
--default-trigger to omit the explicit block trigger and use the identical path
declared by this example's in-memory LabConfig.
"""

import argparse
import math
from pathlib import Path

from taiyidsl import (
    Device,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    average,
    delay,
    experiment,
    ns,
    play,
    realtime,
    rz,
    sweep,
    trig,
    triggered_sequence,
    us,
)

q = Qubit("q")


def build_lab():
    # Work on a serialized copy. The shared example profile stays unchanged.
    profile = LabConfig.load(Path(__file__).with_name("lab_simulated.json")).to_dict()
    group = profile["setup"]["spectrum"]["groups"]["qubit_awg"]
    profile["signals"]["qubit.start"] = [
        {"device": name} for name in sorted(set(group["trigger_devices"].values()))
    ]
    profile["setup"]["spectrum"]["groups"]["qubit_awg"]["trigger_signal"] = (
        "qubit.start"
    )
    profile["setup"]["devices"]["scope_ttl"] = Device(
        "scope_ttl", "ttl", grid_mu=1, ttl_idle_level=1
    ).to_dict()
    profile["signals"]["scope.trigger"] = [{"device": "scope_ttl"}]
    return LabConfig.from_dict(profile)


@experiment(
    qubits=[q],
    name="decorated_hybrid_ramsey",
    metadata={
        "offline_only": True,
        "calibrated_rotations": False,
        "hardware_verified": False,
        "scope_trigger": "independent falling-edge marker, not acquisition",
    },
)
def build_experiment(delays_ms=(0.01, 100), *, shots=2, explicit_trigger=True):
    """Return a fresh Experiment on every call; compiling is a separate step."""
    dark_time = SweepParameter("dark_time", delays_ms, "ms")
    pulse = Square(200 * us, amplitude=0.2, frequency=30_000, phase=0)
    # None selects group.trigger_signal. Both choices use the complete TTL fanout.
    start = "qubit.start" if explicit_trigger else None

    with average(shots, mode="cyclic"):
        with sweep(dark_time):
            with realtime():
                play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
                with triggered_sequence(group="qubit_awg", trigger=start):
                    play(q.rf, pulse)
                    delay(dark_time)
                    rz(q, math.pi / 2)
                    play(q.rf, pulse)

                # Separate physical TTL: high idle -> low for 64 ns -> high.
                # This does not retrigger the AWG, acquire data or reset a frame.
                trig("scope.trigger", width=64 * ns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--default-trigger", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/decorated_ramsey")
    )
    args = parser.parse_args()
    exp = build_experiment(explicit_trigger=not args.default_trigger)
    result = build_lab().compile(exp).export_report(args.output, source_path=__file__)
    print(f"Offline decorated Ramsey: {result['index'].resolve()}")


if __name__ == "__main__":
    main()
