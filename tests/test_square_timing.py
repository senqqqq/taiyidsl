"""Output timing contracts for buffered single-tone DDS updates."""

import math
import runpy
from dataclasses import replace
from pathlib import Path

import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    delay,
    ns,
    play,
    reserve,
    reset_frame,
    shift_phase,
    us,
)
from taiyidsl.artiq import generate_source, verify_equivalence


def timed_lab(count=1, *, shared=False, latencies=None):
    """An offline declaration, not measured hardware calibration."""
    latencies = latencies or [80] * count
    devices, signals = {}, {}
    qubits = [Qubit(f"q{i}") for i in range(count)]
    for i, (q, latency) in enumerate(zip(qubits, latencies)):
        board = 0 if shared else i
        devices[f"dds{i}"] = Device(
            f"dds{i}",
            "dds",
            bus=f"spi{board}",
            update_group=f"board{board}",
            square_latency_mu=latency,
        )
        devices[f"control{i}"] = Device(
            f"control{i}", "ttl", grid_mu=1, min_duration_mu=1
        )
        signals[q.raman] = f"dds{i}"
        signals[q.readout] = f"control{i}"
    return qubits, LabConfig(DeviceSetup(devices), SignalMap(signals))


def test_phase_effect_cannot_enter_reserved_dds_interval():
    from taiyidsl import reserve

    qs, lab = timed_lab(2)
    exp = Experiment("reserved_effect", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(qs[0].raman, Square(4 * us))
                    delay(qs[0].readout, 2 * us)
                    shift_phase(qs[0].frame, 0.5)
                with exp.serial():
                    delay(qs[1].readout, 5992 * ns)
                    reserve(qs[0].raman, 16 * ns)
    # Commit is at5920, outside the reservation; output effect at6000 is inside.
    with pytest.raises(CompileError, match="effect overlaps"):
        lab.compile(exp)


def events(compiled, kind, signal=None):
    return [
        e
        for e in compiled.points[0].physical_events
        if e.kind == kind and (signal is None or e.signal == signal)
    ]


def relative_times(compiled, kind, signal=None):
    return [
        e.start_mu - compiled.points[0].origin_mu
        for e in events(compiled, kind, signal)
    ]


def adjacent(changes, *, duration=4 * us):
    (q,), lab = timed_lab()
    exp = Experiment("adjacent_square", [q])
    pulse = Square(duration, frequency=125e6, amplitude=0.5)
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse)
            play(q.raman, pulse, **changes)
    return lab.compile(exp)


@pytest.mark.parametrize(
    "changes",
    [{"phase": math.pi / 2}, {"frequency": 130e6}, {"amplitude": 0.25}],
)
def test_adjacent_parameter_changes_have_no_added_gap(changes):
    compiled = adjacent(changes)
    point = compiled.points[0]
    assert point.duration_mu == 8000
    assert relative_times(compiled, "dds_effect") == [0, 4000]
    assert relative_times(compiled, "dds_commit") == [-80, 3920]
    assert relative_times(compiled, "dds_commit_off") == [-72, 3928]
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [8000]
    assert verify_equivalence(compiled)["status"] == "passed"


def test_next_state_is_prepared_while_previous_square_is_on():
    compiled = adjacent({"phase": math.pi / 2})
    point = compiled.points[0]
    second = events(compiled, "dds_prepare")[-1]
    assert 0 <= second.start_mu - point.origin_mu
    assert second.end_mu - point.origin_mu <= 3920
    assert second.duration_mu >= 1248
    source = generate_source(compiled)
    assert ".write64(" in source
    assert ".set_mu(" not in source


def test_short_square_is_rejected_instead_of_padded():
    with pytest.raises(CompileError):
        adjacent({"phase": math.pi / 2}, duration=1 * us)


