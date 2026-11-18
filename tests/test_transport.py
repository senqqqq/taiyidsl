"""Transport follows executed structure and preserves shared-field semantics."""

import copy
import json
from dataclasses import replace

import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Position,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    SweepParameter,
    add,
    play,
    section,
    shift_phase,
    trigplay,
    us,
)
from taiyidsl.backends import get_backend
from taiyidsl.dsl import json_value, move
from taiyidsl.shared_fields import validate_shared_fields
from taiyidsl.transport import IZ, MoveProfile, TransportConfig


def transport_lab(*, shared_y=True):
    def qubit(uid, x, y):
        return Qubit(
            uid,
            signals={"rf": f"{uid}.rf", "clock": f"{uid}.clock"},
            parameters={
                "rf": {"frequency": 30_000, "amplitude": 0.2, "frame": "spin"},
                "clock": {"frequency": 1e6, "amplitude": 0.2, "frame": "clock"},
            },
            position=Position(x * 1e-6, y * 1e-6, 0, "tweezer_plane"),
        )

    q0, q1, spectator = (
        qubit("q0", 0, 0),
        qubit("q1", 6, 0 if shared_y else 6),
        qubit("spectator", 50, 20),
    )
    zone = IZ(
        "iz0",
        (
            Position(30e-6, 10e-6, 0, "tweezer_plane"),
            Position(36e-6, (10 if shared_y else 16) * 1e-6, 0, "tweezer_plane"),
        ),
    )
    cards = {
        "A": SpectrumCard("SIM_A", (0,)),
        "B": SpectrumCard("SIM_B", (0,)),
        "C": SpectrumCard("SIM_C", mode="dds", dds_trigger_latency_ns=320),
    }
    outputs = {
        "rf": SpectrumOutput("A", 0),
        "clock": SpectrumOutput("B", 0),
        "aod_x": SpectrumOutput("C", 0),
        "aod_y": SpectrumOutput("C", 1),
    }
    devices = {name: Device(name, "spectrum") for name in outputs}
    devices["trigger"] = Device("trigger", "ttl", grid_mu=1)
    members = (q0, q1, spectator)
    mapping = SignalMap(
        {q.signal(role): role for q in members for role in ("rf", "clock")}
    )
    setup = DeviceSetup(
        devices,
        spectrum=SpectrumConfig(
            {"g": SpectrumGroup("trigger", cards, outputs)},
            {
                q.signal(role): q.drive_frame(role)
                for q in members
                for role in q.drive_names
            },
        ),
    )
    setup._shared_outputs = {"rf": tuple(q.rf for q in members)}
    setup._zone_coverage = {"clock": (zone.uid,)}
    amplitudes = {0: 0.08, 1: 0.08, 8: 0.1}
    if not shared_y:
        amplitudes[9] = 0.1
    transport = TransportConfig(
        card="C",
        x_calibration=((0, 70e6), (60e-6, 100e6)),
        y_calibration=((0, 70e6), (30e-6, 100e6)),
        bindings={
            "q0": {"x_core": 0, "y_core": 8},
            "q1": {"x_core": 1, "y_core": 8 if shared_y else 9},
        },
        initial_amplitudes=amplitudes,
        zones=(zone,),
    )
    return (
        LabConfig(
            setup, mapping, "artiq_spectrum", qubits=members, transport=transport
        ),
        q0,
        q1,
        spectator,
        zone,
    )


def scheduled(lab, exp, coordinates=None):
    exp, setup = lab._compilation_inputs(exp)
    target = get_backend("artiq_spectrum")
    setup = target.prepare_setup(copy.deepcopy(setup))
    root = copy.deepcopy(exp.root)
    validate_shared_fields(root, setup, lab.signal_map, exp.qubits)
    body = root.children[0].children
    while len(body) == 1 and body[0].kind == "sweep":
        body = body[0].children
    return target.schedule(
        exp, body, setup, lab.signal_map, coordinates or {}, False, [], 10000
    )[0]


