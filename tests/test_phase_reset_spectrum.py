"""Unified frame resets and physical AWG carrier references at final times."""

import math
from dataclasses import replace

import numpy as np
import pytest
from test_spectrum_backend import group_program, lab
from test_spectrum_phase_modes import phase_lab, samples

from taiyidsl import (
    CompileError,
    ControlBinding,
    Experiment,
    PolynomialPhase,
    Qubit,
    Square,
    delay,
    ns,
    play,
    reset_frame,
    reset_phase,
    rz,
    section,
    shift_phase,
)
from taiyidsl.spectrum_export import artiq_subplan


@pytest.mark.parametrize("reference", ["sequence", "shot"])
@pytest.mark.parametrize("mode", ["epoch", "continuous"])
def test_region_reference_preserves_preexisting_frame(reference, mode):
    q = Qubit("q")
    exp = Experiment("region_reference", [q])
    with exp.average(1), exp.realtime():
        shift_phase(q.frame, 0.7)
        rz(q, 0.2)
        play("camera", Square(80 * ns))
        with exp.triggered_sequence(group="g", phase_reference=reference):
            play(q.rf, Square(400 * ns, frequency=11e6, phase=0.1, amplitude=0.5))
    compiled = phase_lab(mode).compile(exp)
    group = group_program(compiled)
    program = group["cards"]["A"]["program"]
    pulse = program["pulses"][0]
    carrier_frequency = 7e6 if mode == "continuous" else 11e6
    carrier = 0 if reference == "sequence" else 2 * math.pi * carrier_frequency * 80e-9
    assert pulse["phase_rad"] == pytest.approx((carrier + 0.6) % (2 * math.pi))
    assert pulse["frame_phase"] == pytest.approx(0.5)
    assert group["entry_frames"]["q"]["frame_phase"] == pytest.approx(0.5)
    assert group["phase_reference"]["mode"] == reference
    assert group["phase_reference"]["reference_start_ticks"] == (
        400 if reference == "sequence" else 0
    )
    assert pulse["phase_evolution"]["carrier_reference_kind"] == reference
    n = np.arange(pulse["duration_samples"])
    expected = np.rint(
        32767 * 0.5 * np.cos(carrier + 0.6 + 2 * np.pi * 11e6 * n / 1.25e9)
    )
    np.testing.assert_allclose(samples(program, pulse), expected, atol=1)
    assert not any(
        event.kind in {"rz", "shift_phase"}
        for event in artiq_subplan(compiled).points[0].logical_events
    )


@pytest.mark.parametrize("command", [reset_frame, reset_phase])
@pytest.mark.parametrize("mode", ["epoch", "continuous"])
def test_reset_distinguishes_frame_from_carrier_and_modulation(command, mode):
    profile = phase_lab(mode, scale=-2)
    profile.signal_map["alias"] = ControlBinding("rf", phase_scale=0.5)
    profile.setup.spectrum = replace(
        profile.setup.spectrum,
        signal_frames={**profile.setup.spectrum.signal_frames, "alias": "q"},
    )
    q = Qubit("q")
    exp = Experiment("reset_domains", [q])
    with exp.average(1), exp.realtime():
        play("camera", Square(80 * ns))
        with exp.triggered_sequence(group="g"):
            shift_phase(q.frame, 0.4)
            rz(q, 0.3)
            play(
                q.rf,
                Square(
                    400 * ns,
                    frequency=11e6,
                    phase_modulation=PolynomialPhase((0.2, 0.6)),
                    phase_modulation_mode="carry",
                ),
            )
            command(q.frame)
            delay(120 * ns)
            play("alias", Square(400 * ns, frequency=13e6, phase=0.2))
    compiled = profile.compile(exp)
    group = group_program(compiled)
    program = group["cards"]["A"]["program"]
    second = program["pulses"][1]
    details = second["phase_evolution"]
    reset_physical = command is reset_phase
    carrier_frequency = 11e6 if mode == "continuous" else 13e6
    elapsed = 120e-9 if reset_physical else 520e-9
    carried = 0 if reset_physical else -1.6
    expected_phase = 2 * math.pi * carrier_frequency * elapsed + carried + 0.5 * 0.2
    assert second["phase_rad"] == pytest.approx(expected_phase % (2 * math.pi))
    assert second["frame_phase"] == second["virtual_z_phase"] == 0
    assert details["inherited_modulation_phase_rad"] == pytest.approx(carried)
    assert details["carrier_reference_ticks"] == (2400 if reset_physical else 400)
    assert group["terminal_frames"]["q"] == {
        "frame_phase": 0,
        "effective_phase": 0,
        "hardware_phase": 0,
        "virtual_z": 0,
    }
    assert compiled.frame_phases()["q"] == compiled.virtual_z_frames()["q"] == 0
    n = np.arange(second["duration_samples"])
    expected = np.rint(32767 * np.cos(expected_phase + 2 * np.pi * 13e6 * n / 1.25e9))
    np.testing.assert_allclose(samples(program, second), expected, atol=1)


