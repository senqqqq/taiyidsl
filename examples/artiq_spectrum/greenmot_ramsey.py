"""Native GreenMOT functions + whole-sequence AWG Ramsey, offline export only.

Default: explicitly named native kernel fixture, not a physical MOT.
Actual code: --greenmot-source path/to/artiq-master/lib/_GreenMOTbase.py
The builder reads/pins source; it never imports ARTIQ or starts instruments.
"""

import argparse
import importlib.util
import math
from pathlib import Path

from taiyidsl import (
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    delay,
    play,
    rz,
    us,
)

HERE = Path(__file__).resolve().parent
NATIVE = HERE / "greenmot_native"
__all__ = [
    "GREENMOT_RESOURCES",
    "REVIEWED_COMMIT",
    "GreenMOTAdapter",
    "native_package",
    "validate_greenmot_split",
    "build_experiment",
]
try:
    from examples.artiq_spectrum.greenmot_native import adapter as _adapter
except ModuleNotFoundError as error:
    if error.name != "examples":
        raise
    # Compatibility with file/runpy launchers that omit the repository from
    # sys.path. Only load our host declaration; no native ARTIQ module is imported.
    _spec = importlib.util.spec_from_file_location(
        "greenmot_host_adapter", NATIVE / "adapter.py"
    )
    _adapter = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_adapter)

GREENMOT_RESOURCES = _adapter.GREENMOT_RESOURCES
REVIEWED_COMMIT = _adapter.REVIEWED_COMMIT
GreenMOTAdapter = _adapter.GreenMOTAdapter
native_package = _adapter.native_package
validate_greenmot_split = _adapter.validate_greenmot_split


def build_experiment(
    *,
    greenmot_source=None,
    delays_ms=(0.01, 1, 100),
    shots=2,
    preparation_to_gate=10 * us,
):
    native = GreenMOTAdapter(greenmot_source)
    prepare_MOT = native.prepare_MOT
    execute_readout = native.execute_readout
    q = Qubit("q")
    exp = Experiment(
        "greenmot_native_ramsey",
        [q],
        metadata={
            "offline_only": True,
            "native_source": "external_GreenMOT"
            if greenmot_source
            else "native_fixture",
            "physical_MOT_implemented": greenmot_source is not None,
            "reviewed_upstream_commit": REVIEWED_COMMIT,
            "upstream_byte_identity_verified": False,
            "calibrated_rotations": False,
            "hardware_verified": False,
            "readout_mode": "399_horizontal",
            "preparation_end": "final_Zotino_LDAC_rising_edge_or_fixture_TTL_edge",
            "analog_settling_verified": False,
            "legacy_scan": "not_nested_original_scan_index_was_printed_only",
        },
    )
    dark_time = SweepParameter("dark_time", delays_ms, "ms")
    pulse = Square(200 * us, amplitude=0.2, frequency=30_000, phase=0)

    with exp.average(shots, mode="cyclic"):
        with exp.sweep(dark_time):
            with exp.realtime():
                prepare_MOT()
                delay(preparation_to_gate)
                with exp.triggered_sequence(group="qubit_awg"):
                    play(q.rf, pulse)
                    delay(dark_time)
                    rz(q, math.pi / 2)
                    play(q.rf, pulse)
                execute_readout()
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--greenmot-source", type=Path)
    parser.add_argument("--lab", type=Path, default=HERE / "lab_simulated.json")
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/greenmot_ramsey")
    )
    args = parser.parse_args()
    exp = build_experiment(greenmot_source=args.greenmot_source)
    result = (
        LabConfig.load(args.lab)
        .compile(exp)
        .export_report(args.output, source_path=__file__)
    )
    print(f"Offline native composition ({exp.metadata['native_source']}):")
    print(result["index"].resolve())


if __name__ == "__main__":
    main()
