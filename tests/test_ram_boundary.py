"""Gate-boundary RAM replay preserves the requested nonzero envelope and phase."""

import math
import runpy
from dataclasses import replace
from html import unescape
from pathlib import Path

import pytest

from taiyidsl import (
    CompileError,
    ControlBinding,
    Device,
    DeviceCalibration,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    SignalMap,
    delay,
    ns,
    play,
    reserve,
    reset_frame,
    shift_phase,
    us,
)
from taiyidsl.reporting.visualization import _payload


def boundary_lab(count=1, **overrides):
    """Declared illustrative delays; these are not hardware calibration."""
    qubits = [Qubit(f"q{i}") for i in range(count)]
    settings = dict(
        ram_latency_mu=112,
        ram_phase_latency_mu=80,
        ram_rearm_duration_mu=4000,
        ram_phase_update_duration_mu=480,
    )
    settings.update(overrides)
    devices, signals = {}, {}
    for i, q in enumerate(qubits):
        devices[f"dds{i}"] = Device(
            f"dds{i}",
            "dds",
            bus=f"spi{i}",
            update_group=f"board{i}",
            **settings,
        )
        devices[f"ttl{i}"] = Device(f"ttl{i}", "ttl", grid_mu=1, min_duration_mu=1)
        signals[q.raman] = f"dds{i}"
        signals[q.readout] = f"ttl{i}"
    return qubits, LabConfig(DeviceSetup(devices), SignalMap(signals))


def envelope(*, cutoff=5, sigma=0.5 * us, flat_duration=3 * us):
    return FlatTopGaussian(
        flat_duration,
        sigma=sigma,
        cutoff_sigma=cutoff,
        amplitude=0.5,
        sample_interval=20 * ns,
        frequency=83e6,
    )


def sequence(phases=(0, math.pi / 2), *, cutoff=5, frequency=83e6, **settings):
    (q,), lab = boundary_lab(**settings)
    exp = Experiment("ram_boundary", [q])
    pulse = envelope(cutoff=cutoff)
    with exp.average(1):
        with exp.realtime():
            for phase in phases:
                play(q.raman, pulse, phase=phase, frequency=frequency)
    return lab.compile(exp)


def events(compiled, kind, signal=None):
    return [
        event
        for event in compiled.points[0].physical_events
        if event.kind == kind and (signal is None or event.signal == signal)
    ]


def relative_times(compiled, kind, signal=None):
    return [
        event.start_mu - compiled.points[0].origin_mu
        for event in events(compiled, kind, signal)
    ]


def test_adjacent_xy_has_exact_boundaries_and_continuous_rf_gate():
    compiled = sequence()
    point = compiled.points[0]
    launches = events(compiled, "dds_ram_start")
    assert point.duration_mu == 16000
    assert relative_times(compiled, "dds_ram_effect") == [0, 8000]
    assert relative_times(compiled, "dds_ram_start") == [-112, 7888]
    assert relative_times(compiled, "dds_ram_commit_off") == [-104, 7896]
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [16000]
    assert all(event.duration_mu == 0 for event in launches)
    assert all(event.params["ram_boundary"] for event in launches)
    assert not any(event.params.get("ram_frame_only") for event in launches)
    assert [event.params["ram_profile"] for event in launches] == [0, 0]
    assert [event.params["phase"] for event in launches] == pytest.approx(
        [0, math.pi / 2]
    )
    assert not events(compiled, "dds_ram_rearm")


def test_phase_prewrite_occupies_previous_gate_without_changing_its_samples():
    compiled = sequence()
    point = compiled.points[0]
    first, second = events(compiled, "dds_ram_start")
    (prepare,) = events(compiled, "dds_ram_phase")
    assert point.origin_mu <= prepare.start_mu < prepare.end_mu <= second.start_mu
    assert prepare.duration_mu == 480
    assert second.params["carrier_effect_mu"] - point.origin_mu == 7968
    # The early phase effect may overlap only the existing, quantized zero tail.
    # No zero samples are appended and no samples from the first envelope are cut.
    samples = first.params["ram_asf"]
    assert len(samples) == 401
    assert samples[-1] == 0
    assert all(samples[index] == 0 for index in range(398, 401))
    assert first.params["ram_asf"] == second.params["ram_asf"]
    assert second.params["effect_mu"] - first.params["effect_mu"] == 8000


