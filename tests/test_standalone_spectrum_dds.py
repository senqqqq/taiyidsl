"""Finite DDS20 commands for autonomous playback of ordinary Square pulses."""

import copy
import hashlib
import json
import math

import numpy as np
import pytest

from taiyidsl.backends.spectrum.dds import build_square_program, validate_square_program
from taiyidsl.ir import CompileError

CARD = {
    "model": "M4i.6631-x8",
    "mode": "dds",
    "active_channels": [0],
    "dds_routing": "16/5",
    "sample_rate_hz": 1_250_000_000,
}


def pulse(
    start, length, *, channel=0, frequency=10e6, phase=0, amplitude=0.2, **kwargs
):
    return dict(
        channel=channel,
        start_sample=start,
        duration_samples=length,
        frequency_hz=frequency,
        phase_rad=phase,
        amplitude=amplitude,
        source="scope_pair",
        **kwargs,
    )


def render_commands(program):
    """Model JUMP on all cores and the sine output after each timer commit."""
    result = np.zeros(
        (program["duration_samples"], len(program["card_spec"]["active_channels"]))
    )
    cursor = 0
    for frame in program["frames"]:
        assert frame["at_sample"] == cursor
        if frame["timer_cycles"] is None:
            assert all(tone["amplitude"] == 0 for tone in frame["tones"])
            break
        count = frame["timer_cycles"] * 8
        t = np.arange(count) / 1.25e9
        for column, tone in enumerate(frame["tones"]):
            phase = 2 * np.pi * t * tone["frequency_hz"] + np.deg2rad(tone["phase_deg"])
            result[cursor : cursor + count, column] = tone["amplitude"] * np.sin(phase)
        cursor += count
    assert cursor == len(result)
    return result


def test_shot_local_ramsey_phase_dark_time_padding_and_final_mute():
    frequency = 10e6
    starts = [400, 2800]
    pulses = [
        pulse(start, 800, phase=2 * math.pi * frequency * start / 1.25e9 + offset)
        for start, offset in zip(starts, (0, math.pi / 2), strict=True)
    ]
    program = build_square_program(CARD, pulses, 4000)
    assert validate_square_program(json.loads(json.dumps(program)))
    assert [frame["at_sample"] for frame in program["frames"]] == [
        0,
        400,
        1200,
        2800,
        3600,
        4000,
    ]
    assert program["command_count"] == 5 * 6 + 5
    expected = np.zeros(4000)
    for start, extra in zip(starts, (0, math.pi / 2), strict=True):
        t = np.arange(start, start + 800) / 1.25e9
        expected[start : start + 800] = 0.2 * np.cos(2 * np.pi * frequency * t + extra)
    # 12-bit phase, 16-bit amplitude and 32-bit frequency registers.
    np.testing.assert_allclose(render_commands(program)[:, 0], expected, atol=0.00018)


@pytest.mark.parametrize(("routing", "core"), [("16/5", 8), ("20/1", 20)])
def test_shared_commit_preserves_ongoing_channel_and_adjacent_pulses(routing, core):
    card = {**CARD, "active_channels": [0, 1], "dds_routing": routing}
    wave = pulse(0, 2400, frequency=7.234567e6, phase=0.17, amplitude=0.8)
    program = build_square_program(
        card,
        [
            wave,
            pulse(400, 800, channel=1, phase=1),
            pulse(1200, 400, channel=1, frequency=12.345678e6, phase=2),
        ],
        2400,
    )
    assert validate_square_program(program)
    assert program["frames"][1]["tones"][1]["core"] == core
    output = render_commands(program)
    time = np.arange(2400) / 1.25e9
    expected = wave["amplitude"] * np.cos(
        2 * np.pi * wave["frequency_hz"] * time + 0.17
    )
    np.testing.assert_allclose(output[:, 0], expected, atol=0.00064)
    assert output[400, 1] == pytest.approx(0.2 * math.cos(1), abs=0.00018)
    assert output[1200, 1] == pytest.approx(0.2 * math.cos(2), abs=0.00018)
    assert not output[1600:, 1].any()


def test_discrete_phase_update_matches_rf_in_same_gate():
    program = build_square_program(
        CARD,
        [
            pulse(
                0,
                1600,
                frequency=0,
                phase_steps=[{"sample": 800, "delta_rad": math.pi}],
            )
        ],
        1600,
    )
    assert validate_square_program(program)
    output = render_commands(program)[:, 0]
    np.testing.assert_allclose(output[:800], 0.2, atol=0.00002)
    np.testing.assert_allclose(output[800:], -0.2, atol=0.00002)


def test_timer_grid_and_single_queue_boundaries():
    assert validate_square_program(build_square_program(CARD, [pulse(0, 504)], 504))
    with pytest.raises(CompileError, match="6.4 ns.*scope_pair"):
        build_square_program(CARD, [pulse(0, 500)], 504)
    with pytest.raises(CompileError, match="6.4 ns.*shot end"):
        build_square_program(CARD, [], 500)
    with pytest.raises(CompileError, match="18.*command cycles"):
        build_square_program(CARD, [pulse(0, 17 * 8)], 17 * 8)
    assert validate_square_program(build_square_program(CARD, [], 18 * 8))
    with pytest.raises(CompileError, match="command cycles"):
        build_square_program(CARD, [], (2**32) * 8)
    with pytest.raises(CompileError, match="4096-command"):
        build_square_program(CARD, [pulse(i * 800, 800) for i in range(683)], 683 * 800)


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ({"envelope": {"kind": "flat_top_gaussian"}}, "Square.*scope_pair"),
        (
            {"phase_modulation": {"kind": "half_sine", "amplitude": 1}},
            "modulation.*scope_pair",
        ),
        ({"phase_steps": [{"sample": 500, "delta_rad": 0.5}]}, "6.4 ns.*scope_pair"),
    ],
)
def test_unsupported_requests_identify_the_pulse(extra, error):
    with pytest.raises(CompileError, match=error):
        build_square_program(CARD, [pulse(0, 800, **extra)], 800)


def test_rehashed_wrong_phase_or_terminal_tone_fails_independent_check():
    program = build_square_program(CARD, [pulse(0, 800, frequency=300e6)], 800)
    assert validate_square_program(program)
    for frame, field, value, error in (
        (0, "phase_deg", 0.0, "phase"),
        (0, "core", 1, "assignment"),
        (1, "amplitude", 0.125, "muted"),
    ):
        invalid = copy.deepcopy(program)
        invalid["frames"][frame]["tones"][0][field] = value
        del invalid["sha256"]
        encoded = json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode()
        invalid["sha256"] = hashlib.sha256(encoded).hexdigest()
        with pytest.raises(CompileError, match=error):
            validate_square_program(invalid)
