"""Continuous Square updates and live hardware phase control; offline only.

The 80 ns matched DDS latency below is an illustrative timing contract. Supply
measured values and validate the real target before executing any generated code.
"""

import math
from pathlib import Path

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    delay,
    play,
    reset_frame,
    shift_phase,
    us,
)
from taiyidsl.artiq import verify_equivalence


def square_lab(qubits):
    """Declare independent boards and explicit, unmeasured timing assumptions."""
    devices, signals = {}, {}
    for index, q in enumerate(qubits):
        name = f"dds{index}"
        devices[name] = Device(
            name,
            "dds",
            bus=f"spi{index}",
            update_group=f"board{index}",
            square_latency_mu=80,
            square_prepare_duration_mu=1280,
            spi_ref_period_mu=8,
        )
        signals[q.raman] = name
        # A separate logical lane positions frame commands during a long pulse.
        control = f"control{index}"
        devices[control] = Device(control, "ttl", grid_mu=1, min_duration_mu=1)
        signals[q.readout] = control
    return LabConfig(
        DeviceSetup(devices, name="square-timing-offline-assumptions"),
        SignalMap(signals),
    )


def build_continuous_pair():
    qubits = [Qubit("q0"), Qubit("q1")]
    lab = square_lab(qubits)
    exp = Experiment("square_continuous_pair", qubits)
    pulse = Square(4 * us, frequency=125e6, amplitude=0.5)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qubits:
                    with exp.serial():
                        play(q.raman, pulse)
                        play(q.raman, pulse, phase=math.pi / 2)
                        play(q.raman, pulse, frequency=130e6, amplitude=0.25)
    return lab, exp


def build_live_phase():
    q = Qubit("q0")
    lab = square_lab([q])
    exp = Experiment("square_live_phase", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                play(q.raman, Square(12 * us, frequency=125e6, amplitude=0.5))
                with exp.serial():
                    delay(q.readout, 4 * us)
                    shift_phase(q.uid, math.pi / 2)
                    delay(q.readout, 4 * us)
                    reset_frame(q.uid)
    return lab, exp


def main():
    output = Path(__file__).resolve().parents[1] / "output" / "square_timing"
    for build in (build_continuous_pair, build_live_phase):
        lab, exp = build()
        compiled = lab.compile(exp)
        compiled.export_artiq(output / exp.name)
        compiled.visualize(output / f"{exp.name}.html")
        print(exp.name, verify_equivalence(compiled)["status"])
    print("Offline artifacts only; no hardware execution or timing calibration.")


if __name__ == "__main__":
    main()
