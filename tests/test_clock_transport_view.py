"""A common inspection axis must preserve actual DDS and packed AWG timing."""

import json
import runpy
from pathlib import Path

import numpy as np
import pytest

from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.backends.artiq_spectrum.dds import build_dds_program

ROOT = Path(__file__).parents[1]
VIEW = runpy.run_path(str(ROOT / "examples/artiq_spectrum/clock_transport_view.py"))
build_clock_transport_data = VIEW["build_clock_transport_data"]
export_clock_transport_view = VIEW["export_clock_transport_view"]
_dds_rows = VIEW["_dds_rows"]


@pytest.fixture(scope="module")
def compiled():
    example = runpy.run_path(str(ROOT / "examples/artiq_spectrum/transport_dds.py"))
    lab, experiment = example["build_demo"]()
    return lab.compile(experiment)


def test_common_axis_matches_actual_dac_sample_indices_and_dds_endpoint(compiled):
    data = build_clock_transport_data(compiled, max_bins=100)
    assert data["trigger_us"] == pytest.approx(-1.2)
    assert data["logical_end_us"] == pytest.approx(620.48)
    times = {item["card"]: item for item in data["card_timing"]}
    assert times["A"]["first_output_or_frame_us"] == pytest.approx(-0.8)
    assert times["C"]["first_output_or_frame_us"] == pytest.approx(-0.64)
    assert [
        e["start_us"] for e in data["timing"] if e["kind"] == "move"
    ] == pytest.approx([100, 410.24])
    assert [
        e["end_us"] for e in data["timing"] if e["kind"] == "settle"
    ] == pytest.approx([210.24, 520.48])
    card_programs = next(
        e.params["cards"]
        for e in compiled.points[0].physical_events
        if e.kind == "spectrum_program"
    )
    for trace in data["awg"]:
        item = card_programs[trace["card"]]
        program = item["program"]
        values = reconstruct_window(program, 0, program["physical_duration_samples"])
        column = item["contract"]["active_channels"].index(trace["channel"])
        np.testing.assert_array_equal(
            np.array(trace["normalized"]),
            values[trace["sample_indices"], column] / 32767,
        )
        assert len(trace["sample_indices"]) <= 204
        assert trace["reduced"]
    for tone in data["dds"]:
        assert tone["frequency_hz"][0] == tone["frequency_hz"][-1]
        assert min(tone["amplitude"]) > 0
        assert tone["time_us"][-1] == pytest.approx(620.48)
    assert data["wall_clock_time"] is False
    assert data["hardware_verified"] is False
    assert "not a measured shot gap" in data["hold_margin_semantics"]
    compiled.assert_integrity()


def test_dds_view_respects_same_frame_pin_delay_and_precommit_endpoint():
    spec = {
        "model": "M4i.6631-x8",
        "mode": "dds",
        "sample_rate_hz": 1_250_000_000,
        "active_channels": [0],
        "dds_routing": "16/5",
    }
    a = [{"core": 0, "channel": 0, "frequency_hz": 70e6, "amplitude": 0.1}]
    b = [{**a[0], "frequency_hz": 71e6}]
    moves = [
        {
            "start_mu": i * 8192,
            "motion_duration_mu": 8192,
            "knots": [
                {"offset_mu": 0, "tones": left},
                {"offset_mu": 8192, "tones": right},
            ],
        }
        for i, (left, right) in enumerate(((a, b), (b, a)))
    ]
    program = build_dds_program(spec, a, moves, 16384)
    frame = program["frames"][1]
    assert frame["ramp_update_delay_mu"] == 32
    rows = [row for row in _dds_rows(program, -0.64) if row["frame"] == 1]
    assert rows[1]["frequency_hz"] == rows[0]["frequency_hz"]
    assert rows[1]["time_us"] - rows[0]["time_us"] == pytest.approx(0.0064)
    assert rows[2]["frequency_hz"] == pytest.approx(
        rows[0]["frequency_hz"]
        + frame["tones"][0]["frequency_slope_hz_per_s"] * (8192 - 32) * 0.2e-9
    )


def test_export_shares_all_plot_axes_and_keeps_prediction_labels(compiled, tmp_path):
    result = export_clock_transport_view(compiled, tmp_path)
    assert all(
        result[name].is_file()
        for name in ("html", "png", "svg", "data", "timing_csv", "dds_csv")
    )
    payload = json.loads(result["data"].read_text(encoding="utf-8"))
    assert payload["execution_order"] == [[0, 0], [0, 1]]
    fig = result["figure"]
    assert all(
        getattr(fig.layout, f"xaxis{i}" if i > 1 else "xaxis").matches == "x6"
        for i in range(1, 6)
    )
    page = result["html"].read_text(encoding="utf-8")
    assert "not final RF/DAC samples" in page
    assert "shot / host gaps" not in payload["shot_gaps"]  # explicit unknown duration
    assert payload["shot_gaps"].startswith("unknown")
    compiled.assert_integrity()