def test_batch_move_tracks_sources_zone_occupancy_and_global_rf():
    lab, q0, q1, spectator, zone = transport_lab()
    initial = q0.to_dict(), q1.to_dict()
    pulse = Square(1.024 * us, amplitude=0.2, frequency=1e6)
    exp = Experiment("transport", [q0, q1])
    with exp.average(2), exp.realtime(), exp.triggered_sequence(group="g"):
        move((q0, q1), to=(zone[0], zone[1]), duration=100 * us, settle=10.24 * us)
        play(q0.clock, pulse)
        play(q0.rf, pulse)
        move((q0, q1), to=(q0.position, q1.position), duration=100 * us)
    events = scheduled(lab, exp)
    moves = [e for e in events if e.kind == "move"]
    assert (q0.to_dict(), q1.to_dict()) == initial
    assert moves[1].params["initial_layout"] == moves[0].params["final_layout"]
    assert moves[1].params["final_layout"]["q0"] == q0.position.to_dict()
    assert moves[0].duration_mu == 551200
    optical = next(e for e in events if e.kind == "play" and e.signal == q0.clock)
    rf = next(e for e in events if e.kind == "play" and e.signal == q0.rf)
    assert optical.start_mu == moves[0].end_mu
    assert optical.params["operation_qubits"] == ["q0", "q1"]
    assert rf.params["operation_qubits"] == ["q0", "q1", spectator.uid]
    assert spectator.uid in moves[0].params["initial_layout"]
    assert len(moves[0].params["initial_tones"]) == 3
    assert moves[1].params["initial_tones"] == moves[0].params["knots"][-1]["tones"]
    json.dumps(json_value(exp.root))


def test_static_cases_repeats_and_reused_sections_resolve_each_occurrence():
    lab, q0, q1, _, zone = transport_lab()
    pair, homes = (q0, q1), (q0.position, q1.position)
    with section("return") as return_home:
        move(pair, to=homes, duration=100 * us)
    choice = SweepParameter("choice", [0, 1])
    exp = Experiment("cases", pair)
    with (
        exp.average(1),
        exp.sweep(choice),
        exp.realtime(),
        exp.triggered_sequence(group="g"),
    ):
        with exp.repeat(2):
            with exp.case(choice, 0):
                move(pair, to=(zone[0], zone[1]), duration=100 * us)
            with exp.case(choice, 1):
                move(
                    pair,
                    to=(
                        Position(20e-6, 5e-6, 0, "tweezer_plane"),
                        Position(26e-6, 5e-6, 0, "tweezer_plane"),
                    ),
                    duration=100 * us,
                )
            add(return_home)
    for value, expected in ((0, 30e-6), (1, 20e-6)):
        events = [e for e in scheduled(lab, exp, {"choice": value}) if e.kind == "move"]
        assert len(events) == 4
        assert events[1].params["initial_layout"]["q0"]["x"] == pytest.approx(expected)
        assert events[3].params["initial_layout"] == events[1].params["initial_layout"]
        assert events[2].params["initial_layout"] == events[0].params["initial_layout"]
    assert q0.position.x == 0


