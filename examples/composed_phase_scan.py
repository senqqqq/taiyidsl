"""A two-point scan of the delay between pulses, with a reusable rotation."""

import math

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    SweepParameter,
    delay,
    ns,
    play,
    qubit_operation,
    reset_phase,
    rz,
    section,
)


@qubit_operation
def drive(qubit):
    play(qubit.raman, Square(800 * ns, amplitude=0.25, frequency=83.123456e6))


def compile_scan(mode="cyclic"):
    qubit = Qubit("q0")
    gap = SweepParameter("gap", [4000, 6000], "ns")
    experiment = Experiment("composed_phase_scan", [qubit])
    with experiment.average(2, mode=mode), experiment.sweep(gap), experiment.realtime():
        with section("pulse_pair", schedule="serial"):
            drive(qubit)
            delay(qubit.raman, gap)
            reset_phase(qubit.frame)
            rz(qubit, math.pi / 2)
            drive(qubit)

    # This example declares an offline, matched-latency Square path.
    dds = Device("dds0", "dds", bus="spi", update_group="board", square_latency_mu=80)
    lab = LabConfig(DeviceSetup({"dds0": dds}), SignalMap({qubit.raman: "dds0"}))
    return lab.compile(experiment)


if __name__ == "__main__":
    compiled = compile_scan()
    print("Host (point, shot) order:", list(compiled.execution_order()))
    for point in compiled.points:
        plays = [event for event in point.logical_events if event.kind == "play"]
        print("Point", point.index, [(e.start_mu, e.params["phase"]) for e in plays])