@pytest.mark.parametrize("reference", ["sequence", "shot"])
@pytest.mark.parametrize("mode", ["epoch", "continuous"])
def test_pre_region_explicit_reset_only_sets_carrier_origin_in_shot_mode(
    reference, mode
):
    q = Qubit("q")
    exp = Experiment("outer_reset", [q])
    with exp.average(1), exp.realtime():
        rz(q, 0.4)
        play("camera", Square(40 * ns))
        reset_phase(q.frame)
        play("camera", Square(40 * ns))
        with exp.triggered_sequence(group="g", phase_reference=reference):
            play(q.rf, Square(400 * ns, frequency=11e6))
    compiled = phase_lab(mode).compile(exp)
    pulse = group_program(compiled)["cards"]["A"]["program"]["pulses"][0]
    details = pulse["phase_evolution"]
    expected_reference = 400 if reference == "sequence" else 200
    frequency = 7e6 if mode == "continuous" else 11e6
    expected_phase = 0 if reference == "sequence" else 2 * math.pi * frequency * 40e-9
    assert pulse["phase_rad"] == pytest.approx(expected_phase)
    assert details["carrier_reference_ticks"] == expected_reference
    assert pulse["frame_phase"] == pulse["virtual_z_phase"] == 0
    assert not any(
        event.kind == "reset_phase"
        for event in artiq_subplan(compiled).points[0].logical_events
    )


def test_reset_through_other_qubit_alias_clears_the_actual_output():
    profile = phase_lab("continuous")
    profile.signal_map["other.rf"] = "rf"
    q, other = Qubit("q"), Qubit("other")
    exp = Experiment("physical_alias_reset", [q, other])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        play(
            q.rf,
            Square(
                400 * ns,
                frequency=11e6,
                phase_modulation=PolynomialPhase((0, 0.8)),
                phase_modulation_mode="carry",
            ),
        )
        reset_phase(other.frame)
        play(q.rf, Square(400 * ns, frequency=13e6))
    pulse = group_program(profile.compile(exp))["cards"]["A"]["program"]["pulses"][1]
    assert pulse["phase_rad"] == 0
    assert pulse["phase_evolution"]["inherited_modulation_phase_rad"] == 0
    assert pulse["phase_evolution"]["carrier_reference_ticks"] == 2000


def test_midpulse_reset_is_rejected_through_a_different_qubit_alias():
    profile = lab()
    profile.signal_map["other.rf"] = "rf"
    q, other = Qubit("q"), Qubit("other")
    exp = Experiment("midpulse_reset", [q, other])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.parallel():
            play(q.rf, Square(400 * ns, frequency=11e6))
            with exp.serial():
                delay(80 * ns)
                reset_phase(other.frame)
    with pytest.raises(CompileError, match="reset_phase.*active Spectrum pulse"):
        profile.compile(exp)


