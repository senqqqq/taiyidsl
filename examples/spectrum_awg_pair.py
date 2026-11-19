"""Compile two square pulses for one or two co-triggered Spectrum AWGs."""

import argparse
from pathlib import Path

from taiyidsl import (
    ControlBinding,
    ControlRecipe,
    Device,
    DeviceSetup,
    Experiment,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    compile_experiment,
    delay,
    ns,
    play,
)


def compile_pair(card_count=2, *, memory_bytes=4 * 1024**3):
    if card_count not in (1, 2):
        raise ValueError("Choose one or two AWGs for this example")
    names = ("A", "B")[:card_count]
    group = SpectrumGroup(
        cards={
            name: SpectrumCard(
                f"SIM_{name}",
                (0,),
                trigger_latency_samples=500 - 16 * index,
                memory_bytes=memory_bytes,
            )
            for index, name in enumerate(names)
        },
        outputs={f"out_{name}": SpectrumOutput(name, 0) for name in names},
        trigger_devices={name: f"trig_{name}" for name in names},
    )
    devices = {f"out_{name}": Device(f"out_{name}", "spectrum") for name in names}
    devices.update({f"trig_{name}": Device(f"trig_{name}", "ttl") for name in names})
    setup = DeviceSetup(devices, spectrum=SpectrumConfig({"pair": group}, {"rf": None}))
    mapping = SignalMap(
        {"rf": ControlRecipe(tuple(ControlBinding(f"out_{n}") for n in names))}
    )
    exp = Experiment("two_awg_pulses", [])
    pulse = Square(400 * ns, amplitude=0.25, frequency=1e6)
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="pair"):
        play("rf", pulse)
        delay("rf", 800 * ns)
        play("rf", pulse)
    return compile_experiment(exp, setup, mapping, backend="artiq_spectrum")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=int, choices=[1, 2], default=2)
    parser.add_argument("--output", type=Path, default=Path("output/spectrum_awg_pair"))
    args = parser.parse_args()
    print(compile_pair(args.cards).export(args.output)["manifest"])
