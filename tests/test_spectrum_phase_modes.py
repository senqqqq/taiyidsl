"""Physical carrier history and carried modulation remain independent of F."""

import json
import math
from dataclasses import replace

import numpy as np
import pytest
from test_spectrum_backend import group_program, lab

from taiyidsl import (
    CompileError,
    ControlBinding,
    Experiment,
    HalfSinePhase,
    LabConfig,
    PolynomialPhase,
    Qubit,
    SpectrumOutput,
    Square,
    delay,
    ns,
    play,
    reset_frame,
    rz,
    shift_phase,
    trigplay,
)
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.backends.artiq_spectrum.export import artiq_subplan
from taiyidsl.backends.artiq_spectrum.phase import (
    OutputPhaseState,
    modulation_characteristics,
)


def phase_lab(mode="epoch", *, initial=7e6, scale=1):
    profile = lab()
    group = profile.setup.spectrum.groups["g"]
    outputs = dict(group.outputs)
    outputs["rf"] = SpectrumOutput(
        "A", 0, mode, initial if mode == "continuous" else None
    )
    profile.setup.spectrum = replace(
        profile.setup.spectrum, groups={"g": replace(group, outputs=outputs)}
    )
    profile.signal_map["q.rf"] = ControlBinding("rf", phase_scale=scale)
    return profile


