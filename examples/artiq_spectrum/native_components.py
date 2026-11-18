"""Four separate ARTIQ components around a DSL gate body; offline fixtures."""

import argparse
from pathlib import Path

from taiyidsl import Experiment, LabConfig, Qubit, Square, delay, play, rz, us

try:
    from examples.artiq_spectrum.native_stages.declarations import build_native_program
except ModuleNotFoundError as error:
    if error.name != "examples":
        raise
    from native_stages.declarations import build_native_program

HERE = Path(__file__).resolve().parent


def build_experiment():
    native = build_native_program()
    q = Qubit("q")
    exp = Experiment(
        "independent_native_components",
        [q],
        metadata={
            "offline_only": True,
            "physical_preparation": False,
            "native_components": ["mot", "cooling", "state", "readout"],
        },
    )
    with exp.average(2), exp.realtime():
        native.prepare_MOT()
        native.GM_cooling(20 * us)
        native.state_prep(8 * us)
        delay(10 * us)
        with exp.triggered_sequence(group="qubit_awg"):
            play(q.rf, Square(8 * us, amplitude=0.2, frequency=30_000))
            rz(q, 0.3)
            play(q.rf, Square(8 * us, amplitude=0.2, frequency=30_000))
        native.readout(10 * us)
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/native_components")
    )
    args = parser.parse_args()
    result = (
        LabConfig.load(HERE / "lab_simulated.json")
        .compile(build_experiment())
        .export(args.output)
    )
    print(result)


if __name__ == "__main__":
    main()
