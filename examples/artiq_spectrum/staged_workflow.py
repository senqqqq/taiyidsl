"""Compose named preparation, cooling, AWG gates and readout DSL builders.

The default stage implementations below are explicitly OFFLINE TIMING FIXTURES.
They exercise several DDS/TTL operations, loops and parallel readout; they do not
implement a physical MOT, coils, laser locking, or atom-loading feedback. Supply
laboratory DSL builders and a matching LabConfig for an actual experiment.
Existing native ARTIQ @kernel functions are not accepted as these builders.
"""

import argparse
import inspect
import math
from functools import partial
from pathlib import Path

from taiyidsl import (
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    acquire,
    delay,
    play,
    rz,
    us,
)


def _builder(function, name):
    """Catch accidental native/asynchronous callbacks, not sandbox user Python."""
    target = function
    while isinstance(target, partial):
        target = target.func
    if not callable(target):
        raise TypeError(f"{name} must be a DSL builder")
    candidates = (target, getattr(target, "__call__", None))
    if any(hasattr(candidate, "artiq_embedded") for candidate in candidates):
        raise TypeError(
            f"{name} is native ARTIQ code, not a DSL builder; "
            "declare it through NativeKernelPackage (see greenmot_ramsey.py)"
        )
    if any(
        inspect.iscoroutinefunction(candidate)
        or inspect.isgeneratorfunction(candidate)
        or inspect.isasyncgenfunction(candidate)
        for candidate in candidates
    ):
        raise TypeError(f"{name} must synchronously append DSL nodes")
    return function


def build_experiment(
    *,
    prepare_MOT,
    cooling,
    execute_readout,
    parameters,
    dark_times_us=(10, 1000),
    shots=2,
):
    """Compose explicit builders with signature (exp, qubit, parameters).

    Each builder runs once to construct the IR, not once per physical shot. Use
    DSL sweep/repeat/case for point-dependent operations. This function does not
    call any hardware. Supplied builders are trusted Python and must also remain
    offline; a wrapper cannot detect arbitrary side effects hidden inside them.
    """
    prepare_MOT = _builder(prepare_MOT, "prepare_MOT")
    cooling = _builder(cooling, "cooling")
    execute_readout = _builder(execute_readout, "execute_readout")
    q = Qubit("q")
    exp = Experiment(
        "hybrid_staged_workflow",
        [q],
        parameters=parameters,
        metadata={
            "stage_implementation": "caller_supplied_DSL_builders",
            "stage_validation": "not_verified",
            "native_kernel_embedding": False,
            "hardware_verified": False,
        },
    )
    p = exp.parameters
    dark_time = SweepParameter("dark_time", dark_times_us, "us")
    pulse = Square(200 * us, amplitude=0.2, frequency=30000)

    with exp.average(shots, mode="cyclic"):
        with exp.sweep(dark_time):
            with exp.realtime():
                prepare_MOT(exp, q, p)
                cooling(exp, q, p)

                with exp.triggered_sequence(group="qubit_awg"):
                    play(q.rf, pulse)
                    delay(dark_time)
                    rz(q, math.pi / 2)
                    play(q.rf, pulse)

                execute_readout(exp, q, p)
    return exp


def demo_prepare_MOT(exp, q, p):
    """Two DDS intervals only; NOT a working MOT or native ARTIQ kernel."""
    cfg = p["mot_fixture"]
    with exp.stage("prepare_MOT · offline timing fixture", parameters=cfg):
        play("cooling", Square(cfg["load_s"], amplitude=0.2, frequency=80e6))
        delay("cooling", cfg["settle_s"])
        play("cooling", Square(cfg["compress_s"], amplitude=0.1, frequency=80e6))
        delay("cooling", cfg["settle_s"])


def demo_cooling(exp, q, p):
    """A finite multi-operation cooling fixture, explicit DDS-off waits."""
    cfg = p["cooling_fixture"]
    with exp.stage("cooling · offline timing fixture", parameters=cfg):
        with exp.repeat(cfg["cycles"]):
            play("cooling", Square(cfg["pulse_s"], amplitude=0.15, frequency=80e6))
            delay("cooling", cfg["settle_s"])


def demo_execute_readout(exp, q, p):
    """Parallel illumination/acquisition declaration, no camera IO at build time."""
    cfg = p["readout_fixture"]
    with exp.stage("execute_readout · offline timing fixture", parameters=cfg):
        with exp.parallel(exact_duration=cfg["exposure_s"]):
            play("cooling", Square(cfg["exposure_s"], amplitude=0.2, frequency=80e6))
            acquire(
                "readout",
                duration=cfg["trigger_s"],
                handle="post",
                qubits=[q],
                metadata={
                    "exposure_s": cfg["exposure_s"],
                    "camera_configured": False,
                    "data_collection": "host result adapter after shot completion",
                },
            )
        delay("readout", cfg["settle_s"])


def build_demo():
    exp = build_experiment(
        prepare_MOT=demo_prepare_MOT,
        cooling=demo_cooling,
        execute_readout=demo_execute_readout,
        parameters={
            "mot_fixture": {"load_s": 1e-3, "compress_s": 0.5e-3, "settle_s": 10e-6},
            "cooling_fixture": {"cycles": 3, "pulse_s": 20e-6, "settle_s": 10e-6},
            "readout_fixture": {
                "exposure_s": 60e-6,
                "trigger_s": 10e-6,
                "settle_s": 20e-6,
            },
        },
    )
    exp.metadata["stage_implementation"] = "offline_DDS_TTL_fixtures_not_physical_MOT"
    exp.metadata["offline_only"] = True
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/staged_workflow")
    )
    args = parser.parse_args()
    lab = LabConfig.load(Path(__file__).with_name("lab_simulated.json"))
    compiled = lab.compile(build_demo())
    result = compiled.export_report(args.output, source_path=__file__)
    print(f"Offline staged workflow: {result['index'].resolve()}")


if __name__ == "__main__":
    main()
