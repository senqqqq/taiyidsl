"""End-to-end region ownership, RF phase, native ARTIQ isolation and export."""

import json
import math
from dataclasses import replace

import numpy as np
import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    HalfSinePhase,
    LabConfig,
    PolynomialPhase,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    SweepParameter,
    delay,
    ns,
    play,
    reset_frame,
    rz,
    shift_phase,
    trigplay,
    us,
)
from taiyidsl.spectrum_export import artiq_subplan, spectrum_manifest
from taiyidsl.spectrum_runtime import calibration_fingerprint
from taiyidsl.spectrum_waveforms import reconstruct_window, validate_program


def lab(*, cooling=False, memory_bytes=4 * 1024**3):
    devices = {
        "awg_trigger": Device("awg_trigger", "ttl", grid_mu=1),
        "readout": Device("readout", "ttl", grid_mu=1),
        **{name: Device(name, "spectrum") for name in ("rf", "clock", "rydberg")},
    }
    mapping = SignalMap(
        {"q.rf": "rf", "clock": "clock", "rydberg": "rydberg", "camera": "readout"}
    )
    if cooling:
        devices["cooling"] = Device(
            "cooling", "dds", bus="spi0", update_group="urukul0", urukul_proto_rev=9
        )
        mapping["cooling"] = "cooling"
    cards = {
        "A": SpectrumCard("SIM_A", (0,), memory_bytes=memory_bytes),
        "B": SpectrumCard("SIM_B", (0, 1), memory_bytes=memory_bytes),
    }
    group = SpectrumGroup(
        "awg_trigger",
        cards,
        {
            "rf": SpectrumOutput("A", 0),
            "clock": SpectrumOutput("B", 0),
            "rydberg": SpectrumOutput("B", 1),
        },
    )
    config = SpectrumConfig({"g": group}, {"clock": None, "rydberg": None})
    return LabConfig(DeviceSetup(devices, spectrum=config), mapping, "artiq_spectrum")


def make_ramsey(*, wait=100 * us, phase=math.pi / 2, cooling=False):
    q = Qubit("q")
    exp = Experiment("hybrid_ramsey", [q])
    pulse = Square(200 * us, amplitude=0.5, frequency=30_000)
    with exp.average(2), exp.realtime():
        if cooling:
            play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, pulse)
            delay(wait)
            rz(q, phase)
            trigplay(q.rf, pulse)
            rz(q, 0.3)
        play("camera", Square(8 * ns))
    return exp


def group_program(compiled):
    return next(
        e for e in compiled.points[0].physical_events if e.kind == "spectrum_program"
    ).params


def test_full_region_has_one_trigger_and_native_backend_sees_no_gates():
    compiled = lab(cooling=True).compile(make_ramsey(cooling=True))
    assert compiled.setup.tick_seconds == 0.2e-9
    native = artiq_subplan(compiled)
    assert native.setup.tick_seconds == 1e-9
    assert native.setup.devices["cooling"].config_duration_mu == 3000
    point = compiled.points[0]
    triggers = [e for e in point.physical_events if e.signal == "awg_trigger"]
    assert [e.kind for e in triggers] == ["ttl_on", "ttl_off"]
    assert not any(
        e.signal in {"rf", "clock", "rydberg"} for e in native.points[0].physical_events
    )
    assert not any(
        e.kind in {"rz", "scope_delay", "spectrum_region"}
        for e in native.points[0].logical_events
    )
    assert compiled.virtual_z_frames()["q"] == pytest.approx(math.pi / 2 + 0.3)
    assert len([e for e in point.logical_events if e.params.get("region_id")]) >= 6


def test_ramsey_default_phase_starts_at_region_and_includes_virtual_z():
    compiled = lab(cooling=True).compile(make_ramsey(cooling=True))
    program = group_program(compiled)["cards"]["A"]["program"]
    first, second = program["pulses"]
    assert first["phase_rad"] == pytest.approx(0)
    expected = (2 * math.pi * 30_000 * 300e-6 - math.pi / 2) % (2 * math.pi)
    assert second["phase_rad"] == pytest.approx(expected)
    start = second["start_sample"]
    samples = reconstruct_window(program, start, start + 64)[:, 0]
    target = np.rint(
        32767 * 0.5 * np.cos(expected + 2 * math.pi * 30_000 * np.arange(64) / 1.25e9)
    ).astype(np.int16)
    np.testing.assert_array_equal(samples, target)


