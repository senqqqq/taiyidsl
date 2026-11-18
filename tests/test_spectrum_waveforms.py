import copy
import hashlib
import json
import math

import numpy as np
import pytest

from taiyidsl.ir import CompileError
from taiyidsl.spectrum_waveforms import (
    build_card_program,
    iter_program_samples,
    materialize_segment,
    reconstruct_window,
    validate_program,
)


def card(**kwargs):
    return dict(active_channels=[0, 1], **kwargs)


def pulse(**kwargs):
    result = dict(
        channel=0,
        start_sample=0,
        duration_samples=500,
        frequency_hz=125e6,
        amplitude=0.7,
        phase_rad=0.25,
        envelope={"kind": "square"},
    )
    result.update(kwargs)
    return result


def test_full_carrier_phase_and_interleaved_bytes_are_not_envelope_samples():
    p = pulse(phase_rad=math.pi / 2)
    program = build_card_program(card(), [p], 500)
    assert validate_program(program)["status"] == "passed"
    data = reconstruct_window(program, 0, 500)
    expected = np.rint(
        0.7
        * 32767
        * np.cos(math.pi / 2 + 2 * math.pi * 125e6 * np.arange(500) / 1.25e9)
    )
    np.testing.assert_allclose(data[:, 0], expected, atol=1)
    assert np.all(data[:, 1] == 0)
    assert data.dtype == np.dtype("<i2")
    np.testing.assert_array_equal(np.frombuffer(data.tobytes(), dtype="<i2")[1::2], 0)
    assert len(json.dumps(program)) < 10000


def test_resolved_phase_prefix_and_virtual_z_are_not_applied_twice():
    bare = pulse(phase_rad=-0.73)
    shifted = dict(bare, start_sample=173, epoch_start_sample=999999)
    a = build_card_program(card(), [bare], 500)
    b = build_card_program(card(), [shifted], 673)
    np.testing.assert_array_equal(
        reconstruct_window(a, 0, 500), reconstruct_window(b, 173, 673)
    )
    assert validate_program(b)["status"] == "passed"


@pytest.mark.parametrize(
    "mod",
    [
        {"kind": "half_sine", "amplitude": math.pi},
        {"kind": "polynomial", "coefficients": [0.1, 0.3, -0.7, 0.9]},
    ],
)
def test_gaussian_local_phase_modulation_spans_tiles(mod):
    p = pulse(
        duration_samples=1000,
        frequency_hz=173.123456e6,
        envelope=dict(
            kind="flat_top_gaussian", sigma_samples=50, cutoff_sigma=4, flat_samples=600
        ),
        phase_modulation=mod,
        phase_scale=-1,
    )
    program = build_card_program(card(), [p], 1000, max_tile_samples=512)
    assert validate_program(program)["status"] == "passed"
    data = reconstruct_window(program, 0, 1000)[:, 0]
    n = np.arange(1000)
    env = (
        np.exp(-0.5 * (np.maximum(200 - np.minimum(n, 1000 - n), 0) / 50) ** 2)
        - math.exp(-8)
    ) / (1 - math.exp(-8))
    local = (
        math.pi * np.sin(math.pi * n / 1000)
        if mod["kind"] == "half_sine"
        else sum(c * (n / 1000) ** i for i, c in enumerate(mod["coefficients"]))
    )
    expected = np.rint(
        0.7
        * env
        * np.cos(0.25 + 2 * math.pi * 173.123456e6 * n / 1.25e9 - local)
        * 32767
    )
    np.testing.assert_allclose(data, expected, atol=1)
    assert data[0] == 0
    assert program["physical_duration_samples"] > 1000


def test_phase_steps_are_local_signed_and_same_time_merged():
    p = pulse(
        duration_samples=900,
        phase_steps=[
            {"sample": 150, "delta_rad": 0.4},
            {"sample": 150, "delta_rad": -0.1},
            {"sample": 650, "delta_rad": -0.9},
        ],
    )
    program = build_card_program(card(), [p], 900, max_tile_samples=512)
    data = reconstruct_window(program, 0, 900)[:, 0]
    n = np.arange(900)
    phases = 0.25 + 0.3 * (n >= 150) - 0.9 * (n >= 650)
    expected = np.rint(0.7 * 32767 * np.cos(2 * math.pi * n / 10 + phases))
    np.testing.assert_allclose(data, expected, atol=1)
    assert len(program["pulses"][0]["phase_steps"]) == 2
    assert validate_program(program, expected_pulses=[p])["status"] == "passed"


