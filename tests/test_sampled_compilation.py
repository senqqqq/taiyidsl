"""Lower content-addressed samples with the timing and controls of their route."""

import math
from contextlib import nullcontext
from dataclasses import replace

import numpy as np
import pytest
from test_spectrum_backend import group_program
from test_spectrum_backend import lab as hybrid_lab
from test_standalone_spectrum_backend import card_lab, shot_program

from taiyidsl import (
    CompileError,
    ControlBinding,
    Experiment,
    HalfSinePhase,
    Pulse,
    Qubit,
    SpectrumOutput,
    Square,
    delay,
    ns,
    play,
    reference_lab,
    shift_phase,
)
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window


@pytest.mark.parametrize("hybrid", [False, True])
def test_iq_carrier_and_raw_voltage_use_the_same_asset_per_repeated_play(hybrid):
    q = Qubit("q")
    iq_samples = np.array([0.5j, -0.75, 0.3 + 0.4j, 1j])
    iq = Pulse.from_array(
        iq_samples,
        kind="iq",
        sample_interval=0.8 * ns,
        amplitude=0.4,
        frequency=31.25e6,
        phase=0.21,
    )
    waveform_samples = np.array([0.5, -0.125, 0, 0.75])
    raw = Pulse.from_array(waveform_samples, kind="waveform", sample_interval=0.8 * ns)
    exp = Experiment("iq_and_raw", [q])
    with exp.average(1), exp.realtime():
        with exp.triggered_sequence(group="g") if hybrid else nullcontext():
            # Without explicit controls, raw bypasses the q.rf frequency and amplitude.
            play(q.rf, raw)
            shift_phase(q.frame, 0.19)
            with exp.repeat(2):
                play(q.rf, iq)
    lab = hybrid_lab() if hybrid else card_lab()
    lab.signal_map[q.rf] = ControlBinding("rf", amplitude_scale=0.75)
    compiled = lab.compile(exp)
    program = (
        group_program(compiled)["cards"]["A"]["program"]
        if hybrid
        else shot_program(compiled)
    )
    assert set(program["sample_assets"]) == {iq.sample_digest, raw.sample_digest}
    play_events = [e for e in compiled.points[0].logical_events if e.kind == "play"]
    assert (
        play_events[0].params["frequency"] == 0
        and play_events[0].params["amplitude"] == 1
    )
    assert [e.params["shape"] for e in play_events] == ["sampled"] * 3
    for event, pulse in zip(play_events, program["pulses"], strict=True):
        assert event.params["sampled"] == pulse["envelope"]
        assert "data" not in pulse["envelope"]
    first = program["pulses"][0]["start_sample"]
    samples = reconstruct_window(program, first, first + 12)[:, 0]
    np.testing.assert_array_equal(samples[:4], np.rint(32767 * 0.75 * waveform_samples))
    theta = 2 * np.pi * 31.25e6 * 0.8e-9 * np.arange(4, 12) + 0.4
    expected = 32767 * 0.3 * np.real(np.tile(iq_samples, 2) * np.exp(1j * theta))
    np.testing.assert_array_equal(samples[4:], np.rint(expected))


def test_raw_waveform_leaves_continuous_oscillator_running_at_previous_frequency():
    q = Qubit("q")
    lab = card_lab()
    config = lab.setup.standalone_spectrum
    lab.setup.standalone_spectrum = replace(
        config,
        outputs={**config.outputs, "rf": SpectrumOutput("A", 0, "continuous", 2e6)},
    )
    drive = Square(32 * ns, frequency=3e6, amplitude=0.5)
    wave = Pulse.from_array(np.ones(40), sample_interval=0.8 * ns, kind="waveform")
    exp = Experiment("continuous_reference", [q])
    with exp.average(1), exp.realtime():
        play(q.rf, drive)
        play(q.rf, wave)
        play(q.rf, drive)
    first, raw, last = shot_program(lab.compile(exp))["pulses"]
    assert first["phase_rad"] == raw["phase_rad"] == 0
    assert raw["phase_evolution"] == {"carrier_mode": "none"}
    assert last["phase_rad"] == pytest.approx(2 * math.pi * 3e6 * 64e-9)


@pytest.mark.parametrize("cause", ["frame", "midpulse", "binding"])
def test_raw_waveform_cannot_apply_phase_or_frequency_transforms(cause):
    q = Qubit("q")
    lab = card_lab()
    raw = Pulse.from_array(np.ones(20), kind="waveform", sample_interval=0.8 * ns)
    exp = Experiment("raw_phase", [q])
    if cause == "binding":
        lab.signal_map[q.rf] = ControlBinding("rf", frequency_offset=2e6)
    with exp.average(1), exp.realtime():
        if cause == "frame":
            shift_phase(q.frame, 0.3)
        if cause == "midpulse":
            with exp.parallel():
                play(q.rf, raw)
                with exp.serial():
                    delay(8 * ns)
                    shift_phase(q.frame, 0.3)
        else:
            play(q.rf, raw)
    with pytest.raises(CompileError, match="Raw waveform"):
        lab.compile(exp)


@pytest.mark.parametrize("target", ["artiq", "hybrid_native", "dds"])
def test_unsupported_routes_fail_even_inside_zero_repeats(target):
    q = Qubit("q")
    sample = Pulse.from_array(
        [0.1, 0.2],
        kind="envelope",
        sample_interval=0.8 * ns,
        phase_modulation=HalfSinePhase(0.25),
    )
    exp = Experiment("route", [q])
    with exp.average(1), exp.realtime(), exp.repeat(0):
        play("cooling" if target == "hybrid_native" else q.rf, sample)
    if target == "hybrid_native":
        lab = hybrid_lab(cooling=True)
    elif target == "dds":
        lab = card_lab("dds")
    else:
        lab = reference_lab([q])
    with pytest.raises(CompileError, match="Spectrum AWG"):
        lab.compile(exp)


@pytest.mark.parametrize(
    ("mode", "sample_count", "error"),
    [("strict", 4, "Sample interval"), ("linear", 3, "duration is off")],
)
def test_sampling_grid_and_total_duration_are_checked_without_rounding(
    mode, sample_count, error
):
    wave = Pulse.from_array(
        [1.0] * sample_count, sample_interval=1.2 * ns, kind="waveform", resample=mode
    )
    exp = Experiment("exact", [])
    with exp.average(1), exp.realtime():
        play("aux", wave)
    with pytest.raises(CompileError, match=error):
        card_lab().compile(exp)