def test_long_wait_is_not_a_wall_clock_array():
    compiled = lab().compile(make_ramsey(wait=0.1))
    program = group_program(compiled)["cards"]["A"]["program"]
    assert program["physical_duration_samples"] > 125_000_000
    assert program["memory"]["resident_bytes"] < 2_000_000
    assert any(step["loops"] > 100 for step in program["steps"])
    assert validate_program(program)


def test_scan_points_and_profile_roundtrip():
    profile = lab()
    restored = LabConfig.from_dict(json.loads(json.dumps(profile.to_dict())))
    assert restored.to_dict() == json.loads(json.dumps(profile.to_dict()))
    q = Qubit("q")
    exp = Experiment("scan", [q])
    wait = SweepParameter("wait", (100, 101), "us")
    with exp.average(2, mode="cyclic"), exp.sweep(wait), exp.realtime():
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(200 * us, frequency=30_000))
            delay(wait)
            trigplay(q.rf, Square(200 * us, frequency=30_000))
    compiled = restored.compile(exp)
    assert list(compiled.execution_order()) == [(0, 0), (1, 0), (0, 1), (1, 1)]
    phases = [
        next(e for e in p.physical_events if e.kind == "spectrum_program").params[
            "cards"
        ]["A"]["program"]["pulses"][1]["phase_rad"]
        for p in compiled.points
    ]
    assert (phases[1] - phases[0]) % (2 * math.pi) == pytest.approx(math.radians(10.8))


def test_parallel_clock_and_rydberg_modulation_are_one_card_timeline():
    q = Qubit("q")
    exp = Experiment("two_gate", [q])
    clock = FlatTopGaussian(
        180 * us,
        15 * us,
        amplitude=0.4,
        frequency=150e6,
        sample_interval=0.8 * ns,
        phase_modulation=HalfSinePhase(math.pi),
    )
    rydberg = FlatTopGaussian(
        240 * ns,
        20 * ns,
        amplitude=0.3,
        frequency=200e6,
        sample_interval=0.8 * ns,
        phase_modulation=PolynomialPhase((0, 0, 3 * math.pi, -2 * math.pi)),
    )
    with (
        exp.average(1),
        exp.realtime(),
        exp.triggered_sequence(group="g"),
        exp.parallel(),
    ):
        trigplay("clock", clock)
        with exp.serial():
            delay(100 * us)
            trigplay("rydberg", rydberg)
    compiled = lab().compile(exp)
    program = group_program(compiled)["cards"]["B"]["program"]
    assert [p["duration_samples"] for p in program["pulses"]] == [375000, 500]
    start = program["pulses"][1]["start_sample"]
    samples = reconstruct_window(program, start, start + 500)
    assert np.any(samples[:, 0]) and np.any(samples[:, 1])
    assert validate_program(program)


def test_export_is_offline_and_retains_one_public_trigger(tmp_path):
    compiled = lab(cooling=True).compile(make_ramsey(cooling=True))
    result = compiled.export(tmp_path)
    manifest = json.loads(result["manifest"].read_text())
    assert manifest["verification"]["offline"] == "passed"
    assert manifest["verification"]["hardware"] == "not_verified"
    source = (tmp_path / "artiq" / "experiment.py").read_text()
    assert "point_0" in source
    assert "spectrum_runner" not in source
    assert "spectrum_runner.run_point" in result["source"].read_text()
    report = json.loads(result["report"].read_text())
    assert report["points"][0]["trigger_pulses"] == 1


def test_mixed_profile_without_spectrum_work_skips_programs(tmp_path):
    q = Qubit("q")
    exp = Experiment("camera_only", [q])
    with exp.average(1), exp.realtime():
        play("camera", Square(8 * ns))
    compiled = lab().compile(exp)
    assert spectrum_manifest(compiled)["points"][0]["groups"] == []
    assert compiled.export(tmp_path)["verification"]["offline"] == "passed"


def test_missing_profile_and_low_memory_fail():
    profile = lab()
    profile.setup.spectrum = None
    with pytest.raises(CompileError, match="SpectrumConfig"):
        profile.compile(make_ramsey())
    with pytest.raises(CompileError, match="memory|slot|capacity"):
        lab(memory_bytes=1024).compile(make_ramsey())