def test_nested_sections_and_repeats_do_not_reset_frame_carrier_or_modulation():
    q = Qubit("q")
    exp = Experiment("nested_phase", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        rz(q, 0.2)
        with section("outer"):
            with exp.repeat(2), section("inner"):
                play(
                    q.rf,
                    Square(
                        400 * ns,
                        frequency=11e6,
                        phase_modulation=PolynomialPhase((0, 0.8)),
                        phase_modulation_mode="carry",
                    ),
                )
    pulses = group_program(phase_lab("continuous").compile(exp))["cards"]["A"][
        "program"
    ]["pulses"]
    assert pulses[0]["phase_rad"] == pytest.approx((-0.2) % (2 * math.pi))
    assert pulses[1]["phase_rad"] == pytest.approx(
        (2 * math.pi * 11e6 * 400e-9 + 0.8 - 0.2) % (2 * math.pi)
    )
    assert pulses[1]["phase_evolution"]["carrier_reference_ticks"] == 0


@pytest.mark.parametrize("command", [reset_frame, reset_phase])
def test_outer_phase_commands_reject_parallel_and_mixed_ownership(command):
    q = Qubit("q")
    exp = Experiment("outer_phase_parallel", [q])
    with exp.average(1), exp.realtime(), exp.parallel():
        command(q.frame)
        play("camera", Square(8 * ns))
    with pytest.raises(CompileError, match="serial realtime position"):
        lab().compile(exp)
    profile = lab(cooling=True)
    profile.signal_map["q.raman"] = "cooling"
    exp = Experiment("mixed_frame", [q])
    with exp.average(1), exp.realtime():
        command(q.frame)
    with pytest.raises(CompileError, match="Spectrum-only qubit"):
        profile.compile(exp)


@pytest.mark.parametrize("shape", ["square", "ram", "polar"])
def test_hybrid_dds_carrier_reset_uses_native_mu_once(shape):
    from test_phase_reset_artiq import _actual_phase, _effects, _lab, _pulse

    q, native = _lab(shape, calibrated=True)
    awg = Qubit("q")
    hybrid = lab()
    hybrid.setup.devices.update(native.setup.devices)
    hybrid.setup.calibration.update(native.setup.calibration)
    hybrid.signal_map.update(native.signal_map)
    results = []
    for profile in (native, hybrid):
        exp = Experiment("native_reference_units", [q, awg])
        with exp.average(1), exp.realtime():
            rz(q, 0.4)
            play(q.raman, _pulse(shape), phase=0.2)
            delay(q.raman, 4000 * ns)
            reset_phase(q.frame)
            play(q.raman, _pulse(shape), phase=0.2)
            if profile is hybrid:
                with exp.triggered_sequence(group="g"):
                    play(awg.rf, Square(400 * ns, frequency=11e6))
        compiled = profile.compile(exp)
        results.append(artiq_subplan(compiled) if profile is hybrid else compiled)
    original, converted = results
    converted_reset = next(
        e for e in converted.points[0].logical_events if e.kind == "reset_phase"
    )
    assert converted_reset.params["carrier_reference_mu"] == 4800
    native_effects, hybrid_effects = (_effects(result, shape) for result in results)
    assert len(native_effects) == len(hybrid_effects)
    for expected, actual in zip(native_effects, hybrid_effects):
        assert actual.start_mu - converted.points[0].origin_mu == (
            expected.start_mu - original.points[0].origin_mu
        )
        for name in (
            "phase",
            "carrier_reference_mu",
            "carrier_reference_physical_mu",
            "ftw",
            "square_pow",
            "ram_pow",
        ):
            assert actual.params.get(name) == expected.params.get(name)
        assert _actual_phase(actual, actual.start_mu, shape, True) == _actual_phase(
            expected, expected.start_mu, shape, True
        )
