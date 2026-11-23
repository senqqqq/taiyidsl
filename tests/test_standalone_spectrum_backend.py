"""Single-card playback preserves the scheduled shot and public experiment path."""

import json
import math
from contextlib import nullcontext

import numpy as np
import pytest
from test_spectrum_backend import group_program
from test_spectrum_backend import lab as hybrid_lab

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    HalfSinePhase,
    LabConfig,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumOutput,
    Square,
    StandaloneSpectrumConfig,
    SweepParameter,
    delay,
    ns,
    play,
    reset_phase,
    section,
    shift_phase,
)
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window


def card_lab(mode="awg"):
    config = StandaloneSpectrumConfig(
        "A",
        SpectrumCard("SIM_A", (0, 1), mode=mode),
        {"rf": SpectrumOutput("A", 0), "aux": SpectrumOutput("A", 1)},
    )
    setup = DeviceSetup(
        {name: Device(name, "spectrum") for name in config.outputs},
        standalone_spectrum=config,
    )
    return LabConfig(setup, SignalMap({"q.rf": "rf", "aux": "aux"}), "spectrum")


def ramsey(*, hybrid=False):
    q = Qubit("q")
    exp = Experiment("standalone_ramsey", [q])
    pulse = Square(320 * ns, frequency=2.75e6, amplitude=0.37, phase=0.13)
    shaped = FlatTopGaussian(
        160 * ns,
        16 * ns,
        sample_interval=0.8 * ns,
        amplitude=0.25,
        frequency=5.6e6,
        phase_modulation=HalfSinePhase(0.8),
    )
    with exp.average(1), exp.realtime():
        with exp.triggered_sequence(group="g") if hybrid else nullcontext():
            delay(q.rf, 64 * ns)
            play(q.rf, pulse)
            shift_phase(q.frame, math.pi / 3)
            with section("dark"):
                delay(80 * ns)
            play(q.rf, shaped)
            reset_phase(q.frame)
            play(q.rf, pulse)
            with section("tail"):
                delay(96 * ns)
    return exp


def shot_program(compiled):
    (shot,) = compiled.points[0].physical_events
    assert shot.kind == "spectrum_shot"
    return shot.params["program"]


def test_equal_awg_waveforms_with_hybrid_trigger_prefix_removed():
    standalone = card_lab().compile(ramsey())
    single = shot_program(standalone)
    hybrid = group_program(hybrid_lab().compile(ramsey(hybrid=True)))["cards"]["A"][
        "program"
    ]
    prefix = hybrid["pulses"][0]["start_sample"] - single["pulses"][0]["start_sample"]
    count = single["logical_duration_samples"]
    assert single["pulses"][0]["start_sample"] == 80
    assert count == hybrid["logical_duration_samples"] - prefix
    assert (
        count
        - sum(single["pulses"][-1][k] for k in ("start_sample", "duration_samples"))
        == 120
    )
    assert standalone.points[0].duration_mu == count * 4
    assert (
        standalone.points[0].physical_events[0].duration_mu
        == single["physical_duration_samples"] * 4
    )
    np.testing.assert_array_equal(
        reconstruct_window(single, 0, count)[:, 0],
        reconstruct_window(hybrid, prefix, prefix + count)[:, 0],
    )
    assert [
        p["phase_evolution"]["carrier_reference_kind"] for p in single["pulses"]
    ] == ["shot", "shot", "explicit_reset_phase"]