def test_long_idle_is_finite_compressed_and_validation_does_not_expand_it():
    later = 125000000
    pulses = [pulse(), pulse(start_sample=later, phase_rad=0.91)]
    program = build_card_program(card(), pulses, later + 500)
    report = validate_program(program)
    assert report["checked_samples"] < 3000
    assert program["memory"]["resident_bytes"] < 50000
    assert any(s["loops"] > 30000 for s in program["steps"])
    assert len(json.dumps(program)) < 20000
    np.testing.assert_array_equal(reconstruct_window(program, later - 10, later), 0)
    np.testing.assert_array_equal(
        reconstruct_window(program, later, later + 500),
        reconstruct_window(
            build_card_program(card(), [pulse(phase_rad=0.91)], 500), 0, 500
        ),
    )
    with pytest.raises(CompileError, match="max_samples"):
        reconstruct_window(program, 0, later)


def test_short_pulses_and_boundaries_preserve_exact_positions_and_tail():
    pulses = [
        pulse(start_sample=7, duration_samples=500),
        pulse(
            channel=1,
            start_sample=400,
            duration_samples=1,
            frequency_hz=0,
            phase_rad=0,
        ),
    ]
    program = build_card_program(card(), pulses, 507, max_tile_samples=512)
    data = reconstruct_window(program, 0, program["physical_duration_samples"])
    assert np.all(data[:7] == 0)
    assert np.all(data[507:] == 0)
    assert np.flatnonzero(data[:, 1]).tolist() == [400]
    assert validate_program(program)["status"] == "passed"
    for segment in program["segments"]:
        assert segment["sample_count"] >= 192
        assert segment["sample_count"] % 32 == 0


def test_same_channel_overlap_rejected_but_different_channels_parallel():
    with pytest.raises(CompileError, match="overlap"):
        build_card_program(card(), [pulse(), pulse(start_sample=100)], 600)
    program = build_card_program(card(), [pulse(), pulse(channel=1)], 500)
    data = reconstruct_window(program, 0, 500)
    np.testing.assert_array_equal(data[:, 0], data[:, 1])
    assert validate_program(program)["status"] == "passed"


def test_packed_bytes_dedup_is_phase_aware():
    p = pulse(duration_samples=4000)
    same = build_card_program(
        card(), [p, dict(p, start_sample=4000)], 8000, max_tile_samples=4000
    )
    # A long pulse contains repeatable packed tiles but its final zero tail differs.
    single = build_card_program(
        card(), [pulse(duration_samples=12000)], 12000, max_tile_samples=4000
    )
    different = build_card_program(
        card(),
        [p, dict(p, start_sample=4000, phase_rad=math.pi / 2)],
        8000,
        max_tile_samples=4000,
    )
    assert validate_program(same)["status"] == "passed"
    assert validate_program(different)["status"] == "passed"
    assert any(s["loops"] > 1 for s in single["steps"])
    assert len(different["segments"]) >= len(same["segments"])


def test_equal_partition_memory_is_not_just_total_resident_bytes():
    # Four equal slots are required by three unequal byte-distinct segments.
    program = build_card_program(
        card(),
        [pulse(duration_samples=5000, frequency_hz=123456789)],
        5000,
        max_tile_samples=2000,
    )
    assert program["memory"]["segment_slots"] >= 4
    required = program["memory"]["allocated_bytes"]
    assert required > program["memory"]["resident_bytes"]
    with pytest.raises(CompileError, match="equal-partition"):
        build_card_program(
            card(memory_bytes=required - 1),
            [pulse(duration_samples=5000, frequency_hz=123456789)],
            5000,
            max_tile_samples=2000,
        )


def test_native_segment_step_and_loop_limits():
    p = pulse(duration_samples=5000, frequency_hz=123456789)
    with pytest.raises(CompileError, match="segment limit"):
        build_card_program(card(max_segments=2), [p], 5000, max_tile_samples=512)
    with pytest.raises(CompileError, match="step limit"):
        build_card_program(card(max_steps=2), [p], 5000, max_tile_samples=512)
    program = build_card_program(card(max_loops=2), [], 40000)
    assert all(1 <= s["loops"] <= 2 for s in program["steps"])
    assert validate_program(program)["status"] == "passed"
    program = build_card_program(dict(active_channels=[1]), [pulse(channel=1)], 500)
    assert all(s["sample_count"] >= 384 for s in program["segments"])
    assert validate_program(program)["status"] == "passed"


