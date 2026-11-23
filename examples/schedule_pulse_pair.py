"""Map the authoring example to a DDS time grid."""

from examples.pulse_pair import pulse_pair
from taiyidsl import Device, DeviceSetup, SignalMap
from taiyidsl.backends.artiq.backend import ArtiqBackend


def example_wiring():
    return DeviceSetup({"dds0": Device("dds0", "dds")}), SignalMap({"q0.rf": "dds0"})


def scheduled_pair():
    experiment = pulse_pair()
    setup, mapping = example_wiring()
    (average,) = experiment.root.children
    return ArtiqBackend().schedule(
        experiment, average.children, setup, mapping, {}, False, [], 1000
    )


if __name__ == "__main__":
    events, duration = scheduled_pair()
    for event in events:
        print(event.kind, event.signal, event.start_mu, event.duration_mu)
    print("Total ticks", duration)