@pytest.mark.parametrize("shared", [False, True])
def test_two_channels_commit_together_with_continuous_outputs(shared):
    qs, lab = timed_lab(2, shared=shared)
    exp = Experiment("synchronous_square", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qs:
                    with exp.serial():
                        play(q.raman, Square(4 * us, frequency=125e6))
                        play(q.raman, Square(4 * us, frequency=125e6, phase=0.5))
    compiled = lab.compile(exp)
    for i in range(2):
        assert relative_times(compiled, "dds_effect", f"dds{i}") == [0, 4000]
        assert relative_times(compiled, "dds_on", f"dds{i}") == [0]
        assert relative_times(compiled, "dds_off", f"dds{i}") == [8000]
    assert relative_times(compiled, "dds_commit").count(3920) == (1 if shared else 2)
    if shared:
        assert set(events(compiled, "dds_commit")[-1].params["commit_members"]) == {
            "dds0",
            "dds1",
        }
    assert verify_equivalence(compiled)["status"] == "passed"


def test_shared_commit_preserves_unchanged_channel_phase():
    qs, lab = timed_lab(2, shared=True)
    exp = Experiment("shared_update_held_state", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(qs[0].raman, Square(4 * us, frequency=125e6))
                    play(qs[0].raman, Square(4 * us, frequency=125e6, phase=0.5))
                play(qs[1].raman, Square(8 * us, frequency=123_456_789, phase=0.25))
    compiled = lab.compile(exp)
    held = events(compiled, "dds_effect", "dds1")
    assert relative_times(compiled, "dds_effect", "dds1") == [0, 4000]
    assert [e.params["phase"] for e in held] == pytest.approx([0.25, 0.25])
    prepared = events(compiled, "dds_prepare", "dds1")
    assert prepared[0].params["square_pow"] != prepared[1].params["square_pow"]
    assert relative_times(compiled, "dds_on", "dds1") == [0]
    assert relative_times(compiled, "dds_off", "dds1") == [8000]
    assert verify_equivalence(compiled)["status"] == "passed"


def test_different_latencies_change_commit_times_not_output_alignment():
    qs, lab = timed_lab(2, latencies=[80, 96])
    exp = Experiment("latency_compensated_square", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qs:
                    play(q.raman, Square(4 * us))
    compiled = lab.compile(exp)
    assert relative_times(compiled, "dds_commit", "dds0") == [-80]
    assert relative_times(compiled, "dds_commit", "dds1") == [-96]
    assert relative_times(compiled, "dds_effect", "dds0") == [0]
    assert relative_times(compiled, "dds_effect", "dds1") == [0]
    assert verify_equivalence(compiled)["status"] == "passed"


def test_frame_commands_update_live_hardware_at_written_times():
    (q,), lab = timed_lab()
    exp = Experiment("live_phase", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                play(q.raman, Square(12 * us, frequency=125e6, phase=0.25))
                with exp.serial():
                    delay(q.readout, 4 * us)
                    shift_phase(q.uid, math.pi / 2)
                    delay(q.readout, 4 * us)
                    reset_frame(q.uid)
    compiled = lab.compile(exp)
    assert relative_times(compiled, "dds_effect") == [0, 4000, 8000]
    assert [e.params["phase"] for e in events(compiled, "dds_effect")] == pytest.approx(
        [0.25, 0.25 + math.pi / 2, 0.25]
    )
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [12000]
    assert verify_equivalence(compiled)["status"] == "passed"


def test_frame_change_with_no_following_play_is_not_erased():
    (q,), lab = timed_lab()
    exp = Experiment("phase_after_play", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, Square(4 * us, frequency=125e6, phase=0.25))
            delay(q.raman, 2 * us)
            shift_phase(q.uid, 0.5)
    compiled = lab.compile(exp)
    assert relative_times(compiled, "dds_effect") == [0, 6000]
    assert events(compiled, "dds_effect")[-1].params["phase"] == pytest.approx(0.75)
    assert relative_times(compiled, "dds_off") == [4000]
    assert compiled.points[0].duration_mu == 6000
    assert verify_equivalence(compiled)["status"] == "passed"


def test_frame_at_play_boundary_is_applied_once():
    (q,), lab = timed_lab()
    exp = Experiment("phase_at_boundary", [q])
    pulse = Square(4 * us, frequency=125e6, phase=0.25)
    with exp.average(1):
        with exp.realtime():
            shift_phase(q.uid, 0.5)
            play(q.raman, pulse)
            reset_frame(q.uid)
            play(q.raman, pulse)
    compiled = lab.compile(exp)
    assert relative_times(compiled, "dds_effect") == [0, 4000]
    assert [e.params["phase"] for e in events(compiled, "dds_effect")] == pytest.approx(
        [0.75, 0.25]
    )
    assert verify_equivalence(compiled)["status"] == "passed"


def test_same_time_frame_commands_commit_the_final_state():
    (q,), lab = timed_lab()
    exp = Experiment("coalesced_frame_commands", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, Square(4 * us, frequency=125e6, phase=0.25))
            shift_phase(q.uid, 0.2)
            shift_phase(q.uid, 0.3)
    compiled = lab.compile(exp)
    assert relative_times(compiled, "dds_effect") == [0, 4000]
    assert events(compiled, "dds_effect")[-1].params["phase"] == pytest.approx(0.75)
    assert verify_equivalence(compiled)["status"] == "passed"


def test_frame_only_commands_update_mapped_dds_without_rf_output():
    (q,), lab = timed_lab()
    lab.setup.devices["rf"] = replace(lab.setup.devices["dds0"], name="rf")
    lab.signal_map[q.rf] = "rf"
    exp = Experiment("frame_only", [q])
    with exp.average(1):
        with exp.realtime():
            shift_phase(q.uid, 0.5)
            delay(q.readout, 4 * us)
            reset_frame(q.uid)
    compiled = lab.compile(exp)
    for name in ("dds0", "rf"):
        assert relative_times(compiled, "dds_effect", name) == [0, 4000]
        assert [
            e.params["phase"] for e in events(compiled, "dds_effect", name)
        ] == pytest.approx([0.5, 0])
    assert not events(compiled, "dds_on")
    assert not events(compiled, "dds_off")
    assert verify_equivalence(compiled)["status"] == "passed"


def test_frame_on_legacy_channel_is_explicitly_rejected():
    (q,), lab = timed_lab()
    lab.setup.devices["dds0"] = replace(
        lab.setup.devices["dds0"], square_latency_mu=None
    )
    exp = Experiment("unsupported_hardware_frame", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, Square(4 * us))
            shift_phase(q.uid, 0.5)
    with pytest.raises(CompileError):
        lab.compile(exp)


def test_prepare_budget_below_real_driver_minimum_is_rejected():
    (q,), lab = timed_lab()
    lab.setup.devices["dds0"] = replace(
        lab.setup.devices["dds0"], square_prepare_duration_mu=1232
    )
    exp = Experiment("undersized_prepare", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, Square(4 * us))
    with pytest.raises(CompileError):
        lab.compile(exp)


def test_commit_grid_is_checked_without_moving_output_time():
    with pytest.raises(CompileError):
        adjacent({"phase": 0.5}, duration=4004 * ns)


@pytest.mark.parametrize("operation", ["shift", "reset"])
def test_frame_coalesced_with_first_play_cannot_move_to_earlier_grid(operation):
    (q,), lab = timed_lab()
    exp = Experiment("exact_frame_boundary", [q])
    with exp.average(1):
        with exp.realtime():
            delay(q.raman, 4 * ns)
            if operation == "shift":
                shift_phase(q.uid, 0.5)
            else:
                reset_frame(q.uid)
            play(q.raman, Square(4 * us, frequency=125e6))
    # The RF gate can begin at 4 ns, but an explicit hardware phase command
    # cannot silently take effect at 0 ns to accommodate the update grid.
    with pytest.raises(CompileError, match="configuration grid"):
        lab.compile(exp)


def test_no_op_reset_coalesced_with_play_still_commits_hardware():
    (q,), lab = timed_lab()
    exp = Experiment("explicit_no_op_reset", [q])
    pulse = Square(4 * us, frequency=125e6, phase=0.25)
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse)
            reset_frame(q.uid)
            play(q.raman, pulse)
    compiled = lab.compile(exp)
    assert relative_times(compiled, "dds_effect") == [0, 4000]
    assert relative_times(compiled, "dds_commit") == [-80, 3920]
    assert [e.params["phase"] for e in events(compiled, "dds_effect")] == pytest.approx(
        [0.25, 0.25]
    )
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [8000]
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("lanes", [8, 16])
def test_simultaneous_ttl_edges_obey_declared_lane_count_without_staggering(lanes):
    devices = {
        f"ttl{i}": Device(f"ttl{i}", "ttl", grid_mu=1, min_duration_mu=1)
        for i in range(9)
    }
    lab = LabConfig(
        DeviceSetup(devices, rtio_coarse_mu=8, rtio_lane_count=lanes),
        SignalMap({name: name for name in devices}),
    )
    exp = Experiment("nine_simultaneous_edges", [])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for name in devices:
                    play(name, Square(80 * ns, amplitude=1, frequency=0))
    if lanes == 8:
        with pytest.raises(CompileError, match="lane"):
            lab.compile(exp)
    else:
        compiled = lab.compile(exp)
        assert relative_times(compiled, "ttl_on") == [0] * 9
        assert relative_times(compiled, "ttl_off") == [80] * 9
        assert compiled.points[0].duration_mu == 80
        assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("resource", ["kernel_submit", "board0"])
@pytest.mark.parametrize("start", [3920, 3924])
def test_reservation_covering_commit_is_rejected_even_when_prepare_fits(
    resource, start
):
    baseline = adjacent({"phase": 0.5})
    point = baseline.points[0]
    assert events(baseline, "dds_prepare")[-1].end_mu - point.origin_mu <= 3920
    (q,), lab = timed_lab()
    exp = Experiment("reserved_commit", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(q.raman, Square(4 * us, frequency=125e6))
                    play(q.raman, Square(4 * us, frequency=125e6, phase=0.5))
                with exp.serial():
                    delay(q.readout, start * ns)
                    reserve(resource, 4 * ns)
    with pytest.raises(CompileError, match="commit.*reservation"):
        lab.compile(exp)


def test_repeated_gates_preserve_duration_and_one_continuous_output():
    (q,), lab = timed_lab()
    exp = Experiment("long_square_word", [q])
    pulse = Square(4 * us, frequency=125e6)
    with exp.average(2):
        with exp.realtime():
            with exp.repeat(32):
                play(q.raman, pulse)
                play(q.raman, pulse, phase=math.pi / 2)
    compiled = lab.compile(exp)
    assert compiled.points[0].duration_mu == 256000
    assert relative_times(compiled, "dds_effect") == list(range(0, 256000, 4000))
    assert relative_times(compiled, "dds_on") == [0]
    assert relative_times(compiled, "dds_off") == [256000]
    assert verify_equivalence(compiled)["status"] == "passed"


def test_square_timing_example_builders_are_offline_and_verifiable():
    path = Path(__file__).resolve().parents[1] / "examples" / "square_timing.py"
    module = runpy.run_path(str(path))
    for builder in ("build_continuous_pair", "build_live_phase"):
        lab, exp = module[builder]()
        compiled = lab.compile(exp)
        assert verify_equivalence(compiled)["status"] == "passed"
