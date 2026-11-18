"""Introductory GreenMOT fixture + Ramsey; default invocation is compile-only.

Run: python -m examples.artiq_spectrum.simple_ramsey
Edit ramsey_lab device databases, wiring, qubits and native declaration separately.
"""

from pathlib import Path

from taiyidsl import DeviceSetup, LabConfig, SweepParameter, deg, kHz, ramsey_experiment

from .ramsey_lab import native
from .ramsey_lab.qubits import q0
from .ramsey_lab.wiring import descriptor

LAB = Path(__file__).with_name("ramsey_lab")


def build():
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=LAB / "device_db.py", spec_db=LAB / "spec_db.py"
    )
    lab = LabConfig(setup=setup, qubits=[q0], emulation=True)
    exp = ramsey_experiment(
        qubit=q0,
        native=native,
        delays=SweepParameter("tau", [1, 10, 100], "us"),
        detuning=1 * kHz,
        second_phase=0 * deg,
        shots=2,
    )
    return lab, exp


def main():
    lab, exp = build()
    compiled = lab.compile(exp)
    result = compiled.export_report("output/simple_ramsey/report", source_path=__file__)
    print(result["index"].resolve())


if __name__ == "__main__":
    main()
