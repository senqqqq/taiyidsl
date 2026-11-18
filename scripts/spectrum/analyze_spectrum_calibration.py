"""Analyze saved Spectrum measurements; this command never opens an instrument."""

import argparse
import json
from pathlib import Path

from taiyidsl.spectrum_calibration import analyze_measurements, load_capture_npz


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurements", type=Path)
    parser.add_argument("calibration", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.measurements.read_text(encoding="utf-8"))
    calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
    records = list(data.get("records", []))
    for item in data.get("captures", []):
        records.append(
            load_capture_npz(args.measurements.parent / item["file"], item["metadata"])
        )
    result = analyze_measurements(
        records,
        calibration,
        corrections_applied=data["corrections_applied"],
        required_channels=data.get("required_channels"),
        lock_calibrations=data.get("lock_calibrations"),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(f"Spectrum physical acceptance: {result['status']}")


if __name__ == "__main__":
    main()