def test_strict_grid_and_unmeasured_latency_are_not_silently_changed():
    profile = lab()
    group = profile.setup.spectrum.groups["g"]
    changed = replace(group, trigger_lead_mu=513)
    profile.setup.spectrum = SpectrumConfig(
        {"g": changed}, {"clock": None, "rydberg": None}
    )
    with pytest.raises(CompileError, match="timing grid"):
        profile.compile(make_ramsey())


def test_active_hardware_frame_steps_do_not_change_envelope():
    q = Qubit("q")
    exp = Experiment("active_h", [q])
    with (
        exp.average(1),
        exp.realtime(),
        exp.triggered_sequence(group="g"),
        exp.parallel(),
    ):
        trigplay(q.rf, Square(800 * ns, frequency=30_000, amplitude=0.4))
        with exp.serial():
            delay(400 * ns)
            shift_phase(q.frame, 0.3)
            delay(200 * ns)
            reset_frame(q.frame)
    program = group_program(lab().compile(exp))["cards"]["A"]["program"]
    assert program["pulses"][0]["phase_steps"] == [
        {"sample": 500, "delta_rad": 0.3},
        {"sample": 750, "delta_rad": -0.3},
    ]
    assert validate_program(program)


def test_measured_frequency_calibration_is_applied_once_and_bound_to_samples():
    profile = lab()
    group = profile.setup.spectrum.groups["g"]
    calibration = {
        "status": "measured",
        "revision": "synthetic-test-not-hardware",
        "channels": {
            "A:0": {
                "sample_rate_hz": 1_250_000_000,
                "delay_ns": 1.2,
                "sample_shift_samples": -2,
                "phase_reference": "after_sample_shift",
                "timing_uncertainty_ns": 0.01,
                "frequency_points": [
                    {
                        "carrier_frequency_hz": 30_000,
                        "gain": 2.0,
                        "phase_offset_rad": 0.4,
                        "phase_uncertainty_deg": 0.1,
                    }
                ],
            }
        },
    }
    profile.setup.spectrum = replace(
        profile.setup.spectrum,
        groups={"g": replace(group, calibration=calibration)},
    )
    program = group_program(profile.compile(make_ramsey()))["cards"]["A"]["program"]
    assert program["pulses"][0]["phase_rad"] == pytest.approx(0.4)
    assert program["pulses"][0]["amplitude"] == 0.25
    assert program["pulses"][0]["start_sample"] == 138
    assert program["calibration_sha256"] == calibration_fingerprint(calibration)
    calibration["channels"]["A:0"]["frequency_points"][0]["carrier_frequency_hz"] = (
        31_000
    )
    profile.setup.spectrum = replace(
        profile.setup.spectrum,
        groups={"g": replace(group, calibration=calibration)},
    )
    with pytest.raises(CompileError, match="carrier frequency"):
        profile.compile(make_ramsey())


@pytest.mark.parametrize("revision", [8, 9])
def test_hybrid_preserves_three_native_dds_paths(tmp_path, revision):
    from test_mixed_polar_backend import envelope, mixed_lab, square

    qubits, profile = mixed_lab(revision=revision)
    hybrid = lab()
    profile.setup.devices.update(hybrid.setup.devices)
    profile.setup.spectrum = hybrid.setup.spectrum
    profile.signal_map.update(hybrid.signal_map)
    profile.backend = "artiq_spectrum"
    q = Qubit("q")
    exp = Experiment("hybrid_three_native_paths", [*qubits, q])
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial(), exp.repeat(2):
            play(qubits[0].raman, envelope())
        with exp.serial(), exp.repeat(2):
            play(qubits[1].raman, square())
        with exp.serial(), exp.repeat(2):
            play(qubits[2].raman, envelope())
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(1600 * ns, frequency=30_000, amplitude=0.4))
    compiled = profile.compile(exp)
    native = artiq_subplan(compiled)
    kinds = {event.kind for event in native.points[0].physical_events}
    assert {"dds_polar_select", "dds_ram_start", "dds_nco_epoch"} <= kinds
    assert compiled.export(tmp_path)["verification"]["offline"] == "passed"
