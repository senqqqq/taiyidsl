"""Virtual Z is a logical rotation with explicit residual frame semantics."""

import math
from dataclasses import replace

import numpy as np
import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    Rz,
    SignalMap,
    Square,
    SweepParameter,
    delay,
    ns,
    play,
    rad,
    reset_frame,
    rz,
    shift_phase,
    turn,
    us,
)
from taiyidsl.backends.artiq.verification import verify_equivalence, verify_export
from taiyidsl.compiler.scheduling import resolve_virtual_z
from taiyidsl.ir import ScheduledEvent


def lab_for(qubits, *, carrier_latency=112):
    devices = {
        f"dds{i}": Device(
            f"dds{i}",
            "dds",
            bus=f"spi{i}",
            update_group=f"board{i}",
            square_latency_mu=80,
            nco_mode="continuous",
            nco_epoch_latency_mu=80,
            ram_latency_mu=112,
            ram_pow_latency_mu=carrier_latency,
            ram_rearm_duration_mu=4000,
            ram_phase_update_duration_mu=480,
        )
        for i, _ in enumerate(qubits)
    }
    return LabConfig(
        DeviceSetup(devices),
        SignalMap({q.raman: f"dds{i}" for i, q in enumerate(qubits)}),
    )


def envelope(duration=800 * ns):
    return FlatTopGaussian(
        0,
        sigma=duration / 10,
        cutoff_sigma=5,
        sample_interval=4 * ns,
        amplitude=0.5,
        frequency=83e6,
    )


def virtual_events(compiled, point=0):
    return [e for e in compiled.points[point].logical_events if e.kind == "rz"]


def drives(compiled, point=0):
    return [e for e in compiled.points[point].logical_events if e.kind == "play"]


