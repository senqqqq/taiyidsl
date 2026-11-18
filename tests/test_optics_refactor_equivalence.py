"""Preserve all events while simplifying the public optics parameter API."""

import json
import runpy
from pathlib import Path

import pytest
from optics_fingerprint import assert_logical_baseline

from taiyidsl import LabConfig, OpticsConfig, Qubit

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = "tests/fixtures/pre_optics_refactor_events.json"
BASELINE = json.loads((ROOT / BASELINE_PATH).read_text(encoding="utf-8"))


def compare_examples():
    init = runpy.run_path(str(ROOT / "examples/state_initialization.py"))
    rsc = runpy.run_path(str(ROOT / "examples/raman_sideband_cooling.py"))
    q = Qubit("q0")
    labs = {
        name: LabConfig.load(ROOT / f"examples/config/labs/{name}.json", qubit_id=q.uid)
        for name in ("initialization", "rsc")
    }
    parameters = {
        name: OpticsConfig.load(ROOT / f"examples/config/optics/{name}.json")
        for name in labs
    }
    cases = (
        ("initialization", "initialization", init["build_initialization_scan"]),
        ("rsc_round_scan", "rsc", rsc["build_cooling_scan"]),
        ("rsc_thermometry_x", "rsc", rsc["build_thermometry_scan"]),
    )
    results = {}
    for mode in ("cyclic", "sequential"):
        for name, profile, builder in cases:
            key = f"{name}/{mode}"
            experiment = builder(q, parameters[profile], averaging=mode)
            actual = assert_logical_baseline(
                labs[profile].compile(experiment), BASELINE["experiments"][key]
            )
            results[key] = {"status": "passed", **actual}
    return {
        "status": "passed",
        "total_points": sum(len(item["points"]) for item in results.values()),
        "experiments": results,
        "baseline": BASELINE_PATH,
        "original_source_hashes": BASELINE["source_hashes"],
        "scope": (
            "Frozen logical events including zero-duration delays, experiment "
            "durations, acquisition coordinates and both averaging orders; new "
            "prepare/commit/effect execution independently verified"
        ),
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
    }


@pytest.fixture(scope="module")
def report():
    return compare_examples()


@pytest.mark.parametrize("mode", ["cyclic", "sequential"])
@pytest.mark.parametrize(
    "name,count",
    [
        ("initialization", 12),
        ("rsc_round_scan", 6),
        ("rsc_thermometry_x", 12),
    ],
)
def test_all_original_events_are_unchanged(report, mode, name, count):
    result = report["experiments"][f"{name}/{mode}"]
    assert len(result["points"]) == count
    assert result["status"] == "passed"
    assert result["initialization"]["status"] == "passed"
