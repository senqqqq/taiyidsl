"""Final DAC codes, portable sampled assets and native sequence/bank replay."""

import base64
import copy
import hashlib
import json
from fractions import Fraction

import numpy as np
import pytest

from taiyidsl.backends.artiq_spectrum.awg import (
    build_card_program,
    iter_program_samples,
    reconstruct_window,
    render_work_budget,
)
from taiyidsl.backends.artiq_spectrum.banks import (
    build_bank,
    entry_program,
    validate_bank,
)
from taiyidsl.backends.artiq_spectrum.validation_awg import validate_program
from taiyidsl.ir import CompileError

RATE = 1_250_000_000
CARD = {"active_channels": [0]}


def sampled(
    values, *, kind="waveform", policy="strict", interval=Fraction(1, RATE), **overrides
):
    dtype = "<c16" if np.iscomplexobj(values) else "<f8"
    raw = np.asarray(values, dtype=dtype).tobytes()
    digest = hashlib.sha256(dtype.encode() + raw).hexdigest()
    asset = {
        "dtype": dtype,
        "count": len(values),
        "data": base64.b64encode(raw).decode(),
    }
    pulse = {
        "channel": 0,
        "start_sample": 0,
        "duration_samples": int(len(values) * interval * RATE),
        "frequency_hz": 0,
        "amplitude": 1,
        "phase_rad": 0,
        "envelope": {
            "kind": "sampled",
            "interpretation": kind,
            "resample": policy,
            "sample_digest": digest,
            "sample_count": len(values),
            "sample_interval_s": {
                "numerator": interval.numerator,
                "denominator": interval.denominator,
            },
        },
        **overrides,
    }
    return pulse, {digest: asset}


def test_raw_uses_direct_gain_and_shared_source_in_bank_and_recipes():
    values = np.tile([0.25, -0.5, 0.75, -1.0], 330)
    p, table = sampled(values, amplitude=-0.5)
    duration = len(values)
    inputs = [p, dict(p, start_sample=duration + 4)]
    first = build_card_program(
        CARD, inputs, 2 * duration + 4, max_tile_samples=800, sample_assets=table
    )
    other = build_card_program(
        CARD,
        [dict(p, amplitude=0.25)],
        duration,
        max_tile_samples=800,
        sample_assets=table,
    )
    bank = json.loads(json.dumps(build_bank({0: first, 1: other})))
    assert validate_bank(bank)["point_count"] == 2
    assert entry_program(bank, 0) == first and entry_program(bank, 1) == other
    payload = next(iter(table.values()))["data"]
    assert json.dumps(first).count(payload) == json.dumps(bank).count(payload) == 1
    expected = np.rint(np.r_[values, np.zeros(4), values] * -0.5 * 32767)
    np.testing.assert_array_equal(
        reconstruct_window(first, 0, len(expected))[:, 0], expected
    )
    assert validate_program(first)["status"] == "passed"


def test_shared_bank_slot_preserves_each_entries_distinct_source_asset():
    values = np.linspace(-0.5, 0.5, 64)
    first_pulse, first_assets = sampled(values)
    other_pulse, other_assets = sampled(-values, amplitude=-1)
    first = build_card_program(CARD, [first_pulse], 64, sample_assets=first_assets)
    other = build_card_program(CARD, [other_pulse], 64, sample_assets=other_assets)
    bank = build_bank({0: first, 1: other})
    assert len(bank["segments"]) == 1 and len(bank["sample_assets"]) == 2
    assert entry_program(bank, 0) == first
    assert entry_program(bank, 1) == other
    del bank["sample_assets"][next(iter(first_assets))]
    with pytest.raises(CompileError, match="missing sample assets"):
        validate_bank(bank)


@pytest.mark.parametrize("kind", ["envelope", "iq"])
@pytest.mark.parametrize("ratio", [Fraction(3, 2), Fraction(2, 3)])
def test_linear_resampling_keeps_origin_phase_and_last_value_across_tiles(kind, ratio):
    source_axis = np.arange(3000)
    base = 0.4 * np.cos(source_axis / 19)
    if kind == "iq":
        base = base + 0.4j * np.sin(source_axis / 31)
    p, table = sampled(
        base,
        kind=kind,
        policy="linear",
        interval=ratio / RATE,
        amplitude=0.7,
        phase_rad=0.22,
        frequency_hz=83e6,
        start_sample=37,
        phase_steps=[{"sample": 456, "delta_rad": 0.25}],
        phase_modulation={"kind": "half_sine", "amplitude": 0.15},
    )
    end = p["start_sample"] + p["duration_samples"]
    program = build_card_program(
        CARD, [p], end, sample_assets=table, max_tile_samples=512
    )
    assert len(program["segments"]) > 2
    assert validate_program(program, chunk_samples=117)["status"] == "passed"
    segments = list(iter_program_samples(program, 37, end, chunk_samples=73))
    y = np.concatenate([data[:, 0] for _, data in segments])
    n = np.arange(len(y))
    interpolated = np.interp(n / float(ratio), source_axis, base)
    theta = (
        0.22
        + 2 * np.pi * 83e6 * n / RATE
        + (n >= 456) * 0.25
        + 0.15 * np.sin(np.pi * n / len(y))
    )
    expected = np.rint(32767 * 0.7 * (interpolated * np.exp(1j * theta)).real).astype(
        "<i2"
    )
    np.testing.assert_array_equal(y, expected)
    assert np.all(
        reconstruct_window(program, end, program["physical_duration_samples"]) == 0
    )


def test_sample_digest_grid_and_independent_expected_dac_are_checked():
    p, table = sampled([0.5, 0.25, 1, 0], interval=Fraction(2, RATE))
    with pytest.raises(CompileError, match="grid"):
        build_card_program(CARD, [p], 8, sample_assets=table)
    p["envelope"]["resample"] = "linear"
    with pytest.raises(CompileError, match="Missing"):
        build_card_program(CARD, [p], 8)
    program = build_card_program(CARD, [p], 8, sample_assets=table)
    corrupt = copy.deepcopy(program)
    next(iter(corrupt["sample_assets"].values()))["data"] = base64.b64encode(
        bytes(32)
    ).decode()
    with pytest.raises(CompileError, match="digest"):
        validate_program(corrupt)
    with pytest.raises(CompileError, match="pulse targets"):
        validate_program(program, expected_pulses=[dict(p, amplitude=-1)])


def test_sample_storage_shares_the_streaming_workspace_with_dac_tiles():
    values = np.linspace(-1, 1, 3071)
    p, table = sampled(values)
    # 72 KiB allows 24 KiB of source and one 384-sample native tile.
    with render_work_budget(72 * 1024):
        program = build_card_program(CARD, [p], len(values), sample_assets=table)
        assert max(s["sample_count"] for s in program["segments"]) == 384
        assert validate_program(program)["status"] == "passed"
    with render_work_budget(68 * 1024):
        with pytest.raises(CompileError, match="budget"):
            build_card_program(CARD, [p], len(values), sample_assets=table)

    # Decoding 96 KiB temporarily needs another 128 KiB of ASCII storage.
    large, assets = sampled(np.linspace(-1, 1, 12288))
    with (
        render_work_budget(192 * 1024),
        pytest.raises(CompileError, match="Source samples exceed"),
    ):
        build_card_program(CARD, [large], 12288, sample_assets=assets)
