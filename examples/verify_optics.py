"""Independent offline exporter checks, kept out of experiment definitions.

Run after the examples: uv run python examples/verify_optics.py
This runs mock drivers only; it does not compile ARTIQ or access instruments.
"""

import argparse
import json
import runpy
from pathlib import Path

from taiyidsl import LabConfig, OpticsConfig, Qubit
from taiyidsl.backends.artiq.verification import verify_equivalence

EXPERIMENTS = {
    "initialization": (
        "state_initialization.py",
        "build_initialization_scan",
        "initialization",
        "state_initialization",
    ),
    "rsc_round_scan": (
        "raman_sideband_cooling.py",
        "build_cooling_scan",
        "rsc",
        "rsc_round_scan",
    ),
    "rsc_thermometry_x": (
        "raman_sideband_cooling.py",
        "build_thermometry_scan",
        "rsc",
        "rsc_thermometry_x",
    ),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", choices=EXPERIMENTS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    q = Qubit("q0")
    for name in args.experiment or EXPERIMENTS:
        filename, builder_name, profile, directory = EXPERIMENTS[name]
        builder = runpy.run_path(str(root / "examples" / filename))[builder_name]
        parameters = OpticsConfig.load(root / f"examples/config/optics/{profile}.json")
        lab = LabConfig.load(
            root / f"examples/config/labs/{profile}.json", qubit_id=q.uid
        )
        compiled = lab.compile(builder(q, parameters))
        report = verify_equivalence(compiled)
        report.update(artiq_compile="not_verified", hardware="not_verified")
        output = root / "output" / directory / "verification.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"{name}: {report['status']}; {len(compiled.points)} points")


if __name__ == "__main__":
    main()
