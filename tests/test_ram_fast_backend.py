"""Fast resident replay preserves tracking phase and explicit timing budgets."""

import math
from dataclasses import replace

import pytest

from taiyidsl import CompileError, Device, Qubit
from taiyidsl.dsl import Experiment, FlatTopGaussian, Square, delay, play, reserve
from taiyidsl.lab import LabConfig, reference_lab
from taiyidsl.units import ns, us


def fast_lab(q, **overrides):
    lab = reference_lab([q])
    config = dict(
        nco_mode="legacy_tracking",
        ram_latency_mu=112,
        ram_rearm_duration_mu=4000,
        ram_phase_update_duration_mu=480,
    )
    config.update(overrides)
    lab.setup.devices["urukul0_ch0"] = replace(
        lab.setup.devices["urukul0_ch0"], **config
    )
    return lab


def sequence(calls, gap=592, **overrides):
    q = Qubit("q0")
    exp = Experiment("fast_replay", [q])
    pulse = FlatTopGaussian(0 * us, 0.5 * us, sample_interval=40 * ns)
    with exp.average(1):
        with exp.realtime():
            for index, (amplitude, phase, frequency) in enumerate(calls):
                if index:
                    delay(q.raman, gap * ns)
                play(
                    q.raman,
                    pulse,
                    amplitude=amplitude,
                    phase=phase,
                    frequency=frequency,
                )
    return fast_lab(q, **overrides).compile(exp)


def launches(compiled):
    return [e for e in compiled.points[0].physical_events if e.kind == "dds_ram_start"]


def test_xy_only_writes_phase_and_reuses_profile():
    compiled = sequence([(0.5, 0, 125e6), (0.5, math.pi / 2, 125e6)])
    first, second = launches(compiled)
    assert first.params["ram_replay_action"] == "first"
    assert second.params["ram_replay_action"] == "phase"
    assert second.params["ram_prepare_duration_mu"] == 480
    assert second.params["ram_min_gap_mu"] == 592
    assert first.params["ram_profile"] == second.params["ram_profile"] == 0
    assert (second.params["ram_pow"] - first.params["ram_pow"]) % 65536 == 16384
    phase = next(
        e for e in compiled.points[0].physical_events if e.kind == "dds_ram_phase"
    )
    assert phase.duration_mu == 480
    assert phase.end_mu == second.start_mu
    assert phase.params["ref_time_mu"] == compiled.points[0].origin_mu
    assert not any(
        e.kind == "dds_ram_rearm" for e in compiled.points[0].physical_events
    )


def test_identical_hardware_phase_retriggers_without_preparation():
    compiled = sequence([(0.5, 0, 125e6)] * 16, gap=112)
    events = launches(compiled)
    assert {e.params["ram_replay_action"] for e in events[1:]} == {"retrigger"}
    assert {e.params["ram_pow"] for e in events} == {0}
    assert {e.params["ram_min_gap_mu"] for e in events[1:]} == {112}
    assert not any(
        e.kind in {"dds_ram_phase", "dds_ram_rearm"}
        for e in compiled.points[0].physical_events
    )
    with pytest.raises(CompileError, match="at least 112 mu.*got 104"):
        sequence([(0.5, 0, 125e6)] * 2, gap=104)


def test_identical_logical_gates_can_require_tracking_pow_updates():
    compiled = sequence([(0.5, 0, 83e6)] * 3)
    events = launches(compiled)
    assert {e.params["pow"] for e in events} == {0}
    assert len({e.params["ram_pow"] for e in events}) == 3
    assert {e.params["ram_replay_action"] for e in events[1:]} == {"phase"}
    with pytest.raises(CompileError, match="at least 592 mu.*got 584"):
        sequence([(0.5, 0, 83e6)] * 2, gap=584)


def test_classification_uses_quantized_frequency_and_calibrated_launch_time():
    compiled = sequence(
        [(0.5, 0, 125e6), (0.5, 0, 125e6 + 0.01)],
        gap=112,
        latency_mu=32,
    )
    first, second = launches(compiled)
    assert first.params["ftw"] == second.params["ftw"]
    assert second.params["ram_replay_action"] == "retrigger"
    assert compiled.points[0].origin_mu == 144
    for event in (first, second):
        reference_launch = event.start_mu - event.params["ref_time_mu"]
        assert (
            event.params["ram_pow"]
            == (event.params["pow"] + ((reference_launch * event.params["ftw"]) >> 16))
            & 0xFFFF
        )


@pytest.mark.parametrize("second", [(0.25, 0, 125e6), (0.5, 0, 126e6)])
def test_amplitude_or_frequency_change_retains_full_rearm(second):
    compiled = sequence([(0.5, 0, 125e6), second], gap=4112)
    event = launches(compiled)[1]
    assert event.params["ram_replay_action"] == "rearm"
    assert event.params["ram_prepare_duration_mu"] == 4000
    assert event.params["ram_min_gap_mu"] == 4112
    with pytest.raises(CompileError, match="at least 4112 mu"):
        sequence([(0.5, 0, 125e6), second])


def test_omitted_fast_budget_preserves_full_rearm():
    compiled = sequence(
        [(0.5, 0, 125e6)] * 2, gap=4112, ram_phase_update_duration_mu=None
    )
    assert launches(compiled)[1].params["ram_replay_action"] == "rearm"


@pytest.mark.parametrize("budget", [8, 488])
def test_phase_prepare_window_reserves_kernel_submission(budget):
    q = Qubit("q0")
    exp = Experiment("reserved_phase", [q])
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse, frequency=125e6)
            with exp.parallel():
                reserve("kernel_submit", 8 * ns)
                with exp.serial():
                    delay(q.raman, (budget + 112) * ns)
                    play(q.raman, pulse, frequency=125e6, phase=math.pi / 2)
    with pytest.raises(CompileError, match="RAM phase overlaps.*reservation"):
        fast_lab(q, ram_phase_update_duration_mu=budget).compile(exp)


def test_phase_prepare_does_not_swallow_independent_output_edges():
    q = Qubit("q0")
    exp = Experiment("phase_edge", [q])
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(q.raman, pulse, frequency=125e6)
                    delay(q.raman, 592 * ns)
                    play(q.raman, pulse, frequency=125e6, phase=math.pi / 2)
                with exp.serial():
                    delay(q.readout, 4200 * ns)
                    play(q.readout, Square(8 * ns, amplitude=1, frequency=0))
    with pytest.raises(CompileError, match="edge overlaps.*RAM phase"):
        fast_lab(q).compile(exp)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "480"])
def test_phase_budget_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="ram_phase_update_duration_mu"):
        Device("dds0", "dds", ram_phase_update_duration_mu=value)


@pytest.mark.parametrize("value", [None, 480])
def test_phase_budget_config_roundtrip(value):
    lab = fast_lab(Qubit("q0"), ram_phase_update_duration_mu=value)
    assert LabConfig.from_dict(lab.to_dict()).to_dict() == lab.to_dict()


def test_fast_mode_requires_matched_bank_and_aligned_budget_even_for_single_gate():
    calls = [(0.5, 0, 125e6)]
    with pytest.raises(CompileError, match="ram_rearm_duration_mu"):
        sequence(calls, ram_rearm_duration_mu=None)
    with pytest.raises(CompileError, match="phase_update.*configuration grid"):
        sequence(calls, ram_phase_update_duration_mu=481)
    with pytest.raises(CompileError, match="matched-RAM"):
        sequence(calls, ram_latency_mu=64)
