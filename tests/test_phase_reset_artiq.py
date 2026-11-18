"""Unified DDS frames and explicit carrier-reference resets, entirely offline."""

import math
from dataclasses import replace

import pytest

from taiyidsl import (
    CompileError,
    ControlBinding,
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    delay,
    ns,
    play,
    reset_frame,
    reset_phase,
    rz,
    shift_phase,
)
from taiyidsl.artiq import verify_equivalence
from taiyidsl.hardware import DeviceCalibration
from taiyidsl.polar_validation import verify_polar_output


def _lab(shape, mode="continuous", *, calibrated=False):
    q = Qubit("q0")
    settings = dict(
        nco_mode=mode,
        nco_epoch_latency_mu=80,
        square_latency_mu=80,
        ram_latency_mu=112,
        ram_pow_latency_mu=112,
        ram_phase_latency_mu=112,
        ram_phase_update_duration_mu=480,
        ram_rearm_duration_mu=4000,
        latency_mu=16 if calibrated else 0,
    )
    if shape == "polar":
        settings.update(
            ram_destination="powasf",
            ram_polar_phase_latency_mu=100,
            ram_profile_latency_mu=112,
            ram_profile_phase_latency_mu=100,
            ram_profile_control="urukul_rev8",
        )
    device = Device("dds", "dds", bus="spi", update_group="board", **settings)
    setup = DeviceSetup({"dds": device})
    binding = ControlBinding("dds")
    if calibrated:
        setup.calibration["dds"] = DeviceCalibration(latency_mu=8, phase_offset=0.11)
        binding = ControlBinding("dds", phase_scale=2, phase_offset=0.7)
    return q, LabConfig(setup, SignalMap({q.raman: binding}))


def _pulse(shape, duration=800):
    if shape == "square":
        return Square(duration * ns, frequency=83.123456e6, amplitude=0.5)
    return FlatTopGaussian(
        0 * ns,
        sigma=duration / 10 * ns,
        cutoff_sigma=5,
        sample_interval=4 * ns,
        frequency=83.123456e6,
        amplitude=0.5,
    )


def _effects(compiled, shape):
    kind = {
        "square": "dds_effect",
        "ram": "dds_ram_effect",
        "polar": "dds_polar_effect",
    }[shape]
    return [e for e in compiled.points[0].physical_events if e.kind == kind]


def _actual_phase(event, when, shape, continuous):
    p = event.params
    word = p["square_pow" if shape == "square" else "ram_pow"]
    epoch = (
        p["nco_epoch_effect_mu"]
        if continuous
        else p.get("carrier_effect_mu", event.start_mu)
    )
    return ((when - epoch) * p["ftw"] + (word << 16)) % 2**32


def _assert_phase(actual, phase):
    desired = round(phase / (2 * math.pi) * 65536) << 16
    error = (actual - desired + 2**31) % 2**32 - 2**31
    assert abs(error) < 65536


@pytest.mark.parametrize(
    "shape,mode",
    [
        ("square", "continuous"),
        ("ram", "continuous"),
        ("polar", "continuous"),
        ("square", "legacy_tracking"),
        ("ram", "legacy_tracking"),
    ],
)
def test_reset_phase_references_its_calibrated_boundary_and_keeps_phase_calibration(
    shape, mode
):
    q, lab = _lab(shape, mode, calibrated=True)
    exp = Experiment("reference_at_calibrated_boundary", [q])
    with exp.average(1), exp.realtime():
        shift_phase(q.frame, 0.4)
        rz(q, 0.1)
        play(q.raman, _pulse(shape), phase=0.2)
        delay(q.raman, 4000 * ns)
        reset_phase(q.frame)
        play(q.raman, _pulse(shape), phase=0.2)
        delay(q.raman, 4000 * ns)
        play(q.raman, _pulse(shape), phase=0.2)
    compiled = lab.compile(exp)
    point = compiled.points[0]
    effects = _effects(compiled, shape)
    reset = next(e for e in effects if e.params.get("command_kind") == "reset_phase")
    assert reset.params["carrier_reference_mu"] == 4800
    assert reset.params["carrier_reference_physical_mu"] == 4776
    assert reset.start_mu - point.origin_mu == 4776
    assert reset.params["phase"] == pytest.approx(1.21)
    assert (
        "carrier_reference_physical_mu"
        not in next(e for e in point.logical_events if e.kind == "reset_phase").params
    )
    _assert_phase(
        _actual_phase(reset, reset.start_mu, shape, mode == "continuous"), 1.21
    )
    assert len([e for e in point.physical_events if e.kind == "dds_nco_epoch"]) == (
        mode == "continuous"
    )
    assert compiled.frame_phases() == {"q0": 0}
    assert compiled.virtual_z_frames() == {"q0": 0}
    assert verify_equivalence(compiled)["status"] == "passed"
    if shape == "polar":
        assert verify_polar_output(compiled)["status"] == "passed"


