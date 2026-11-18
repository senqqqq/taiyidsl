"""View-only concatenation keeps runtime order and does not invent host time."""

import copy
import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from taiyidsl.chunking import execution_order
from taiyidsl.viewer_execution import execution_axis

JS = Path(__file__).parents[1] / "src/taiyidsl/reporting/assets/viewer_execution.js"


def test_known_windows_include_preroll_fixed_native_suffix_and_dac_tail():
    spectrum = SimpleNamespace(
        kind="spectrum_program",
        params={
            "trigger_physical_mu": 900,
            "trigger_time_unit_s": 1e-9,
            "cards": {
                "A": {
                    "contract": {"trigger_latency_samples": 250},
                    "program": {
                        "card_spec": {"sample_rate_hz": 1_250_000_000},
                        "physical_duration_samples": 1408,
                    },
                }
            },
        },
    )
    point = SimpleNamespace(
        index=0,
        origin_mu=1000,
        duration_mu=1100,
        logical_events=[
            SimpleNamespace(
                kind="spectrum_region",
                signal="awg",
                start_mu=100,
                duration_mu=1000,
                params={},
            )
        ],
        physical_events=[
            SimpleNamespace(kind="dds_config", start_mu=500, duration_mu=100),
            spectrum,
        ],
    )
    compiled = SimpleNamespace(
        points=[point],
        shots=2,
        averaging_mode="cyclic",
        setup=SimpleNamespace(
            tick_seconds=1e-9, spectrum=SimpleNamespace(artiq_tick_seconds=1e-9)
        ),
        metadata={
            "native_artiq": {
                "package": {},
                "points": [
                    {
                        "prefix": [{"method": "prepare", "args": []}],
                        "suffix": [
                            {
                                "method": "readout",
                                "args": [],
                                "duration_mu": 2000,
                                "phase_sensitive": False,
                            }
                        ],
                    }
                ],
            }
        },
    )
    unchanged = copy.deepcopy(spectrum.params)
    axis = execution_axis(compiled)
    window = axis["points"][0]
    assert window["control"]["start_s"] == -0.5e-6
    assert window["control"]["end_s"] == 3.1e-6
    assert window["awg"]["start_s"] == 0.1e-6
    assert Fraction(*map(int, window["awg"]["end_fraction"])) == Fraction(
        1533, 1_250_000_000
    )
    assert window["native_prefix_duration_unknown"]
    assert not axis["wall_clock_time"] and not axis["host_intervals_included"]
    assert spectrum.params == unchanged


def test_javascript_pages_match_host_order_without_expanding_shot_count():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for execution-axis viewer checks")
    points = [
        SimpleNamespace(
            index=index,
            origin_mu=0,
            duration_mu=duration,
            logical_events=[],
            physical_events=[],
        )
        for index, duration in enumerate((3, 5, 7))
    ]
    requests, expected = [], []
    for scope in ("global", "chunk"):
        for mode in ("cyclic", "sequential"):
            plan = {
                "shots": 3,
                "point_count": 3,
                "averaging_mode": mode,
                "averaging_scope": scope,
                "chunks": [{"index": 0, "points": [0, 1]}, {"index": 1, "points": [2]}],
            }
            compiled = SimpleNamespace(
                points=points,
                setup=SimpleNamespace(tick_seconds=1e-9),
                host_execution_plan=plan,
            )
            axis = execution_axis(compiled)
            requests.append({"axis": axis})
            order = list(execution_order(plan))
            expected.append(order)
    # A distant page must use arithmetic indexing, not allocate one billion shots.
    large = copy.deepcopy(requests[0]["axis"])
    large["shots"], large["total_executions"] = 1_000_000_000, 3_000_000_000
    requests.append(
        {
            "axis": large,
            "options": {"start": 2_999_999_997, "count": 3},
        }
    )
    assert len(json.dumps(large)) < 4000
    script = """
const fs = require('node:fs'), api = require(process.argv[1]);
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(requests.map(({axis,options}) => ({
  page: api.selectExecutions(axis, options),
  point: api.selectExecutions(axis, {point:1,count:3}),
  shot: api.selectExecutions(axis, {shot:1,count:3})
}))));
"""
    result = subprocess.run(
        [node, "-e", script, str(JS)],
        input=json.dumps(requests),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=15,
    )
    decoded = json.loads(result.stdout)
    for result, order in zip(decoded[:4], expected, strict=True):
        entries = result["page"]["entries"]
        assert [(e["point"], e["shot"]) for e in entries] == order
        elapsed = Fraction(0)
        for ordinal, entry in enumerate(entries):
            assert entry["ordinal"] == ordinal
            assert Fraction(*map(int, entry["start_fraction"])) == elapsed
            elapsed += Fraction((3, 5, 7)[entry["point"]], 1_000_000_000)
            assert Fraction(*map(int, entry["end_fraction"])) == elapsed
        assert [e["ordinal"] for e in result["point"]["entries"]] == [
            i for i, (point, _) in enumerate(order) if point == 1
        ]
        assert [e["ordinal"] for e in result["shot"]["entries"]] == [
            i for i, (_, shot) in enumerate(order) if shot == 1
        ]
    last = decoded[-1]["page"]["entries"]
    assert [(e["point"], e["shot"]) for e in last] == [
        (0, 999_999_999),
        (1, 999_999_999),
        (2, 999_999_999),
    ]
    assert Fraction(*map(int, last[-1]["end_fraction"])) == 15
