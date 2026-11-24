"""Check DAC output, interpolation and timing for padded sample assets."""

import math
from contextlib import nullcontext

import numpy as np
import pytest
from test_spectrum_backend import group_program
from test_spectrum_backend import lab as hybrid_lab
from test_standalone_spectrum_backend import card_lab, shot_program

from taiyidsl import (
    CompileError,
    Experiment,
    PolynomialPhase,
    Pulse,
    Qubit,
    Square,
    ns,
    play,
    us,
)
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.backends.artiq_spectrum.validation_awg import validate_program


@pytest.mark.parametrize("hybrid", [False, True])
def test_iq_active_window_local_modulation_and_carrier_across_pads(hybrid):
    # 1.6 ns source samples, but each pad need only lie on the 0.8 ns DAC grid.
    q = Qubit("q")
    source = np.array([1j, -0.5, 0.4 - 0.3j, -0.2j])
    frequency = 31.25e6
    padded = Pulse.from_array(
        source,
        kind="iq",
        sample_interval=1.6 * ns,
        resample="linear",
        pad_before=2.4 * ns,
        pad_after=0.8 * ns,
        frequency=frequency,
        phase=0.3,
        amplitude=0.6,
        phase_modulation=PolynomialPhase((0.13, 0.5)),
        phase_modulation_mode="carry",
    )
    follow = Square(3.2 * ns, frequency=frequency, amplitude=0.6)
    exp = Experiment("padded_iq", [q])
    with exp.average(1), exp.realtime():
        with exp.triggered_sequence(group="g") if hybrid else nullcontext():
            play(q.rf, padded)
            play(q.rf, follow)
    compiled = (hybrid_lab() if hybrid else card_lab()).compile(exp)
    program = (
        group_program(compiled)["cards"]["A"]["program"]
        if hybrid
        else shot_program(compiled)
    )
    assert validate_program(program)["status"] == "passed"
    first, second = program["pulses"]
    assert (
        first["duration_samples"]
        == second["start_sample"] - first["start_sample"]
        == 12
    )
    assert (
        first["envelope"]["pad_before_samples"] == 3
        and first["envelope"]["pad_after_samples"] == 1
    )
    assert program["sample_assets"][padded.sample_digest]["count"] == len(source)
    signal = reconstruct_window(
        program, first["start_sample"], second["start_sample"] + 4
    )[:, 0]
    omega = 2 * np.pi * frequency * 0.8e-9
    interpolated = np.interp(np.arange(8) / 2, np.arange(4), source)
    expected = np.zeros(16)
    expected[3:11] = 0.6 * np.real(
        interpolated
        * np.exp(1j * (omega * np.arange(3, 11) + 0.3 + 0.13 + 0.5 * np.arange(8) / 8))
    )
    expected[12:] = 0.6 * np.cos(omega * np.arange(12, 16) + 0.63)
    np.testing.assert_array_equal(signal, np.rint(expected * 32767))
    assert first["phase_evolution"]["instantaneous_frequency_max_hz"] == pytest.approx(
        frequency + 0.5 / (2 * math.pi * 6.4e-9)
    )


@pytest.mark.parametrize(
    ("pad_before", "pad_after", "source_interval", "match"),
    [
        (0.4, 0.4, 0.8, "pad_before"),
        (0, 0.4, 0.8, "pad_after"),
        (0.4, 0, 0.4, "duration is off"),
    ],
)
def test_total_duration_cannot_hide_off_grid_source_or_padding(
    pad_before, pad_after, source_interval, match
):
    pulse = Pulse.from_array(
        [1.0],
        kind="waveform",
        sample_interval=source_interval * ns,
        resample="linear",
        pad_before=pad_before * ns,
        pad_after=pad_after * ns,
    )
    exp = Experiment("off_grid", [])
    with exp.average(1), exp.realtime(), exp.repeat(0):
        play("aux", pulse)
    with pytest.raises(CompileError, match=match):
        card_lab().compile(exp)


def test_multi_channel_long_zero_pads_keep_active_ranges_in_playback_order():
    long_pad = Pulse.from_array(
        [1.0, -1.0],
        sample_interval=0.8 * ns,
        kind="waveform",
        pad_before=800 * us,
        pad_after=800 * us,
    )
    early = Pulse.from_array(
        [0.75], sample_interval=0.8 * ns, kind="waveform", pad_before=8 * ns
    )
    q = Qubit("q")
    exp = Experiment("opposite_order", [q])
    with exp.average(1), exp.realtime(), exp.parallel():
        play(q.rf, long_pad)
        play("aux", early)
    program = shot_program(card_lab().compile(exp))
    assert program["logical_duration_samples"] == 2_000_002
    assert any(step["loops"] > 100 for step in program["steps"])
    assert validate_program(program)["checked_samples"] < 10_000
    first = reconstruct_window(program, 9, 12)
    np.testing.assert_array_equal(first, [[0, 0], [0, round(32767 * 0.75)], [0, 0]])
    second = reconstruct_window(program, 999_999, 1_000_003)
    np.testing.assert_array_equal(second, [[0, 0], [32767, 0], [-32767, 0], [0, 0]])
