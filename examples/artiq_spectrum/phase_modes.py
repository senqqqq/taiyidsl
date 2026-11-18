"""Compare four explicit Spectrum phase policies without opening instruments.

The simulated laboratory profile is copied in memory. Frequencies, amplitudes,
phase curves and timing are illustrative, not calibrated physical gates. The
default command writes a compact JSON comparison; --reports exports four full
offline reports including final packed DAC samples.
"""

import argparse
import json
from pathlib import Path

from taiyidsl import (
    Experiment,
    LabConfig,
    PolynomialPhase,
    Qubit,
    Square,
    delay,
    ns,
    play,
    reset_frame,
    reset_phase,
    rz,
    shift_phase,
)


def build_lab(carrier_mode="epoch"):
    """Choose each output's carrier policy in laboratory configuration."""
    profile = LabConfig.load(Path(__file__).with_name("lab_simulated.json")).to_dict()
    output = profile["setup"]["spectrum"]["groups"]["qubit_awg"]["outputs"][
        "nuclear_rf"
    ]
    output["carrier_mode"] = carrier_mode
    if carrier_mode == "continuous":
        # Physical Hz, before the first pulse; deliberately differs from 11 MHz.
        output["initial_frequency_hz"] = 7e6
    return LabConfig.from_dict(profile)


def build_experiment(modulation_mode="local"):
    """Use one waveform in both modes; only its following state differs."""
    q = Qubit("q")
    exp = Experiment(
        f"phase_modes_{modulation_mode}",
        [q],
        metadata={"offline_only": True, "hardware_verified": False},
    )
    first = Square(
        400 * ns,
        amplitude=0.3,
        frequency=11e6,
        phase=0.17,
        # Nonzero entrance +0.2, then +0.7: full endpoint is +0.9 rad.
        phase_modulation=PolynomialPhase((0.2, 0.7)),
        phase_modulation_mode=modulation_mode,
    )
    with exp.average(2), exp.realtime():
        # Spectrum-only logical qubit: no DDS command is generated here.
        rz(q, 0.3)
        with exp.triggered_sequence(group="qubit_awg"):
            delay(80 * ns)
            shift_phase(q.frame, 0.4)
            play(q.rf, first)
            reset_frame(q.frame)  # Clear F, including Rz; retain carrier/carry.
            delay(120 * ns)
            play(q.rf, Square(400 * ns, amplitude=0.3, frequency=13e6))
            reset_phase(q.frame)  # New carrier reference; discard carried modulation.
            play(q.rf, Square(400 * ns, amplitude=0.3, frequency=13e6))
    return exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/phase_modes")
    )
    parser.add_argument("--reports", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {
        "verification": "offline_compilation_only_not_hardware_acceptance",
        "cases": [],
    }
    for carrier_mode in ("epoch", "continuous"):
        for modulation_mode in ("local", "carry"):
            name = f"{carrier_mode}_{modulation_mode}"
            compiled = build_lab(carrier_mode).compile(
                build_experiment(modulation_mode)
            )
            group = next(
                event.params
                for event in compiled.points[0].physical_events
                if event.kind == "spectrum_program"
            )
            program = group["cards"]["A"]["program"]
            case = {
                "name": name,
                "terminal_frame_rad": compiled.frame_phases(),
                "terminal_virtual_z_rad": compiled.virtual_z_frames(),
                "terminal_frames": group["terminal_frames"],
                "pulses": [
                    {
                        "phase_rad": pulse["phase_rad"],
                        "phase_evolution": pulse["phase_evolution"],
                    }
                    for pulse in program["pulses"]
                ],
                "segment_sha256": [
                    segment["sha256"] for segment in program["segments"]
                ],
            }
            if args.reports:
                result = compiled.export_report(
                    args.output / name, source_path=__file__
                )
                case["report"] = str(result["index"].resolve())
            summary["cases"].append(case)
    path = args.output / "phase_comparison.json"
    path.write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Offline phase comparison: {path.resolve()}")


if __name__ == "__main__":
    main()
