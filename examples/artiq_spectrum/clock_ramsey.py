"""GreenMOT fixture + RF/clock Ramsey; default invocation is compile-only.

Run: python -m examples.artiq_spectrum.clock_ramsey
Clock pi time 100 us gives one continuous 200 us demonstration pulse at 200 kHz.
The second RF pi/2 phase is scanned over 0, 45, 90, 135 and 180 degrees.
"""

from pathlib import Path

from taiyidsl import DeviceSetup, LabConfig

from .ramsey_lab.experiments import clock_ramsey_experiment
from .ramsey_lab.native import prepare_MOT, readout
from .ramsey_lab.qubits import q0
from .ramsey_lab.wiring import descriptor

LAB = Path(__file__).with_name("ramsey_lab")


def build():
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=LAB / "device_db.py", spec_db=LAB / "spec_db.py"
    )
    lab = LabConfig(setup=setup, qubits=[q0], emulation=True)
    exp = clock_ramsey_experiment(qubit=q0, prepare=prepare_MOT, readout=readout)
    return lab, exp


def main():
    lab, exp = build()
    compiled = lab.compile(exp)
    result = compiled.export_report("output/clock_ramsey/report", source_path=__file__)
    print(result["index"].resolve())


if __name__ == "__main__":
    main()
