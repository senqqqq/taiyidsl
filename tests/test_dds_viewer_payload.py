"""DDS display curves preserve command timing without changing packed AWG data."""

import copy
import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from taiyidsl.dds_view import dds_card_views
from taiyidsl.output_simulator import _payload, simulate_output
from taiyidsl.spectrum_dds import build_dds_program
from taiyidsl.viewer_execution import execution_axis


def test_mixed_payload_adds_tones_without_changing_awg_bytes_or_times():
    source = Path(__file__).parents[1] / "examples/artiq_spectrum/transport_dds.py"
    lab, exp = runpy.run_path(str(source))["build_demo"]()
    compiled = lab.compile(exp)
    expected = simulate_output(compiled, start_s=105e-6, duration_s=1e-6)
    payload = _payload(compiled)
    point = payload["points"][0]
    assert {card["card"] for card in point["cards"]} == {"A", "B"}
    assert len(point["dds"]) == 1
    view = point["dds"][0]
    assert view["card"] == "C" and view["mode"] == "dds"
    assert "segments" not in view and "sample_rate_hz" not in view
    assert view["trigger_time_s"] == pytest.approx(-1.2e-6)
    assert view["frame0_time_s"] == pytest.approx(-0.64e-6)
    assert view["command_end_s"] == pytest.approx(620.48e-6)
    assert {(c["core"], c["channel"]) for c in view["curves"]} == {
        (0, 0),
        (1, 0),
        (8, 1),
    }
    for curve in view["curves"]:
        assert np.all(np.diff(curve["time_s"]) >= 0)
        assert min(curve["amplitude"]) > 0
        assert curve["frequency_hz"][0] == curve["frequency_hz"][-1]
        assert curve["stage"][0] == "initial_hold"
        assert curve["stage"][-1] == "terminal_hold"
    actual = simulate_output(compiled, start_s=105e-6, duration_s=1e-6)
    for before, after in zip(expected["traces"], actual["traces"], strict=True):
        np.testing.assert_array_equal(before["dac_codes"], after["dac_codes"])
        np.testing.assert_array_equal(before["time_s"], after["time_s"])
    compiled.assert_integrity()


def _dds_only(two_regions=False):
    spec = {
        "mode": "dds",
        "model": "M4i.6631-x8",
        "active_channels": [0],
        "sample_rate_hz": 1_250_000_000,
        "dds_routing": "16/5",
        "dds_trigger_latency_ns": 560,
        "dds_amplitude_slope_step_per_s": 1e-6,
    }
    home = [{"core": 0, "channel": 0, "frequency_hz": 70e6, "amplitude": 0.125}]
    target = [{**home[0], "frequency_hz": 71e6, "amplitude": 0.0625}]
    moves = [
        {
            "start_mu": i * 8192,
            "motion_duration_mu": 8192,
            "knots": [{"offset_mu": 0, "tones": a}, {"offset_mu": 8192, "tones": b}],
        }
        for i, (a, b) in enumerate(((home, target), (target, home)))
    ]
    program = build_dds_program(spec, home, moves, 16384)
    events = [
        SimpleNamespace(
            kind="spectrum_program",
            params={
                "name": "transport",
                "region_id": f"r{i}",
                "trigger_physical_mu": 20_000 + i * 10_000,
                "trigger_time_unit_s": 1e-9,
                "calibration": {"status": "unmeasured"},
                "cards": {"C": {"contract": spec, "program": program}},
            },
        )
        for i in range(2 if two_regions else 1)
    ]
    point = SimpleNamespace(
        index=0,
        origin_mu=50_000,
        duration_mu=200_000,
        coordinates={},
        logical_events=[],
        physical_events=events,
    )
    compiled = SimpleNamespace(
        name="DDS only",
        points=[point],
        metadata={},
        provenance={"backend": {"name": "artiq_spectrum"}},
        setup=SimpleNamespace(tick_seconds=0.2e-9),
        assert_integrity=lambda: None,
    )
    return compiled, program


def test_dds_only_keeps_pin_delay_amplitude_ramp_and_known_hold_bounds():
    compiled, program = _dds_only()
    snapshot = copy.deepcopy(program)
    payload = _payload(compiled)
    assert payload["packed"] == {} and payload["points"][0]["cards"] == []
    assert payload["unique_resident_bytes_upper_bound"] == 0
    view = payload["points"][0]["dds"][0]
    curve = view["curves"][0]
    assert view["frame0_time_s"] == pytest.approx(10.56e-6)
    assert curve["time_s"][0] == 0
    assert curve["time_s"][-1] == pytest.approx(40e-6)
    frame = program["frames"][1]
    assert frame["ramp_update_delay_mu"] == 32
    indices = [i for i, f in enumerate(curve["frame"]) if f == 1]
    start, ramp, end = indices
    assert curve["frequency_hz"][ramp] == curve["frequency_hz"][start]
    assert curve["amplitude"][ramp] == curve["amplitude"][start]
    assert curve["time_s"][ramp] - curve["time_s"][start] == pytest.approx(6.4e-9)
    seconds = (frame["timer_mu"] - 32) * 0.2e-9
    assert curve["amplitude"][end] == pytest.approx(
        frame["tones"][0]["amplitude"]
        + seconds * frame["tones"][0]["amplitude_slope_per_s"]
    )
    axis = execution_axis(compiled)["points"][0]
    assert axis["awg"]["start_s"] == pytest.approx(view["command_start_s"])
    assert axis["awg"]["end_s"] == pytest.approx(view["command_end_s"])
    assert program == snapshot


def test_multiple_dds_regions_partition_hold_without_duplicate_gap_ramps():
    compiled, _ = _dds_only(two_regions=True)
    first, second = list(dds_card_views(compiled, compiled.points[0]))
    assert first["hold_end_s"] == second["hold_start_s"] == second["command_start_s"]
    assert first["hold_end_s"] > first["command_end_s"]
    assert second["hold_end_s"] == pytest.approx(40e-6)
    assert (
        first["curves"][0]["frequency_hz"][-1] == second["curves"][0]["frequency_hz"][0]
    )