def test_scan_order_multiple_channels_and_verified_export(tmp_path):
    q = Qubit("q")
    exp = Experiment("two_outputs", [q])
    amplitude = SweepParameter("amp", [0.25, 0.75], "dimensionless")
    pulse = Square(320 * ns, frequency=0, amplitude=0.25)
    with exp.average(2, mode="cyclic"), exp.sweep(amplitude), exp.realtime():
        with exp.parallel():
            play(q.rf, pulse, amplitude=amplitude)
            play("aux", pulse)
        delay(8 * ns)
    compiled = card_lab().compile(exp)
    assert list(compiled.execution_order()) == [(0, 0), (1, 0), (0, 1), (1, 1)]
    files = compiled.export(tmp_path)
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    assert files["plan"].exists()
    assert (
        manifest["backend"] == "spectrum"
        and manifest["compiled_sha256"] == compiled.sha256
    )
    assert manifest["verification"] == {"offline": "passed", "hardware": "not_verified"}
    for index, point in enumerate(manifest["points"]):
        assert point["offline_check"]["method"] == "awg_sample_replay"
        assert point["contract"]["trigger_source"] == "software"
        assert point["contract"]["clock_source"] == "internal"
        np.testing.assert_array_equal(
            reconstruct_window(point["program"], 0, 1)[0],
            [round(32767 * [0.25, 0.75][index]), round(32767 * 0.25)],
        )


@pytest.mark.parametrize(
    ("mode", "duration"),
    [("awg", 0.4 * ns), ("dds", 400 * ns)],
)
def test_shot_grid_is_exact_even_for_structural_waits(mode, duration):
    exp = Experiment("wait_grid", [])
    with exp.average(1), exp.realtime(), section("idle"):
        delay(duration)
    with pytest.raises(CompileError, match="timing grid"):
        card_lab(mode).compile(exp)


def test_idle_only_shot_and_resource_conflicts():
    exp = Experiment("idle_only", [])
    with exp.average(1), exp.realtime(), section("wait"):
        delay(64 * ns)
    program = shot_program(card_lab().compile(exp))
    assert program["logical_duration_samples"] == 80
    assert not np.any(reconstruct_window(program, 0, 80))
    exp = Experiment("overlap", [])
    pulse = Square(64 * ns, frequency=1e6)
    with exp.average(1), exp.realtime(), exp.parallel():
        play("aux", pulse)
        delay("aux", 32 * ns)
    with pytest.raises(CompileError, match="Overlapping Spectrum"):
        card_lab().compile(exp)


def test_one_experiment_compiles_to_awg_and_dds_with_the_same_shot_reference(tmp_path):
    q = Qubit("q")
    exp = Experiment("two_tones", [q])
    pulse = Square(403.2 * ns, frequency=2.75e6, amplitude=0.375, phase=0.2)
    with exp.average(1), exp.realtime():
        delay(320 * ns)
        play(q.rf, pulse)
        delay(640 * ns)
        shift_phase(q.frame, math.pi / 4)
        play(q.rf, pulse)
        delay(320 * ns)
    awg = shot_program(card_lab().compile(exp))
    compiled = card_lab("dds").compile(exp)
    dds = shot_program(compiled)
    for a, d in zip(awg["pulses"], dds["pulses"], strict=True):
        for key in ("start_sample", "duration_samples", "phase_rad"):
            assert a[key] == d[key]
    assert dds["duration_samples"] == awg["logical_duration_samples"]
    for frame in (dds["frames"][0], dds["frames"][-1]):
        assert all(tone["amplitude"] == 0 for tone in frame["tones"])
    manifest = json.loads(compiled.export(tmp_path)["manifest"].read_text("utf-8"))
    assert manifest["points"][0]["offline_check"]["method"] == "dds_command_validation"


def test_hybrid_trigger_and_inactive_dds_waveform_are_rejected():
    with pytest.raises(CompileError, match="triggered_sequence"):
        card_lab().compile(ramsey(hybrid=True))
    exp = Experiment("unsupported_dds", [])
    shaped = Square(640 * ns, frequency=2e6, phase_modulation=HalfSinePhase(0.1))
    with exp.average(1), exp.realtime(), exp.repeat(0):
        play("aux", shaped)
    with pytest.raises(CompileError, match="unmodulated Square"):
        card_lab("dds").compile(exp)