def test_physical_plot_splits_xy_at_envelope_effect_not_preparation_or_launch():
    compiled = sequence()
    point = compiled.points[0]
    outputs = [
        trace
        for trace in _payload(compiled)[0]["physical"]
        if trace["name"] == "dds_output"
    ]
    origin_us = point.origin_mu * 1e-3
    assert len(outputs) == 2
    assert outputs[0]["x"] == pytest.approx([origin_us, origin_us + 8])
    assert outputs[1]["x"] == pytest.approx([origin_us + 8, origin_us + 16])
    first, second = events(compiled, "dds_ram_start")
    for trace, event in zip(outputs, (first, second)):
        # Hover is a short summary; complete RAM samples remain in selection data.
        parameters = trace["meta"]["details"]["parameters"]
        assert parameters["phase"] == pytest.approx(event.params["phase"])
        assert parameters["ram_asf"] == event.params["ram_asf"]
        hover = unescape(trace["hovertemplate"])
        assert f"<b>Phase (rad)</b>: {event.params['phase']}" in hover
        assert "ram_asf" not in hover
    assert first.params["phase"] != second.params["phase"]
    prepare = events(compiled, "dds_ram_phase")[0]
    assert prepare.start_mu < second.start_mu < point.origin_mu + 8000
    assert first.params["ram_asf"] == second.params["ram_asf"]


def test_tracking_uses_carrier_effect_time_at_noncommensurate_frequency():
    compiled = sequence(phases=(0, math.pi / 2, 0), frequency=83.123456e6)
    point = compiled.points[0]
    launches = events(compiled, "dds_ram_start")
    for event in launches:
        reference = event.params["carrier_effect_mu"] - point.origin_mu
        expected = event.params["pow"] + ((reference * event.params["ftw"]) >> 16)
        assert event.params["ram_pow"] == expected & 0xFFFF
    assert len({event.params["ram_pow"] for event in launches}) == 3


@pytest.mark.parametrize("phases", [(0, math.pi / 2), (0, 0)])
def test_short_zero_tail_rejects_early_phase_change_instead_of_padding(phases):
    # Default cutoff4 has a 20 ns quantized-zero tail; the declared skew is32 ns.
    # At83 MHz equal logical phase still needs an inexact tracking update.
    with pytest.raises(CompileError, match="(?i)(zero|phase|tail)"):
        sequence(phases=phases, cutoff=4)


def test_preparation_that_cannot_fit_between_commits_is_rejected():
    with pytest.raises(CompileError):
        sequence(ram_phase_update_duration_mu=9000)


def test_exact_phase_continuity_does_not_require_a_zero_tail_guard():
    compiled = sequence(phases=(0, 0), cutoff=4, frequency=125e6)
    first, second = events(compiled, "dds_ram_start")
    delta = second.params["carrier_effect_mu"] - first.params["carrier_effect_mu"]
    residual = (
        delta * first.params["ftw"]
        + (first.params["ram_pow"] - second.params["ram_pow"]) * 65536
    ) % 2**32
    assert residual == 0
    assert compiled.points[0].duration_mu == 14000
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [14000]


def test_boundary_frame_commands_match_explicit_gate_phases():
    (q,), lab = boundary_lab()
    exp = Experiment("ram_boundary_frames", [q])
    pulse = envelope()
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse)
            shift_phase(q.frame, math.pi / 2)
            play(q.raman, pulse)
            reset_frame(q.frame)
            play(q.raman, pulse)
    compiled = lab.compile(exp)
    explicit = sequence(phases=(0, math.pi / 2, 0))
    for kind in ("dds_ram_start", "dds_ram_effect"):
        actual = events(compiled, kind)
        expected = events(explicit, kind)
        assert relative_times(compiled, kind) == relative_times(explicit, kind)
        assert [event.params["ram_pow"] for event in actual] == [
            event.params["ram_pow"] for event in expected
        ]
    assert len(events(compiled, "dds_ram_start")) == 3
    assert relative_times(compiled, "dds_off") == [24000]


