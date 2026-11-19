import copy

import pytest

from taiyidsl.backends.artiq_spectrum.dds import (
    FREQUENCY_STEP_HZ,
    build_dds_program,
    dds_channel_cores,
    validate_dds_program,
)
from taiyidsl.ir import CompileError


def card_spec(**changes):
    return {
        "model": "M4i.6631-x8",
        "serial_number": "101",
        "mode": "dds",
        "active_channels": [0, 1],
        "sample_rate_hz": 1_250_000_000,
        "reference_clock_hz": 125_000_000,
        "output_range_mv": 500,
        "dds_routing": "16/5",
        "dds_trigger_latency_ns": 560,
        "dds_amplitude_slope_step_per_s": None,
        **changes,
    }


def tones(frequency=100e6, amplitude=0.125):
    return [
        dict(core=0, channel=0, frequency_hz=frequency, amplitude=amplitude),
        dict(core=20, channel=1, frequency_hz=90e6, amplitude=0.125),
    ]


def closed_move(amplitude=0.125):
    return dict(
        start_mu=4096,
        motion_duration_mu=8192,
        knots=[
            dict(offset_mu=0, tones=tones()),
            dict(offset_mu=4096, tones=tones(101e6, amplitude)),
            dict(offset_mu=8192, tones=tones()),
        ],
    )


def test_closed_transport_has_complete_hold_and_native_slopes():
    program = build_dds_program(
        card_spec(), tones(), [closed_move()], 16384, prefix_mu=1024
    )
    assert validate_dds_program(program)
    assert [frame["at_mu"] for frame in program["frames"]] == [
        0,
        5120,
        9216,
        13312,
        17408,
    ]
    assert program["initial_tones"] == program["final_tones"]
    assert program["frames"][0]["tones"][0]["frequency_slope_hz_per_s"] == 0
    assert program["frames"][1]["tones"][0]["frequency_slope_hz_per_s"] > 0
    assert program["frames"][2]["tones"][0]["frequency_slope_hz_per_s"] < 0
    assert program["frames"][-1]["trigger_source"] == "card"
    assert program["frames"][-1]["timer_mu"] is None
    assert all(t["amplitude"] > 0 for f in program["frames"] for t in f["tones"])
    assert program["initial_tones"][0]["frequency_hz"] / FREQUENCY_STEP_HZ == round(
        100e6 / FREQUENCY_STEP_HZ
    )
    assert program["quantization"]["max_endpoint_correction_hz"] > 0
    settle = program["frames"][-2]
    assert settle["set_frequency"] and settle["set_amplitude"]
    assert settle["tones"][0]["frequency_slope_hz_per_s"] == 0
    assert program["resident_command_count"] == (
        program["command_count"] + program["initialization_command_count"]
    )
    assert program["initialization_command_count"] == 4 * len(tones()) + 2 + 4


def test_consecutive_moves_pin_endpoints_and_compensate_first_update():
    forward = dict(
        start_mu=0,
        motion_duration_mu=4096,
        knots=[
            dict(offset_mu=0, tones=tones()),
            dict(offset_mu=4096, tones=tones(101e6)),
        ],
    )
    backward = dict(
        start_mu=4096,
        motion_duration_mu=4096,
        knots=[
            dict(offset_mu=0, tones=tones(101e6)),
            dict(offset_mu=4096, tones=tones()),
        ],
    )
    program = build_dds_program(card_spec(), tones(), [forward, backward], 8192)
    first, second, terminal = program["frames"]
    assert not first["set_frequency"]
    assert second["set_frequency"] and second["ramp_update_delay_mu"] == 32
    assert second["tones"][0]["frequency_hz"] == pytest.approx(101e6, abs=0.15)
    predicted = (
        second["tones"][0]["frequency_hz"]
        + (second["timer_mu"] - second["ramp_update_delay_mu"])
        * 0.2e-9
        * second["tones"][0]["frequency_slope_hz_per_s"]
    )
    assert abs(predicted - terminal["tones"][0]["frequency_hz"]) < 1
    assert validate_dds_program(program)


def test_amplitude_slopes_require_and_use_explicit_precision():
    with pytest.raises(CompileError, match="amplitude_slope_step"):
        build_dds_program(card_spec(), tones(), [closed_move(0.0625)], 16384)
    program = build_dds_program(
        card_spec(dds_amplitude_slope_step_per_s=1e-6),
        tones(),
        [closed_move(0.0625)],
        16384,
    )
    assert program["frames"][1]["tones"][0]["amplitude_slope_per_s"] < 0
    assert program["frames"][2]["tones"][0]["amplitude_slope_per_s"] > 0
    assert program["frames"][-1]["tones"][0]["amplitude_slope_per_s"] == 0
    assert validate_dds_program(program)


def test_topology_and_packet_limits_are_real_constraints():
    assert len(dds_channel_cores("16/5")[0]) == 16
    assert dds_channel_cores("16/5")[1] == (8, 9, 10, 11, 20)
    invalid = tones()
    invalid[0]["core"] = 8
    with pytest.raises(CompileError, match="route"):
        build_dds_program(card_spec(), invalid, [], 4096)
    assert validate_dds_program(
        build_dds_program(card_spec(dds_routing="20/1"), invalid, [], 4096)
    )
    with pytest.raises(CompileError, match="command cycles"):
        build_dds_program(card_spec(), tones(), [], 32 * 13)
    moves = [
        dict(start_mu=i * 8192, motion_duration_mu=8192, knots=closed_move()["knots"])
        for i in range(400)
    ]
    with pytest.raises(CompileError, match="4096"):
        build_dds_program(card_spec(), tones(), moves, 400 * 8192)


@pytest.mark.parametrize(
    "change,match",
    [
        (
            lambda move: move["knots"][-1]["tones"][0].update(frequency_hz=102e6),
            "return",
        ),
        (lambda move: move["knots"][0]["tones"][0].update(frequency_hz=99e6), "starts"),
        (lambda move: move["knots"][1].update(offset_mu=4097), "grid"),
        (lambda move: move["knots"][1]["tones"].pop(), "all and only"),
    ],
)
def test_invalid_motion_contracts_are_rejected(change, match):
    move = closed_move()
    change(move)
    with pytest.raises(CompileError, match=match):
        build_dds_program(card_spec(), tones(), [move], 16384)


def test_validator_detects_mutation_but_allows_external_metadata():
    program = build_dds_program(card_spec(), tones(), [closed_move()], 16384)
    program["calibration_sha256"] = "external manifest metadata"
    assert validate_dds_program(program)
    bad = copy.deepcopy(program)
    bad["frames"][1]["tones"][0]["frequency_slope_hz_per_s"] += 1
    with pytest.raises(CompileError, match="source intent"):
        validate_dds_program(bad)