def test_public_alias_and_input_validation():
    assert Rz is rz
    q = Qubit("q0")
    exp = Experiment("validation", [q])
    with exp.average(1):
        with exp.realtime():
            with pytest.raises(TypeError, match="Qubit"):
                rz(q.frame, 0.2)
            for invalid in (float("nan"), float("inf"), 1 * ns):
                with pytest.raises((ValueError, TypeError)):
                    rz(q, invalid)
            rz(q, -0.25 * turn)
    compiled = lab_for([q]).compile(exp)
    assert virtual_events(compiled)[0].params["angle"] == pytest.approx(-math.pi / 2)
    assert compiled.points[0].duration_mu == 0
    effects = [e for e in compiled.points[0].physical_events if e.kind == "dds_effect"]
    assert len(effects) == 1
    assert effects[0].start_mu == compiled.points[0].origin_mu
    assert effects[0].params["phase"] == pytest.approx(math.pi / 2)
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_standalone_rz_updates_both_mapped_drives_without_a_play():
    q = Qubit("q0")
    lab = lab_for([q])
    lab.setup.devices["rf"] = replace(
        lab.setup.devices["dds0"], name="rf", bus="spi1", update_group="board1"
    )
    lab.signal_map[q.rf] = "rf"
    exp = Experiment("standalone_two_drive_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
    compiled = lab.compile(exp)
    point = compiled.points[0]
    effects = [e for e in point.physical_events if e.kind == "dds_effect"]
    assert {e.signal for e in effects} == {"dds0", "rf"}
    assert {e.start_mu - point.origin_mu for e in effects} == {0}
    assert all(e.params["phase"] == pytest.approx(3 * math.pi / 2) for e in effects)
    assert not any(e.kind == "dds_on" for e in point.physical_events)
    assert verify_equivalence(compiled)["status"] == "passed"


def test_repeated_standalone_rz_keeps_one_known_rf_off_state():
    q = Qubit("q0")
    exp = Experiment("two_standalone_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, 0.2)
        delay(q.raman, 1600 * ns)
        rz(q, 0.3)
    compiled = lab_for([q]).compile(exp)
    point = compiled.points[0]
    effects = [e for e in point.physical_events if e.kind == "dds_effect"]
    assert [e.start_mu - point.origin_mu for e in effects] == [0, 1600]
    assert [e.params["phase"] for e in effects] == pytest.approx(
        [2 * math.pi - 0.2, 2 * math.pi - 0.5]
    )
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("duration", [200 * ns, 800 * ns])
def test_initial_and_terminal_rz_have_hardware_effect_without_later_play(duration):
    q = Qubit("q0")
    exp = Experiment("initial_terminal_virtual_z", [q])
    with exp.average(1):
        with exp.realtime():
            rz(q, 0.3)
            play(q.raman, envelope(duration))
            Rz(q, -0.7)
    if duration == 200 * ns:
        with pytest.raises(CompileError, match="(?i)(preparation|window)"):
            lab_for([q]).compile(exp)
        return
    compiled = lab_for([q]).compile(exp)
    point = compiled.points[0]
    assert len([e for e in point.physical_events if e.kind == "dds_ram_start"]) == 2
    assert point.duration_mu == round(duration.value / ns.value)
    assert drives(compiled)[0].params["phase"] == pytest.approx(-0.3)
    assert drives(compiled)[0].params["frame_phase"] == pytest.approx(-0.3)
    assert virtual_events(compiled)[-1].params[
        "accumulated_virtual_z"
    ] == pytest.approx(-0.4)
    assert any(e.params.get("ram_frame_only") for e in point.physical_events)
    final = compiled.virtual_z_frames()
    assert final == pytest.approx({"q0": -0.4})
    final["q0"] = 99
    assert compiled.virtual_z_frames() == pytest.approx({"q0": -0.4})
    assert verify_equivalence(compiled)["status"] == "passed"


def test_virtual_z_next_ram_pulse_has_real_phase_preparation():
    q = Qubit("q0")
    exp = Experiment("virtual_phase_is_programmed", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, envelope())
            rz(q, math.pi / 2)
            play(q.raman, envelope())
    compiled = lab_for([q]).compile(exp)
    launches = [
        e for e in compiled.points[0].physical_events if e.kind == "dds_ram_start"
    ]
    assert len(launches) == 2
    assert launches[1].params["phase"] == pytest.approx(3 * math.pi / 2)
    assert (
        len(
            [e for e in compiled.points[0].physical_events if e.kind == "dds_ram_phase"]
        )
        == 1
    )
    assert compiled.points[0].duration_mu == 1600
    assert verify_equivalence(compiled)["status"] == "passed"


def test_virtual_z_does_not_hide_200ns_phase_preparation_bandwidth():
    q = Qubit("q0")
    exp = Experiment("virtual_phase_still_needs_bandwidth", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, envelope(200 * ns))
            rz(q, math.pi / 2)
            play(q.raman, envelope(200 * ns))
    # Equal declared delays isolate the bandwidth failure from zero-tail guards.
    with pytest.raises(CompileError, match="(?i)(prepar|window|fit|commit)"):
        lab_for([q], carrier_latency=112).compile(exp)


def test_repeat_sweep_and_zero_repeat_reset_virtual_state_per_point():
    q = Qubit("q0")
    angle = SweepParameter("angle", [0.2, -0.4], unit="rad")
    exp = Experiment("virtual_sweep", [q])
    with exp.average(2):
        with exp.sweep(angle):
            with exp.realtime():
                with exp.repeat(0):
                    rz(q, math.pi)
                with exp.repeat(3):
                    rz(q, angle)
                play(q.raman, envelope())
    compiled = lab_for([q]).compile(exp)
    for index, value in enumerate((0.2, -0.4)):
        assert len(virtual_events(compiled, index)) == 3
        assert drives(compiled, index)[0].params["phase"] == pytest.approx(-3 * value)
        assert compiled.virtual_z_frames(index) == pytest.approx({"q0": 3 * value})
    for invalid in (-1, 2, True):
        with pytest.raises(IndexError):
            compiled.virtual_z_frames(invalid)
    assert verify_equivalence(compiled)["status"] == "passed"


def test_frame_reset_clears_shifts_and_virtual_rotations():
    q = Qubit("q0")
    exp = Experiment("unified_phase_frame", [q])
    with exp.average(1):
        with exp.realtime():
            shift_phase(q.frame, 0.3)
            rz(q, 0.2)
            play(q.raman, envelope())
            reset_frame(q.frame)
            play(q.raman, envelope())
    compiled = lab_for([q]).compile(exp)
    assert [e.params["phase"] for e in drives(compiled)] == pytest.approx([0.1, 0])
    assert [e.params["frame_phase"] for e in drives(compiled)] == pytest.approx(
        [0.1, 0]
    )
    assert [e.params["virtual_z_phase"] for e in drives(compiled)] == pytest.approx(
        [0.2, 0]
    )
    assert compiled.frame_phases() == pytest.approx({"q0": 0})
    assert compiled.virtual_z_frames() == pytest.approx({"q0": 0})
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("virtual_first", [False, True])
@pytest.mark.parametrize("kind", ["square", "ram"])
def test_hardware_command_before_first_play_cannot_use_future_virtual_offset(
    virtual_first,
    kind,
):
    q = Qubit("q0")
    exp = Experiment("hardware_before_first_virtual_play", [q])
    with exp.average(1):
        with exp.realtime():
            if virtual_first:
                rz(q, 0.2)
            shift_phase(q.frame, 0.3)
            delay(q.raman, 4 * us)
            if not virtual_first:
                rz(q, 0.2)
            pulse = Square(800 * ns, frequency=83e6) if kind == "square" else envelope()
            play(q.raman, pulse, phase=0.1)
    compiled = lab_for([q]).compile(exp)
    effect_kind = "dds_effect" if kind == "square" else "dds_ram_effect"
    effects = [e for e in compiled.points[0].physical_events if e.kind == effect_kind]
    assert [e.params["phase"] for e in effects] == pytest.approx(
        [0.1 if virtual_first else 0.3, 0.2]
    )
    assert verify_equivalence(compiled)["status"] == "passed"


def test_virtual_frame_getter_detects_modified_compiled_state():
    q = Qubit("q0")
    exp = Experiment("virtual_snapshot", [q])
    with exp.average(1):
        with exp.realtime():
            rz(q, 0.2)
    compiled = lab_for([q]).compile(exp)
    virtual_events(compiled)[0].params["accumulated_virtual_z"] = 99
    with pytest.raises(CompileError, match="modified"):
        compiled.virtual_z_frames()


def test_virtual_z_does_not_shift_a_ttl_readout_play():
    q = Qubit("q0")
    lab = lab_for([q])
    lab.setup.devices["readout"] = Device(
        "readout", "ttl", grid_mu=1, min_duration_mu=1
    )
    lab.signal_map[q.readout] = "readout"
    exp = Experiment("virtual_z_ttl_readout", [q])
    with exp.average(1):
        with exp.realtime():
            rz(q, 0.3)
            play(q.readout, Square(200 * ns))
    compiled = lab.compile(exp)
    assert drives(compiled)[0].params["phase"] == 0
    assert "virtual_z_drive_offset" not in drives(compiled)[0].params
    assert [
        e.kind for e in compiled.points[0].physical_events if e.signal == "readout"
    ] == ["ttl_on", "ttl_off"]
    assert any(e.kind == "dds_effect" for e in compiled.points[0].physical_events)
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_independent_illumination_can_overlap_virtual_z_in_parallel():
    q = Qubit("q0")
    lab = lab_for([q])
    lab.setup.devices["light"] = Device("light", "ttl", grid_mu=1, min_duration_mu=1)
    lab.signal_map["q0.illumination"] = "light"
    exp = Experiment("virtual_z_during_illumination", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    delay(q.raman, 104 * ns)
                    rz(q, 0.3)
                play("q0.illumination", Square(800 * ns))
    compiled = lab.compile(exp)
    assert drives(compiled)[0].params["phase"] == 0
    assert virtual_events(compiled)[0].start_mu == 104
    assert compiled.virtual_z_frames() == pytest.approx({"q0": 0.3})
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_virtual_z_applies_to_defined_rf_drive_role():
    q = Qubit("q0")
    lab = lab_for([q])
    lab.signal_map[q.rf] = "dds0"
    exp = Experiment("virtual_z_rf", [q])
    with exp.average(1):
        with exp.realtime():
            rz(q, 0.3)
            play(q.rf, Square(200 * ns))
    compiled = lab.compile(exp)
    assert drives(compiled)[0].params["phase"] == pytest.approx(-0.3)
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_independent_qubit_parallel_branches_keep_separate_virtual_frames():
    qs = [Qubit("q0"), Qubit("q1")]
    exp = Experiment("parallel_virtual_z", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for index, q in enumerate(qs):
                    with exp.serial():
                        rz(q, 0.2 + index)
                        play(q.raman, envelope())
            rz(qs[0], 0.3)
    compiled = lab_for(qs).compile(exp)
    assert [e.params["phase"] for e in drives(compiled)] == pytest.approx([-0.2, -1.2])
    assert virtual_events(compiled)[-1].params[
        "accumulated_virtual_z"
    ] == pytest.approx(0.5)
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_ambiguous_same_qubit_parallel_virtual_write_is_rejected():
    q = Qubit("q0")
    exp = Experiment("ambiguous_virtual_z", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                rz(q, 0.2)
                play(q.raman, envelope())
    with pytest.raises(CompileError, match="serial branch"):
        lab_for([q]).compile(exp)


def test_generic_resolution_rejects_a_virtual_z_inside_active_pulse():
    logical = [
        ScheduledEvent("play", "q0.raman", 0, 800, {"qubit": "q0", "phase": 0}),
        ScheduledEvent("rz", "q0", 400, 0, {"angle": 0.2}),
    ]
    with pytest.raises(CompileError, match="inside an active"):
        resolve_virtual_z(logical)


def rotation_z(angle):
    return np.diag([np.exp(-0.5j * angle), np.exp(0.5j * angle)])


def rotation_xy(phase, angle):
    axis = np.array([[0, np.exp(-1j * phase)], [np.exp(1j * phase), 0]])
    return np.cos(angle / 2) * np.eye(2) - 1j * np.sin(angle / 2) * axis


@pytest.mark.parametrize("angle", [0.37, -0.91, math.pi / 2, 2 * math.pi + 0.3])
def test_matrix_equivalence_includes_terminal_frame_and_y_sign(angle):
    q = Qubit("q0")
    exp = Experiment("virtual_matrix", [q])
    phases = (math.pi / 2, -0.27, 0.61)
    rotations = (0.63, -1.14, 2.17)
    with exp.average(1):
        with exp.realtime():
            play(q.raman, envelope(), phase=phases[0])
            rz(q, angle * rad)
            play(q.raman, envelope(), phase=phases[1])
            rz(q, -0.23)
            play(q.raman, envelope(), phase=phases[2])
            rz(q, 0.17)
    compiled = lab_for([q]).compile(exp)
    logical = (
        rotation_z(0.17)
        @ rotation_xy(phases[2], rotations[2])
        @ rotation_z(-0.23)
        @ rotation_xy(phases[1], rotations[1])
        @ rotation_z(angle)
        @ rotation_xy(phases[0], rotations[0])
    )
    physical = np.eye(2, dtype=complex)
    for event, rotation in zip(drives(compiled), rotations):
        physical = rotation_xy(event.params["phase"], rotation) @ physical
    residual = virtual_events(compiled)[-1].params["accumulated_virtual_z"]
    np.testing.assert_allclose(rotation_z(residual) @ physical, logical, atol=2e-15)
    # Computational-basis readout is invariant under the residual Z frame.
    state = np.array([np.sqrt(0.3), np.sqrt(0.7) * np.exp(0.4j)])
    np.testing.assert_allclose(
        abs(logical @ state) ** 2, abs(physical @ state) ** 2, atol=2e-15
    )
    # An equatorial observable must be interpreted in phase phi - residual.
    phi = 0.39

    def observable(phase):
        return np.array([[0, np.exp(-1j * phase)], [np.exp(1j * phase), 0]])

    logical_state, physical_state = logical @ state, physical @ state
    expected = np.vdot(logical_state, observable(phi) @ logical_state)
    actual = np.vdot(physical_state, observable(phi - residual) @ physical_state)
    np.testing.assert_allclose(actual, expected, atol=2e-15)