def sequence(builder):
    q = Qubit("q")
    exp = Experiment("phase_modes", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        builder(q)
    return exp


def pulse_program(profile, exp):
    return group_program(profile.compile(exp))["cards"]["A"]["program"]


def samples(program, pulse):
    return reconstruct_window(
        program,
        pulse["start_sample"],
        pulse["start_sample"] + pulse["duration_samples"],
    )[:, 0]


@pytest.mark.parametrize("mode", ["epoch", "continuous"])
def test_carrier_uses_final_times_and_continuous_mode_integrates_idle(mode):
    def body(q):
        delay(80 * ns)
        play(q.rf, Square(400 * ns, frequency=11e6, amplitude=0.5))
        delay(120 * ns)
        play(q.rf, Square(400 * ns, frequency=13e6, amplitude=0.5))

    program = pulse_program(phase_lab(mode), sequence(body))
    first, second = program["pulses"]
    expected_first = 2 * math.pi * (7e6 if mode == "continuous" else 11e6) * 80e-9
    expected_second = (
        2
        * math.pi
        * (7e6 * 80e-9 + 11e6 * 520e-9 if mode == "continuous" else 13e6 * 600e-9)
    )
    assert first["phase_rad"] == pytest.approx(expected_first % (2 * math.pi))
    assert second["phase_rad"] == pytest.approx(expected_second % (2 * math.pi))
    n = np.arange(second["duration_samples"])
    expected = np.rint(
        32767 * 0.5 * np.cos(expected_second + 2 * np.pi * 13e6 * n / 1.25e9)
    )
    np.testing.assert_allclose(samples(program, second), expected, atol=1)


@pytest.mark.parametrize("scale", [1, -2])
def test_carry_transfers_full_analytic_endpoint_but_preserves_current_samples(scale):
    def body(mode):
        def build(q):
            play(
                q.rf,
                Square(
                    400 * ns,
                    frequency=11e6,
                    phase=0.17,
                    phase_modulation=PolynomialPhase((0.2, 0.7)),
                    phase_modulation_mode=mode,
                ),
            )
            play(q.rf, Square(400 * ns, frequency=11e6))

        return build

    local = pulse_program(phase_lab(scale=scale), sequence(body("local")))
    carry = pulse_program(phase_lab(scale=scale), sequence(body("carry")))
    np.testing.assert_array_equal(
        samples(local, local["pulses"][0]), samples(carry, carry["pulses"][0])
    )
    first, second = carry["pulses"]
    assert second["phase_evolution"]["inherited_modulation_phase_rad"] == pytest.approx(
        scale * 0.9
    )
    assert first["phase_evolution"]["modulation_end_rad"] == pytest.approx(0.9)
    assert first["phase_evolution"]["entry_modulation_jump_rad"] == pytest.approx(
        scale * 0.2
    )
    assert second["phase_rad"] == pytest.approx(
        (local["pulses"][1]["phase_rad"] + scale * 0.9) % (2 * math.pi)
    )


def test_reset_frame_clears_f_but_retains_carrier_and_carry_across_aliases():
    profile = phase_lab("continuous", scale=-2)
    profile.signal_map["other"] = ControlBinding("rf", phase_scale=0.5)
    profile.setup.spectrum = replace(
        profile.setup.spectrum,
        signal_frames={**profile.setup.spectrum.signal_frames, "other": "q"},
    )

    def body(q):
        shift_phase(q.frame, 0.4)
        rz(q, 0.3)
        play(
            q.rf,
            Square(
                400 * ns,
                frequency=11e6,
                phase_modulation=PolynomialPhase((0, 0.8)),
                phase_modulation_mode="carry",
            ),
        )
        reset_frame(q.frame)
        delay(120 * ns)
        play("other", Square(400 * ns, frequency=13e6))

    program = pulse_program(profile, sequence(body))
    second = program["pulses"][1]
    details = second["phase_evolution"]
    assert details["inherited_modulation_phase_rad"] == pytest.approx(-1.6)
    expected = 2 * math.pi * (11e6 * 520e-9) - 1.6
    assert second["phase_rad"] == pytest.approx(expected % (2 * math.pi))


def test_zero_duration_does_not_update_frequency_or_modulation():
    def body(q):
        play(
            q.rf,
            Square(
                0 * ns,
                frequency=21e6,
                phase_modulation=PolynomialPhase((1, 2)),
                phase_modulation_mode="carry",
            ),
        )
        delay(80 * ns)
        play(q.rf, Square(400 * ns, frequency=11e6))

    program = pulse_program(phase_lab("continuous"), sequence(body))
    assert len(program["pulses"]) == 1
    first = program["pulses"][0]
    assert first["phase_rad"] == pytest.approx(2 * math.pi * 7e6 * 80e-9)
    assert first["phase_evolution"]["inherited_modulation_phase_rad"] == 0


def test_outer_rz_stays_out_of_native_subplan_and_preserves_region_exit_frame():
    q = Qubit("q")
    exp = Experiment("outer_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, 0.3)
        with exp.triggered_sequence(group="g"):
            play(q.rf, Square(400 * ns, frequency=11e6))
        rz(q, 0.7)
    compiled = lab().compile(exp)
    native = artiq_subplan(compiled)
    assert all(e.kind != "rz" for e in native.points[0].logical_events)
    assert compiled.virtual_z_frames()["q"] == pytest.approx(1)
    group = group_program(compiled)
    assert group["terminal_frames"]["q"]["virtual_z"] == pytest.approx(0.3)
    assert group["cards"]["A"]["program"]["pulses"][0]["phase_rad"] == pytest.approx(
        2 * math.pi - 0.3
    )


def test_play_alias_and_tiling_preserve_samples_and_accumulate_modulation_once():
    pulse = Square(
        800 * ns,
        frequency=13.123456e6,
        phase_modulation=PolynomialPhase((0.1, 0.6)),
        phase_modulation_mode="carry",
    )
    results = []
    for command, tile in [(play, 500000), (trigplay, 512)]:
        profile = phase_lab("continuous")
        group = profile.setup.spectrum.groups["g"]
        profile.setup.spectrum = replace(
            profile.setup.spectrum, groups={"g": replace(group, max_tile_samples=tile)}
        )

        def body(q):
            command(q.rf, pulse)
            command(q.rf, pulse, phase_modulation_mode="local")

        program = pulse_program(profile, sequence(body))
        results.append([samples(program, item) for item in program["pulses"]])
        assert program["pulses"][1]["phase_evolution"][
            "inherited_modulation_phase_rad"
        ] == pytest.approx(0.7)
    for left, right in zip(*results):
        np.testing.assert_array_equal(left, right)


def test_modulation_range_is_diagnostic_without_claiming_analog_bandwidth():
    def body(q):
        play(
            q.rf,
            Square(
                8 * ns, frequency=399e6, phase_modulation=HalfSinePhase(2 * math.pi)
            ),
        )

    compiled = lab().compile(sequence(body))
    warnings = [
        d
        for d in compiled.diagnostics
        if d["kind"] == "spectrum_modulation_frequency_range"
    ]
    assert warnings and warnings[0]["maximum_hz"] > 400e6
    details = group_program(compiled)["cards"]["A"]["program"]["pulses"][0][
        "phase_evolution"
    ]
    assert (
        details["frequency_check_scope"]
        == "smooth_phase_derivative_not_analog_bandwidth"
    )
    extrema = modulation_characteristics(
        {"kind": "polynomial", "coefficients": [0, 0, 3, -2]}, 1, 10, 2 * math.pi
    )
    assert extrema["instantaneous_frequency_min_hz"] == 10
    assert extrema["instantaneous_frequency_max_hz"] == pytest.approx(11.5)


def test_output_mode_configuration_roundtrip_and_explicit_initial_frequency():
    profile = phase_lab("continuous")
    encoded = json.loads(json.dumps(profile.to_dict()))
    assert LabConfig.from_dict(encoded).to_dict() == encoded
    with pytest.raises(ValueError, match="initial_frequency"):
        SpectrumOutput("A", 0, "continuous")
    with pytest.raises(ValueError, match="only used"):
        SpectrumOutput("A", 0, "epoch", 1e6)
    for value in (True, float("inf"), -1, 401e6):
        with pytest.raises(ValueError, match="physical Hz"):
            SpectrumOutput("A", 0, "continuous", value)


def test_state_recreated_per_shot_and_large_idle_does_not_lose_carrier_fraction():
    output = SpectrumOutput("A", 0, "continuous", 13.123456e6)
    records = []
    for _ in range(2):
        state = OutputPhaseState(output)
        records.append(
            state.pulse(
                start_ticks=500_000_000_001,
                duration_ticks=4000,
                frequency_hz=11e6,
                phase_scale=1,
                modulation=None,
            )
        )
    assert records[0] == records[1]
    assert records[0]["carrier_phase_rad"] == pytest.approx(
        2 * math.pi * 13.123456e6 * 0.2e-9
    )


@pytest.mark.parametrize(
    "modulation", [PolynomialPhase((1e308, 1e308)), HalfSinePhase(1e308)]
)
def test_nonfinite_resolved_modulation_is_a_compile_error(modulation):
    def body(q):
        play(
            q.rf,
            Square(
                400 * ns,
                frequency=11e6,
                phase_modulation=modulation,
                phase_modulation_mode="carry",
            ),
        )

    with pytest.raises(CompileError, match="finite"):
        lab().compile(sequence(body))


def test_continuous_initial_frequency_is_checked_against_output_limits():
    profile = phase_lab("continuous", initial=7e6)
    profile.setup.devices["rf"] = replace(
        profile.setup.devices["rf"], max_frequency=5e6
    )
    with pytest.raises(CompileError, match="initial carrier frequency"):
        profile.compile(sequence(lambda q: play(q.rf, Square(400 * ns, frequency=4e6))))


def test_no_modulation_carry_and_half_sine_carry_have_zero_terminal_increment():
    def body(q):
        play(q.rf, Square(400 * ns, frequency=11e6, phase_modulation_mode="carry"))
        play(
            q.rf,
            Square(
                400 * ns,
                frequency=11e6,
                phase_modulation=HalfSinePhase(0.8),
                phase_modulation_mode="carry",
            ),
        )
        play(q.rf, Square(400 * ns, frequency=11e6))

    program = pulse_program(phase_lab(), sequence(body))
    assert all(
        p["phase_evolution"]["terminal_modulation_phase_rad"] == 0
        for p in program["pulses"]
    )
