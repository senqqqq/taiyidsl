"""Compare canonical loaders/builders against frozen pre-refactor fingerprints."""

import json
import runpy
from pathlib import Path

import pytest
from optics_fingerprint import assert_logical_baseline

from taiyidsl import LabConfig, OpticsConfig, Qubit

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads(
    (ROOT / "tests/fixtures/pre_refactor_events.json").read_text(encoding="utf-8")
)["experiments"]


def compare_examples():
    q = Qubit("q0")
    init = runpy.run_path(str(ROOT / "examples/state_initialization.py"))
    rsc = runpy.run_path(str(ROOT / "examples/raman_sideband_cooling.py"))
    init_lab = LabConfig.load(
        ROOT / "examples/config/labs/initialization.json", qubit_id=q.uid
    )
    rsc_lab = LabConfig.load(ROOT / "examples/config/labs/rsc.json", qubit_id=q.uid)
    init_params = OpticsConfig.load(ROOT / "examples/config/optics/initialization.json")
    rsc_params = OpticsConfig.load(ROOT / "examples/config/optics/rsc.json")
    plans = {
        "initialization": init_lab.compile(
            init["build_initialization_scan"](q, init_params)
        ),
        "rsc_round_scan": rsc_lab.compile(rsc["build_cooling_scan"](q, rsc_params)),
        "rsc_thermometry_x": rsc_lab.compile(
            rsc["build_thermometry_scan"](q, rsc_params)
        ),
    }
    result = {}
    for name, plan in plans.items():
        actual = assert_logical_baseline(plan, BASELINE[name], ignore_zero_delays=True)
        result[name] = {"status": "passed", **actual}
    return {
        "status": "passed",
        "total_points": sum(len(item["points"]) for item in result.values()),
        "experiments": result,
        "baseline": "tests/fixtures/pre_refactor_events.json",
        "scope": (
            "Frozen logical events, coordinates, experiment durations and host order; "
            "new prepare/commit/effect execution independently verified"
        ),
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
    }


@pytest.fixture(scope="module")
def report():
    return compare_examples()


@pytest.mark.parametrize(
    "name,count",
    [
        ("initialization", 12),
        ("rsc_round_scan", 6),
        ("rsc_thermometry_x", 12),
    ],
)
def test_original_events_are_unchanged(report, name, count):
    assert len(report["experiments"][name]["points"]) == count
    assert report["experiments"][name]["status"] == "passed"