def test_initial_and_terminal_frame_commands_have_real_hardware_commits():
    (q,), lab = boundary_lab()
    exp = Experiment("ram_initial_terminal_frames", [q])
    with exp.average(1):
        with exp.realtime():
            shift_phase(q.frame, math.pi / 2)
            play(q.raman, envelope())
            reset_frame(q.frame)
    compiled = lab.compile(exp)
    point = compiled.points[0]
    first, terminal = events(compiled, "dds_ram_start")
    assert first.params["phase"] == pytest.approx(math.pi / 2)
    assert terminal.params["ram_frame_only"]
    assert terminal.params["carrier_effect_mu"] - point.origin_mu == 8000
    assert terminal.params["phase"] == pytest.approx(0)
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [8000]


def test_carrier_latency_need_not_align_to_commit_grid_for_play():
    compiled = sequence(ram_phase_latency_mu=79)
    point = compiled.points[0]
    assert relative_times(compiled, "dds_ram_start") == [-112, 7888]
    assert relative_times(compiled, "dds_ram_effect") == [0, 8000]
    assert [
        event.params["carrier_effect_mu"] - point.origin_mu
        for event in events(compiled, "dds_ram_start")
    ] == [-33, 7967]


def test_off_grid_standalone_frame_effect_is_rejected_without_rounding():
    (q,), lab = boundary_lab(ram_phase_latency_mu=79)
    exp = Experiment("ram_frame_off_grid", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, envelope())
            reset_frame(q.frame)
    with pytest.raises(CompileError, match="(?i)(grid|commit)"):
        lab.compile(exp)


def test_initial_frame_then_delay_and_multiple_rf_off_frame_updates():
    (q,), lab = boundary_lab()
    exp = Experiment("ram_rf_off_frames", [q])
    with exp.average(1):
        with exp.realtime():
            shift_phase(q.frame, math.pi / 2)
            delay(q.raman, 1 * us)
            play(q.raman, envelope())
            delay(q.raman, 1 * us)
            reset_frame(q.frame)
            delay(q.raman, 1 * us)
            shift_phase(q.frame, math.pi)
            delay(q.raman, 1 * us)
            play(q.raman, envelope())
    compiled = lab.compile(exp)
    point = compiled.points[0]
    launches = events(compiled, "dds_ram_start")
    frame_only = [event for event in launches if event.params["ram_frame_only"]]
    assert [
        event.params["carrier_effect_mu"] - point.origin_mu for event in frame_only
    ] == [0, 10000, 11000]
    assert [event.params["phase"] for event in frame_only] == pytest.approx(
        [math.pi / 2, 0, math.pi]
    )
    plays = [event for event in launches if not event.params["ram_frame_only"]]
    assert [event.params["phase"] for event in plays] == pytest.approx(
        [math.pi / 2, math.pi]
    )
    assert relative_times(compiled, "dds_on") == [1000, 12000]
    assert relative_times(compiled, "dds_off") == [9000, 20000]


def test_frame_only_restart_cannot_corrupt_following_envelope():
    (q,), lab = boundary_lab()
    exp = Experiment("ram_frame_restart_overlap", [q])
    with exp.average(1):
        with exp.realtime():
            shift_phase(q.frame, math.pi / 2)
            delay(q.raman, 16 * ns)
            play(q.raman, envelope(), frequency=125e6)
    # Carrier update at0 would restart RAM at32, after the play already began at16.
    with pytest.raises(CompileError):
        lab.compile(exp)


def test_successive_ram_updates_require_a_real_low_interval():
    (q,), lab = boundary_lab()
    exp = Experiment("ram_update_fall_rise_collision", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, envelope(), frequency=125e6)
            reset_frame(q.frame)
            delay(q.readout, 8 * ns)
            reset_frame(q.frame)
    # Equal POWs avoid SPI preparation; the remaining fall/rise collision must
    # still fail rather than silently losing the second IO_UPDATE rising edge.
    with pytest.raises(CompileError, match="(?i)(update|commit|low|overlap)"):
        lab.compile(exp)


