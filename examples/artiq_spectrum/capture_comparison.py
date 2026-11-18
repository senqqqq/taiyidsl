"""Generate an explicitly synthetic capture comparison, without instruments.

Run from the project root with python -m examples.artiq_spectrum.capture_comparison.
For instrument data use compare_capture/write_capture_comparison with the saved
artifact and its real capture metadata; this example never labels data as measured.
"""

import argparse
from pathlib import Path

import numpy as np

from taiyidsl import Experiment, LabConfig, Qubit, Square, ns, play
from taiyidsl.reporting.capture_view import write_capture_comparison
from taiyidsl.reporting.output_simulator import simulate_output


def build_report(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    profile = LabConfig.load(Path(__file__).with_name("lab_simulated.json"))
    q = Qubit("q")
    exp = Experiment("capture_comparison", [q], metadata={"offline_only": True})
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        play(q.rf, Square(800 * ns, frequency=11e6, amplitude=0.5))
    compiled = profile.compile(exp)
    trace = next(
        t
        for t in simulate_output(compiled, duration_s=400e-9)["traces"]
        if t["card"] == "A" and t["channel"] == 0
    )
    voltage = trace["voltage_v"][10:-10]
    origin = float(trace["time_s"][10]) + 4e-9
    capture = directory / "synthetic_capture.npz"
    np.savez(capture, voltage=voltage + 0.01)
    metadata = {
        "artifact_sha256": compiled.sha256,
        "point": 0,
        "shot": 0,
        "attempt": 0,
        "run_id": "offline_synthetic_fixture",
        "group": "qubit_awg",
        "card": "A",
        "channel": 0,
        "sample_rate_hz": trace["sample_rate_hz"],
        "time_origin_s": origin,
        "deskew_ns": 4.0,
        "calibration_revision": profile.setup.spectrum.groups["qubit_awg"].calibration[
            "revision"
        ],
        "synthetic": True,
    }
    compiled.save(directory / "compiled.json")
    return write_capture_comparison(
        compiled,
        capture,
        metadata,
        directory / "index.html",
        exploratory_alignment=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/extended_hybrid/capture")
    )
    args = parser.parse_args()
    result = build_report(args.output)
    print(result["html"].resolve())
    print("Synthetic capture only; hardware acceptance not evaluated.")


if __name__ == "__main__":
    main()