@pytest.mark.parametrize("shape", ["square", "ram", "polar"])
def test_reset_frame_clears_rz_but_does_not_restart_the_carrier(shape):
    q, lab = _lab(shape)
    exp = Experiment("frame_reset_keeps_carrier", [q])
    with exp.average(1), exp.realtime():
        rz(q, 0.4)
        play(q.raman, _pulse(shape))
        delay(q.raman, 4000 * ns)
        reset_frame(q.frame)
        play(q.raman, _pulse(shape))
    compiled = lab.compile(exp)
    event = _effects(compiled, shape)[-1]
    assert "carrier_reference_mu" not in event.params
    expected = (4800 * event.params["ftw"]) % 2**32
    actual = _actual_phase(event, event.start_mu, shape, True)
    error = (actual - expected + 2**31) % 2**32 - 2**31
    assert abs(error) < 65536
    assert abs((actual + 2**31) % 2**32 - 2**31) > 65536
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("shape", ["square", "ram", "polar"])
def test_terminal_reset_phase_has_a_real_update_without_a_later_play(shape):
    q, lab = _lab(shape)
    exp = Experiment("terminal_carrier_reset", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, _pulse(shape), phase=0.2)
        delay(q.raman, 4000 * ns)
        reset_phase(q.frame)
    compiled = lab.compile(exp)
    event = _effects(compiled, shape)[-1]
    assert event.params["command_kind"] == "reset_phase"
    _assert_phase(_actual_phase(event, event.start_mu, shape, True), 0.2)
    assert verify_equivalence(compiled)["status"] == "passed"
    if shape == "polar":
        assert verify_polar_output(compiled)["status"] == "passed"


@pytest.mark.parametrize("shape", ["square", "ram", "polar"])
def test_reset_phase_preserves_short_gate_hardware_limits(shape):
    q, lab = _lab(shape)
    exp = Experiment("reset_has_no_bandwidth_exemption", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, _pulse(shape, 200))
        reset_phase(q.frame)
        play(q.raman, _pulse(shape, 200))
    with pytest.raises(
        CompileError, match="(?i)(preparation|window|throughput|select|fit)"
    ):
        lab.compile(exp)


def test_reset_phase_rejects_an_unknown_legacy_frame_path():
    q, lab = _lab("square", "legacy_tracking")
    lab.setup.devices["dds"] = replace(lab.setup.devices["dds"], square_latency_mu=None)
    exp = Experiment("no_silent_reset", [q])
    with exp.average(1), exp.realtime():
        reset_phase(q.frame)
        play(q.raman, _pulse("square"))
    with pytest.raises(CompileError, match="Hardware frame updates require"):
        lab.compile(exp)


@pytest.mark.parametrize("shape", ["square", "ram", "polar"])
def test_a_future_reset_reference_does_not_leak_into_an_earlier_held_state(shape):
    q, lab = _lab(shape)
    exp = Experiment("reference_is_not_retroactive", [q])
    with exp.average(1), exp.realtime():
        shift_phase(q.frame, 0.3)
        delay(q.raman, 4000 * ns)
        reset_phase(q.frame)
        play(q.raman, _pulse(shape), phase=0.2)
    compiled = lab.compile(exp)
    first, last = _effects(compiled, shape)
    assert "carrier_reference_mu" not in first.params
    assert last.params["carrier_reference_mu"] == 4000
    _assert_phase(_actual_phase(first, first.start_mu, shape, True), 0.3)
    _assert_phase(_actual_phase(last, last.start_mu, shape, True), 0.2)
    assert verify_equivalence(compiled)["status"] == "passed"
    if shape == "polar":
        result = verify_polar_output(compiled)
        summary = result["points"][0]["channels"][0]
        assert summary["carrier_reference_resets"] == [4000]
        assert "constant_nco_quantization_residual_u32" not in summary


def test_shared_dds_cannot_keep_independent_qubit_reset_references():
    q, lab = _lab("square")
    q1 = Qubit("q1")
    lab.signal_map[q1.raman] = "dds"
    exp = Experiment("shared_carrier_has_one_reference", [q, q1])
    with exp.average(1), exp.realtime():
        play(q.raman, _pulse("square"))
        delay(q.raman, 4000 * ns)
        reset_phase(q.frame)
        delay(q.raman, 4000 * ns)
        play(q1.raman, _pulse("square"))
    with pytest.raises(CompileError, match="unambiguous .*DDS ownership"):
        lab.compile(exp)


def test_reset_phase_cannot_silently_change_a_non_drive_alias():
    q, lab = _lab("square")
    lab.signal_map["q0.illumination"] = "dds"
    exp = Experiment("shared_non_drive_alias", [q])
    with exp.average(1), exp.realtime():
        play("q0.illumination", _pulse("square"))
        delay(q.raman, 4000 * ns)
        reset_phase(q.frame)
    with pytest.raises(CompileError, match="non-drive alias"):
        lab.compile(exp)