def test_phase_inversion_and_calibration_apply_to_boundary_and_tail_frames():
    (q,), lab = boundary_lab()
    lab.signal_map[q.raman] = ControlBinding("dds0", phase_scale=-1, phase_offset=0.125)
    lab.setup.calibration["dds0"] = DeviceCalibration(phase_offset=0.25)
    exp = Experiment("ram_calibrated_frames", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, envelope(), phase=0.5)
            shift_phase(q.frame, math.pi / 2)
            play(q.raman, envelope(), phase=0.5)
            reset_frame(q.frame)
    compiled = lab.compile(exp)
    first, shifted, reset = events(compiled, "dds_ram_start")
    expected = (-0.5 + 0.125 + 0.25) % (2 * math.pi)
    assert first.params["phase"] == pytest.approx(expected)
    assert shifted.params["phase"] == pytest.approx(
        (expected - math.pi / 2) % (2 * math.pi)
    )
    assert reset.params["phase"] == pytest.approx(expected)
    assert reset.params["ram_frame_only"]


def test_frame_command_inside_ram_pulse_is_rejected():
    (q,), lab = boundary_lab()
    exp = Experiment("ram_interior_frame", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                play(q.raman, envelope())
                with exp.serial():
                    delay(q.readout, 4 * us)
                    shift_phase(q.frame, math.pi / 2)
    with pytest.raises(CompileError, match="(?i)(inside|boundary|active)"):
        lab.compile(exp)


def test_frame_carrier_effect_cannot_enter_a_reserved_dds_interval():
    (q, other), lab = boundary_lab(2)
    exp = Experiment("ram_reserved_frame_effect", [q, other])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(q.raman, envelope())
                    delay(q.readout, 2 * us)
                    shift_phase(q.frame, 0.5)
                with exp.serial():
                    delay(other.readout, 9992 * ns)
                    reserve(q.raman, 16 * ns)
    # Commit is at9920, but its carrier effect at10000 enters the reservation.
    with pytest.raises(CompileError, match="(?i)(effect|reservation)"):
        lab.compile(exp)


def test_independent_boards_start_and_change_phase_at_same_boundaries():
    qubits, lab = boundary_lab(2)
    exp = Experiment("ram_synchronous_boards", qubits)
    pulse = envelope()
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qubits:
                    with exp.serial():
                        play(q.raman, pulse)
                        shift_phase(q.frame, math.pi / 2)
                        play(q.raman, pulse)
    compiled = lab.compile(exp)
    for signal in ("dds0", "dds1"):
        assert relative_times(compiled, "dds_ram_start", signal) == [-112, 7888]
        assert relative_times(compiled, "dds_ram_effect", signal) == [0, 8000]
        assert relative_times(compiled, "dds_on", signal) == [0]
        assert relative_times(compiled, "dds_off", signal) == [16000]
    preparations = sorted(
        events(compiled, "dds_ram_phase"), key=lambda event: event.start_mu
    )
    assert len(preparations) == 2
    # The two physical SPI buses prepare together; output boundaries are intact.
    assert preparations[0].start_mu == preparations[1].start_mu
    assert preparations[0].end_mu == preparations[1].end_mu


@pytest.mark.parametrize("changes", [{"amplitude": 0.25}, {"frequency": 84e6}])
def test_profile_or_frequency_change_requires_explicit_rearm_interval(changes):
    (q,), lab = boundary_lab()
    pulse = envelope()
    for gap in (0, 4112):
        exp = Experiment(f"ram_rearm_{gap}", [q])
        with exp.average(1):
            with exp.realtime():
                play(q.raman, pulse)
                if gap:
                    delay(q.raman, gap * ns)
                play(q.raman, pulse, **changes)
        if gap:
            compiled = lab.compile(exp)
            assert len(events(compiled, "dds_ram_rearm")) == 1
            assert compiled.points[0].duration_mu == 20112
        else:
            with pytest.raises(CompileError, match="(?i)(rearm|gap|4112)"):
                lab.compile(exp)


def test_phase_latency_is_explicit_and_roundtrips():
    (q,), lab = boundary_lab()
    assert LabConfig.from_dict(lab.to_dict()).to_dict() == lab.to_dict()
    lab.setup.devices["dds0"] = replace(
        lab.setup.devices["dds0"], ram_phase_latency_mu=None
    )
    exp = Experiment("ram_legacy_still_requires_gap", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, envelope())
            play(q.raman, envelope(), phase=math.pi / 2)
    with pytest.raises(CompileError):
        lab.compile(exp)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "80"])
