"""Driver trace checks use real SPI durations, independently of DSL budgets."""

import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_square_driver.py"
spec = importlib.util.spec_from_file_location("verify_square_driver", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def trace_fixture():
    point = {
        "index": 0,
        "physical_events": [
            {
                "kind": "dds_prepare",
                "signal": "dds0",
                "start_mu": 0,
                "duration_mu": 1280,
                "params": {"ftw": 123, "square_pow": 45, "asf": 678},
            },
            {"kind": "dds_commit", "signal": "dds0", "start_mu": 1280},
            {"kind": "dds_commit_off", "signal": "dds0", "start_mu": 1288},
            {"kind": "dds_effect", "signal": "dds0", "start_mu": 1360},
            {"kind": "dds_on", "signal": "dds0", "start_mu": 1360},
            {"kind": "dds_off", "signal": "dds0", "start_mu": 5360},
        ],
    }
    writes = [
        {
            "device": "dds0",
            "address": 0x15,
            "hi": (678 << 16) | 45,
            "lo": 123,
            "start_mu": 10000,
            "end_mu": 11248,
        }
    ]
    edges = [
        {"timestamp_mu": t, "target": target, "value": value}
        for t, target, value in (
            (11280, 256, 1),
            (11288, 256, 0),
            (11360, 512, 1),
            (15360, 512, 0),
        )
    ]
    targets = {"dds0": {"update": 256, "switch": 512}}
    return point, writes, edges, targets


def test_driver_trace_compares_cursor_cost_instead_of_assuming_budget():
    args = trace_fixture()
    assert audit.compare_trace(*args)["maximum_profile_duration_mu"] == 1248
    point, writes, edges, targets = copy.deepcopy(args)
    point["physical_events"][0]["duration_mu"] = 1000
    with pytest.raises(ValueError, match="exceeds declared"):
        audit.compare_trace(point, writes, edges, targets)


@pytest.mark.parametrize("mutation", ["missing_fall", "late_commit", "wrong_pow"])
def test_driver_trace_rejects_incorrect_generated_events(mutation):
    point, writes, edges, targets = trace_fixture()
    if mutation == "missing_fall":
        edges.pop(1)
    elif mutation == "late_commit":
        edges[0]["timestamp_mu"] += 8
    else:
        writes[0]["hi"] += 1
    with pytest.raises(ValueError, match="differs"):
        audit.compare_trace(point, writes, edges, targets)


def test_virtual_clock_rejects_rewinding_a_real_driver_submission():
    clock = audit.VirtualClock(1e-9)
    clock.set_time_mu(100)
    clock.take_time_mu(1248)
    with pytest.raises(ValueError, match="rewinds"):
        clock.set_time_mu(1100)


def test_actual_driver_edges_include_spi_in_coarse_lane_budget():
    edges = [
        {"timestamp_mu": 1000 + i % 8, "target": i << 8, "value": 1} for i in range(9)
    ]
    with pytest.raises(ValueError, match="target declares 8 RTIO lanes"):
        audit.validate_rtio_trace(edges, 8, 8)
    assert audit.validate_rtio_trace(edges, 8, 16) == 9


@pytest.mark.skipif(
    os.name != "nt"
    or not (ROOT / ".toolchains/artiq9/clang64/bin/python.exe").exists(),
    reason="Optional installed ARTIQ 9 toolchain is unavailable",
)
@pytest.mark.parametrize("mode", ["auto", "unrolled"])
@pytest.mark.parametrize("timed", [True, False])
def test_generated_square_runs_real_driver_bodies(tmp_path, mode, timed):
    from taiyidsl import (
        Device,
        DeviceSetup,
        Experiment,
        Qubit,
        SignalMap,
        Square,
        compile_experiment,
        play,
        us,
    )
    from taiyidsl.artiq import export_artiq

    qubits = [Qubit("q0"), Qubit("q1")]
    setup = DeviceSetup(
        {
            f"dds{i}": Device(
                f"dds{i}",
                "dds",
                bus=f"spi{i}",
                update_group=f"board{i}",
                square_latency_mu=80 if timed else None,
            )
            for i in range(2)
        }
    )
    mapping = SignalMap({f"q{i}.raman": f"dds{i}" for i in range(2)})
    exp = Experiment("real_driver_square", qubits)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for i, q in enumerate(qubits):
                    with exp.serial():
                        play(q.raman, Square(4 * us, frequency=80e6, amplitude=0.5))
                        if timed:
                            play(
                                q.raman,
                                Square(
                                    4 * us,
                                    frequency=(85 + i) * 1e6,
                                    amplitude=0.25,
                                    phase=0.75,
                                ),
                            )
    export = tmp_path / mode
    export_artiq(compile_experiment(exp, setup, mapping), export, mode=mode)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts/artiq-python.ps1"),
            str(SCRIPT),
            str(export),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    report = json.loads((export / "real_driver_trace.json").read_text(encoding="utf-8"))
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    assert report["status"] == "passed"
    duration = (
        "maximum_profile_duration_mu" if timed else "maximum_configuration_duration_mu"
    )
    assert report["points"][0][duration] == (1248 if timed else 2664)
    assert not report["hardware_executed"]
