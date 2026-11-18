"""Offline 2/3/4-card example: one TTL per card, one simultaneous launch.

Run as ``python -m examples.artiq_spectrum.multicard_trigger --cards 4``.
The SIM card names, phases and delays are illustrative, not measured contracts.
"""

import argparse
from pathlib import Path

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    layer,
    ns,
    play,
)


def build(card_count=2):
    if type(card_count) is not int or card_count not in (2, 3, 4):
        raise ValueError("This example selects 2, 3 or 4 cards")
    names = tuple("ABCD"[:card_count])
    qubits = tuple(Qubit(f"q{index}") for index in range(card_count))
    routes = {name: f"awg_trigger_{name.lower()}" for name in names}
    outputs = {f"drive_{name}": SpectrumOutput(name, 0) for name in names}
    group = SpectrumGroup(
        cards={name: SpectrumCard(f"SIM_{name}", (0,)) for name in names},
        outputs=outputs,
        trigger_devices=routes,
        calibration={"status": "unmeasured", "revision": "multicard-demo"},
    )
    lab = LabConfig(
        DeviceSetup(
            {
                **{name: Device(name, "spectrum") for name in outputs},
                **{name: Device(name, "ttl") for name in routes.values()},
            },
            spectrum=SpectrumConfig({"qubit_awg": group}),
        ),
        SignalMap(
            {q.rf: f"drive_{name}" for q, name in zip(qubits, names, strict=True)}
        ),
        backend="artiq_spectrum",
    )
    exp = Experiment(
        f"synchronous_{card_count}_cards",
        qubits,
        metadata={"offline_only": True, "hardware_verified": False},
    )
    with exp.average(2), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        with layer(*qubits):
            for index, q in enumerate(qubits):
                play(
                    q.rf, Square(800 * ns, frequency=(10 + index) * 1e6, amplitude=0.2)
                )
    return lab, exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", type=int, choices=(2, 3, 4), default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    lab, exp = build(args.cards)
    destination = args.output or Path(f"output/artiq_spectrum/cards_{args.cards}")
    result = lab.compile(exp).export_report(destination, source_path=Path(__file__))
    print(result["index"].resolve())
    print(
        "Offline planned edges only; reference clock and measured deskew are required."
    )


if __name__ == "__main__":
    main()