def test_phase_latency_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="ram_phase_latency_mu"):
        Device("dds", "dds", ram_phase_latency_mu=value)


def test_phase_latency_later_than_envelope_is_rejected():
    with pytest.raises(CompileError, match="(?i)(phase.*latency|latency.*phase)"):
        sequence(ram_phase_latency_mu=120)


def test_long_example_reuses_one_small_bank_without_added_delay():
    script = Path(__file__).parents[1] / "examples" / "ram_boundary_sequence.py"
    example = runpy.run_path(str(script))
    compiled = example["build_sequence"]()
    launches = events(compiled, "dds_ram_start")
    assert len(launches) == 1024
    assert compiled.points[0].duration_mu == 1024 * 800
    assert {event.params["ram_profile"] for event in launches} == {0}
    assert {len(event.params["ram_asf"]) for event in launches} == {201}
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [1024 * 800]
    assert not events(compiled, "dds_ram_rearm")


def test_two_board_800ns_same_axis_example_includes_terminal_hardware_updates():
    script = Path(__file__).parents[1] / "examples" / "ram_boundary_sequence.py"
    example = runpy.run_path(str(script))
    compiled = example["build_sequence"](count=8, channels=2, terminal_frame=True)
    assert compiled.points[0].duration_mu == 6400
    assert len(events(compiled, "dds_ram_start")) == 18
    assert len(events(compiled, "dds_nco_epoch")) == 2
    assert len(events(compiled, "dds_ram_phase")) == 2


def test_single_board_800ns_example_includes_real_terminal_frame_update():
    from taiyidsl.backends.artiq.codegen_ram import ram_bank_summary

    script = Path(__file__).parents[1] / "examples" / "ram_boundary_sequence.py"
    example = runpy.run_path(str(script))
    compiled = example["build_sequence"](count=8, terminal_frame=True)
    point = compiled.points[0]
    assert point.duration_mu == 6400
    (bank,) = ram_bank_summary(compiled)
    assert bank["plays"] == 8
    assert bank["launches"] == 9
    assert bank["frame_only_updates"] == 1
    launches = events(compiled, "dds_ram_start", "urukul0_ch0")
    assert launches[-1].params["ram_frame_only"]
    assert launches[-1].params["carrier_effect_mu"] - point.origin_mu == 6400
    assert relative_times(compiled, "dds_on", "urukul0_ch0") == [0]
    assert relative_times(compiled, "dds_off", "urukul0_ch0") == [6400]


@pytest.mark.parametrize("duration", [200, 800])
def test_short_xy_example_rejects_strict_latency_mismatch_without_padding(duration):
    script = Path(__file__).parents[1] / "examples" / "ram_boundary_sequence.py"
    example = runpy.run_path(str(script))
    with pytest.raises(CompileError, match="Strict RAM"):
        example["build_sequence"](count=8, duration_ns=duration, phase_pattern="xy")


def test_200ns_same_axis_example_keeps_original_short_envelope():
    script = Path(__file__).parents[1] / "examples" / "ram_boundary_sequence.py"
    example = runpy.run_path(str(script))
    compiled = example["build_sequence"](count=8, duration_ns=200)
    assert compiled.points[0].duration_mu == 1600
    assert {len(e.params["ram_asf"]) for e in events(compiled, "dds_ram_start")} == {51}
    assert not events(compiled, "dds_ram_phase")