@pytest.mark.parametrize(
    "change",
    [
        {"amplitude": 1.001},
        {"amplitude": float("nan")},
        {"frequency_hz": 400e6 + 1},
        {"start_sample": -1},
        {"duration_samples": 1.5},
        {"phase_modulation": {"kind": "unknown"}},
    ],
)
def test_unsupported_or_clipping_inputs_are_rejected(change):
    with pytest.raises(CompileError):
        build_card_program(card(), [pulse(**change)], 500)


def test_validation_detects_wrong_hash_phase_position_next_and_duration():
    original = build_card_program(
        card(), [pulse(duration_samples=2000)], 2000, max_tile_samples=512
    )
    bad = copy.deepcopy(original)
    bad["segments"][0]["sha256"] = "00" * 32
    with pytest.raises(CompileError, match="hash"):
        validate_program(bad)
    for field, value in (("phase_rad", 1.3), ("frequency_hz", 110e6)):
        bad = copy.deepcopy(original)
        bad["segments"][0]["recipe"]["fragments"][0]["pulse"][field] = value
        bad["segments"][0]["sha256"] = hashlib.sha256(
            materialize_segment(bad, 0).tobytes()
        ).hexdigest()
        with pytest.raises(CompileError, match="pulse targets"):
            validate_program(bad)
    bad = copy.deepcopy(original)
    bad["steps"][0]["next_step"] = 0
    with pytest.raises(CompileError, match="cycle"):
        validate_program(bad)
    bad = copy.deepcopy(original)
    bad["physical_duration_samples"] += 32
    with pytest.raises(CompileError, match="duration"):
        validate_program(bad)


def test_external_oracle_and_input_snapshots_cannot_be_mutated_silently():
    p = pulse()
    program = build_card_program(card(), [p], 500)
    p["phase_rad"] = 1.2
    assert validate_program(program)["status"] == "passed"
    with pytest.raises(CompileError, match="pulse targets"):
        validate_program(program, expected_pulses=[p])
    windows = list(iter_program_samples(program, 100, 130, chunk_samples=11))
    assert [len(array) for _, array in windows] == [11, 11, 8]
    np.testing.assert_array_equal(
        np.concatenate([a for _, a in windows]), reconstruct_window(program, 100, 130)
    )


def test_tight_tile_bound_and_empty_card_keep_native_sizes():
    program = build_card_program(card(), [pulse()], 500, max_tile_samples=256)
    assert [s["sample_count"] for s in program["segments"]] == [256, 256]
    assert validate_program(program)["status"] == "passed"
    for duration in (0, 1, 191, 192, 3999, 4000, 4031, 4032):
        program = build_card_program(card(), [], duration)
        assert validate_program(program)["checked_samples"] == 0
        assert program["physical_duration_samples"] > duration


def test_deterministic_small_random_multichannel_programs():
    rng = np.random.default_rng(491)
    for _ in range(16):
        pulses = []
        for channel in (0, 1):
            start = int(rng.integers(0, 400))
            for _ in range(5):
                duration = int(rng.integers(1, 700))
                pulses.append(
                    pulse(
                        channel=channel,
                        start_sample=start,
                        duration_samples=duration,
                        frequency_hz=float(rng.uniform(30000, 200e6)),
                        phase_rad=float(rng.uniform(-math.pi, math.pi)),
                    )
                )
                start += duration + int(rng.integers(0, 6000))
        duration = max(p["start_sample"] + p["duration_samples"] for p in pulses)
        program = build_card_program(card(), pulses, duration, max_tile_samples=1024)
        assert validate_program(program)["status"] == "passed"


def test_large_malformed_segment_rejected_before_materialization():
    program = build_card_program(card(), [pulse()], 500)
    program["segments"][0]["sample_count"] = 2**40
    with pytest.raises(CompileError, match="materialization bound"):
        validate_program(program)


def test_native_indices_cannot_escape_allocated_slots():
    program = build_card_program(card(), [pulse()], 500)
    program["segments"][0]["id"] = 1234
    program["steps"][0]["segment_id"] = 1234
    with pytest.raises(CompileError, match="native slots"):
        validate_program(program)


def test_zero_loop_cannot_hide_deleted_pulse_or_added_samples():
    program = build_card_program(card(), [], 100000000)
    with pytest.raises(CompileError, match="pulse targets"):
        validate_program(program, expected_pulses=[pulse(start_sample=50000000)])
    program["steps"][0]["loops"] += 1
    with pytest.raises(CompileError, match="duration"):
        validate_program(program)
