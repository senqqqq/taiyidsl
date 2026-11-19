"""Build and inspect a pair of pulses on one declared RF signal."""

from taiyidsl import Experiment, MHz, Qubit, Square, delay, play, us


def pulse_pair():
    qubit = Qubit(
        "q0",
        signals={"rf": "q0.rf"},
        parameters={"rf": {"frequency": 10 * MHz, "amplitude": 0.25}},
    )
    pulse = Square(4 * us)
    experiment = Experiment("pulse_pair", [qubit])
    with experiment.average(1), experiment.realtime():
        play(qubit.rf, pulse)
        delay(qubit.rf, 12 * us)
        play(qubit.rf, pulse, phase=0.5)
    return experiment


if __name__ == "__main__":
    experiment = pulse_pair()
    for node in experiment.root.children[0].children[0].children:
        print(node.kind, node.params)