@pytest.mark.parametrize("schedule", ["parallel", "asap"])
def test_move_rejects_unordered_sections(schedule):
    lab, q0, q1, _, zone = transport_lab()
    exp = Experiment("unordered", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section("unordered", schedule=schedule, qubits=(q0, q1)):
            move((q0, q1), to=(zone[0], zone[1]), duration=100 * us)
    with pytest.raises(CompileError, match="serial scheduling"):
        scheduled(lab, exp)


def test_shared_axis_cannot_silently_move_omitted_spectator():
    lab, q0, _, _, zone = transport_lab()
    exp = Experiment("one_atom", [q0])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        move(q0, to=zone[0], duration=100 * us)
    with pytest.raises(CompileError, match="sharing DDS core"):
        scheduled(lab, exp)


def test_swept_path_checks_stationary_spectators_between_endpoints():
    lab, q0, q1, _, _ = transport_lab()
    exp = Experiment("path_collision", [q0, q1])
    targets = (
        Position(54e-6, 21.6e-6, 0, "tweezer_plane"),
        Position(60e-6, 21.6e-6, 0, "tweezer_plane"),
    )
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        move((q0, q1), to=targets, duration=100 * us)
        move((q0, q1), to=(q0.position, q1.position), duration=100 * us)
    with pytest.raises(CompileError, match="minimum separation"):
        scheduled(lab, exp)


def test_routing_and_frequency_tolerance_are_checked_before_output():
    lab, q0, q1, _, zone = transport_lab()
    exp = Experiment("tight_tolerance", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        move((q0, q1), to=(zone[0], zone[1]), duration=100 * us)
        move((q0, q1), to=(q0.position, q1.position), duration=100 * us)
    lab.transport = replace(lab.transport, frequency_tolerance_hz=1e-8)
    with pytest.raises(CompileError, match="frequency exceeds transport tolerance"):
        scheduled(lab, exp)
    lab.transport = replace(
        lab.transport,
        frequency_tolerance_hz=1,
        bindings={"q0": {"x_core": 10, "y_core": 8}, "q1": {"x_core": 1, "y_core": 8}},
        initial_amplitudes={10: 0.08, 1: 0.08, 8: 0.1},
    )
    with pytest.raises(CompileError, match="not routed to channel"):
        scheduled(lab, exp)


@pytest.mark.parametrize("failure", ["open", "cross", "frame", "range", "z"])
def test_transport_rejects_invalid_physical_paths_and_open_shots(failure):
    lab, q0, q1, _, zone = transport_lab()
    targets = (zone[0], zone[1])
    if failure == "cross":
        targets = (zone[1], zone[0])
    elif failure == "frame":
        targets = (Position(30e-6, 10e-6, 0, "camera"), zone[1])
    elif failure == "range":
        targets = (Position(100e-6, 10e-6, 0, "tweezer_plane"), zone[1])
    elif failure == "z":
        targets = (Position(30e-6, 10e-6, 1e-6, "tweezer_plane"), zone[1])
    exp = Experiment("invalid", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        move((q0, q1), to=targets, duration=100 * us)
    message = {
        "open": "return every",
        "cross": "coincide or cross",
        "frame": "different coordinate",
        "range": "outside calibrated",
        "z": "cannot change z",
    }[failure]
    with pytest.raises(CompileError, match=message):
        scheduled(lab, exp)


def test_profile_calibration_snapshot_and_compensated_knots():
    lab, q0, q1, _, zone = transport_lab()
    lab.transport = replace(
        lab.transport, x_calibration=((0, 70e6, 1), (60e-6, 100e6, 1.5))
    )
    exp = Experiment("profile", [q0, q1])
    profile = MoveProfile.minimum_jerk(segments=8)
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        move((q0, q1), to=(zone[0], zone[1]), duration=100 * us, profile=profile)
        move((q0, q1), to=(q0.position, q1.position), duration=100 * us)
    move_event = next(e for e in scheduled(lab, exp) if e.kind == "move")
    knots = move_event.params["knots"]
    assert len(knots) == 9
    assert all(k["offset_mu"] % 32 == 0 for k in knots)
    timing = move_event.params["timing_quantization"]
    assert [item["requested_offset_mu"] for item in timing] == [
        62500 * i for i in range(9)
    ]
    assert [item["offset_mu"] for item in timing] == [k["offset_mu"] for k in knots]
    assert timing[1]["error_mu"] == -4
    assert timing[1]["error_s"] == pytest.approx(-0.8e-9)
    assert move_event.params["max_timing_quantization_error_mu"] == 16
    assert move_event.params["max_timing_quantization_error_s"] == pytest.approx(3.2e-9)
    assert move_event.params["trajectory_model"] == "piecewise_linear_frequency"
    assert knots[-1]["tones"][0]["amplitude"] == pytest.approx(0.1)
    assert knots[0]["tones"][0]["amplitude"] == pytest.approx(0.08)
    restored = LabConfig.from_dict(json.loads(json.dumps(lab.to_dict())))
    assert restored.transport == lab.transport
    assert restored.setup.zone_coverage == lab.setup.zone_coverage
    captured, setup = lab._compilation_inputs(exp)
    assert setup._transport is not lab.transport
    lab.transport.bindings["q0"]["x_core"] = 7
    assert setup._transport.bindings["q0"]["x_core"] == 0
    assert captured.qubits["q0"].position == q0.position


def test_optical_targets_are_an_assertion_and_source_must_be_inside_zone():
    lab, q0, q1, _, zone = transport_lab()
    pulse = Square(1.024 * us, amplitude=0.2, frequency=1e6)
    exp = Experiment("wrong_targets", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        move((q0, q1), to=(zone[0], zone[1]), duration=100 * us)
        play(q0.clock, pulse, targets=(q0,))
        move((q0, q1), to=(q0.position, q1.position), duration=100 * us)
    with pytest.raises(CompileError, match="current zone occupancy"):
        scheduled(lab, exp)
    exp = Experiment("outside", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        play(q0.clock, pulse)
    with pytest.raises(CompileError, match="outside illuminated"):
        scheduled(lab, exp)


@pytest.mark.parametrize("operation", ["play", "trigplay", "shift_phase"])
def test_native_dds_outputs_reject_gate_and_frame_ops_even_in_inactive_case(operation):
    lab, q0, q1, _, _ = transport_lab()
    lab.signal_map[q0.rf] = "aod_x"
    lab.setup._shared_outputs = {}
    pulse = Square(1.024 * us, amplitude=0.2, frequency=80e6)
    exp = Experiment("unsupported_dds_gate", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.repeat(0):
            if operation == "shift_phase":
                shift_phase(q0.drive_frame("rf"), 0.2)
            else:
                {"play": play, "trigplay": trigplay}[operation](q0.rf, pulse)
    with pytest.raises(CompileError, match="cannot target native DDS transport"):
        scheduled(lab, exp)
