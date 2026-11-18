"""Offline example coverage, including end-to-end export without the SDK."""

import json
import math
import runpy
import sys
from pathlib import Path

import pytest

from taiyidsl.spectrum_waveforms import reconstruct_window, validate_program

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "spectrum_hybrid.py"


@pytest.fixture(scope="module")
def example():
    return runpy.run_path(str(EXAMPLE))


def programs(compiled, point=0):
    return next(
        e
        for e in compiled.points[point].physical_events
        if e.kind == "spectrum_program"
    ).params["cards"]


def test_three_real_rf_channels_and_native_preparation(example):
    compiled = example["build_three_channel"]()
    cards = programs(compiled)
    assert cards["A"]["contract"]["serial_number"] == "SIM_NUCLEAR"
    assert cards["B"]["contract"]["serial_number"] == "SIM_AOM"
    assert cards["A"]["program"]["card_spec"]["active_channels"] == [0]
    assert cards["B"]["program"]["card_spec"]["active_channels"] == [0, 1]
    for entry in cards.values():
        assert validate_program(entry["program"])["status"] == "passed"
    (nuclear,) = cards["A"]["program"]["pulses"]
    clock, rydberg = cards["B"]["program"]["pulses"]
    assert [
        nuclear["duration_samples"],
        clock["duration_samples"],
        rydberg["duration_samples"],
    ] == [250000, 375000, 500]
    assert [p["frequency_hz"] for p in (nuclear, clock, rydberg)] == [
        30000,
        150e6,
        200e6,
    ]
    assert clock["phase_modulation"]["kind"] == "half_sine"
    assert rydberg["phase_modulation"]["kind"] == "polynomial"
    assert nuclear["envelope"]["sigma_samples"] == 12500
    assert clock["envelope"]["sigma_samples"] == 18750
    assert rydberg["envelope"]["sigma_samples"] == 25
    events = compiled.points[0].physical_events
    assert any(e.signal == "cooling_dds" and e.kind == "dds_on" for e in events)
    assert any(e.signal == "readout_ttl" and e.kind == "ttl_on" for e in events)
    edges = {
        name: [(e.kind, e.start_mu) for e in events if e.signal == name]
        for name in ("awg_trigger_a", "awg_trigger_b")
    }
    assert edges["awg_trigger_a"] == edges["awg_trigger_b"]
    assert [kind for kind, _ in edges["awg_trigger_a"]] == ["ttl_on", "ttl_off"]


def test_scan_and_terminal_virtual_z_examples(example):
    rabi = example["build_rabi"](amplitudes=(0.1, 0.3, 0.5))
    assert [
        programs(rabi, i)["A"]["program"]["pulses"][0]["amplitude"] for i in range(3)
    ] == [0.1, 0.3, 0.5]
    ramsey = example["build_ramsey"](delays_ms=(1, 100))
    program = programs(ramsey, 1)["A"]["program"]
    first, second = program["pulses"]
    assert (
        second["start_sample"] - first["start_sample"] - first["duration_samples"]
        == 125000000
    )
    assert program["memory"]["resident_bytes"] < 3000000
    assert validate_program(program)["checked_samples"] < 1000000
    vz = example["build_virtual_z"]()
    assert vz.virtual_z_frames()["q"] == pytest.approx(math.pi / 3)
    assert vz.frame_phases()["q"] == pytest.approx(-math.pi / 3)


@pytest.mark.parametrize("count", [100, 500])
def test_random_phase_stress_is_gapless_and_explicitly_not_rb(example, count):
    compiled = example["build_stress"](count=count)
    assert compiled.metadata["statistical_rb"] is False
    assert compiled.metadata["inversion_gate"] is False
    program = programs(compiled)["B"]["program"]
    pulses = program["pulses"]
    assert len(pulses) == count
    assert all(p["duration_samples"] == 500 for p in pulses)
    assert all(
        b["start_sample"] == a["start_sample"] + 500 for a, b in zip(pulses, pulses[1:])
    )
    assert len({round(p["phase_rad"], 8) for p in pulses}) == 4
    assert validate_program(program)["status"] == "passed"
    assert max(s["sample_count"] for s in program["segments"]) <= 4000
    sample = reconstruct_window(
        program, pulses[0]["start_sample"], pulses[0]["start_sample"] + 1
    )
    assert sample.shape == (1, 2)


def test_long_nuclear_capacity_is_a_bound_not_reported_as_validation(example):
    small, large = [example["nuclear_capacity_prediction"](n) for n in (100, 500)]
    assert small["status"] == "analytic_upper_bound_not_a_compiled_program"
    assert small["A"]["equal_partition_required_bytes_upper_bound"] == 64000000
    assert large["A"]["equal_partition_required_bytes_upper_bound"] == 256000000
    assert large["A"]["fits_memory_bound"] is True
    assert large["A"]["fits_step_bound"] is True


def test_optional_nuclear_stress_uses_long_gates_on_single_channel_card(example):
    compiled = example["build_stress"](count=3, nuclear=True)
    cards = programs(compiled)
    pulses = cards["A"]["program"]["pulses"]
    assert len(pulses) == 3
    assert all(p["duration_samples"] == 250000 for p in pulses)
    assert all(p["frequency_hz"] == 30000 for p in pulses)
    assert cards["B"]["program"]["pulses"] == []
    assert validate_program(cards["A"]["program"])["status"] == "passed"


def test_selected_examples_export_without_loading_spcm(example, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "spcm", None)
    report = example["main"](
        ["--output", str(tmp_path), "--case", "three_channel", "--case", "rb_like_100"]
    )
    assert report["hardware"] == "not_verified"
    assert report["artiq_compile"] == "not_run_by_this_example"
    assert len(report["cases"]) == 2
    assert json.loads((tmp_path / "summary.json").read_text())["offline_only"] is True
    for entry in report["cases"]:
        assert entry["verification"]["offline"] == "passed"
        assert entry["verification"]["hardware"] == "not_verified"
        assert all(p["trigger_pulses"] == 2 for p in entry["points"])
        manifest = json.loads(Path(entry["manifest"]).read_text())
        assert manifest["verification"]["spectrum_driver"] == "not_verified"
