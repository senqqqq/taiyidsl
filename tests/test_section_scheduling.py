"""Resource ordering, indivisible operations and final-time phase resolution."""

import math

import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
    Square,
    barrier,
    delay,
    ns,
    parallel,
    play,
    qubit_operation,
    repeat,
    reserve,
    reset_frame,
    rz,
    section,
    shift_phase,
)
from taiyidsl.compiler.scheduling import resolve_frames


def lab_for(*, alias=False):
    outputs = {
        "rf0": SpectrumOutput("A", 0),
        "raman0": SpectrumOutput("A", 1),
        "rf1": SpectrumOutput("B", 0),
    }
    group = SpectrumGroup(
        trigger_device="ttl",
        cards={"A": SpectrumCard("SIM_A"), "B": SpectrumCard("SIM_B", (0,))},
        outputs=outputs,
    )
    return LabConfig(
        DeviceSetup(
            {
                **{name: Device(name, "spectrum") for name in outputs},
                "ttl": Device("ttl", "ttl"),
            },
            spectrum=SpectrumConfig({"g": group}),
        ),
        SignalMap(
            {"q0.rf": "rf0", "q0.raman": "raman0", "q1.rf": "rf0" if alias else "rf1"}
        ),
        backend="artiq_spectrum",
    )


@qubit_operation
def gate(q, duration=800 * ns):
    play(q.rf, Square(duration, frequency=1e6, amplitude=0.3))


@qubit_operation
def pair(q0, q1):
    with parallel():
        gate(q0)
        gate(q1)


def experiment():
    qs = (Qubit("q0"), Qubit("q1"))
    return qs, Experiment("asap", qs)


def drives(compiled):
    return [
        event for event in compiled.points[0].logical_events if event.kind == "play"
    ]


def test_independent_qubits_and_multi_qubit_join_are_not_global_serial():
    (q0, q1), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section("gates", schedule="asap"):
            gate(q0)
            gate(q1, 1600 * ns)
            pair(q0, q1)
            gate(q0)
    compiled = lab_for().compile(exp)
    assert [(e.signal, e.start_mu) for e in drives(compiled)] == [
        ("q0.rf", 0),
        ("q1.rf", 0),
        ("q0.rf", 8000),
        ("q1.rf", 8000),
        ("q0.rf", 12000),
    ]
    assert compiled.points[0].duration_mu == 16000


def test_same_qubit_different_signals_and_physical_alias_are_ordered():
    (q0, q1), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section(schedule="asap"):
            play(q0.rf, Square(800 * ns))
            play(q0.raman, Square(800 * ns))
            play(q1.rf, Square(800 * ns))
    assert [e.start_mu for e in drives(lab_for().compile(exp))] == [0, 0, 4000]
    assert [e.start_mu for e in drives(lab_for(alias=True).compile(exp))] == [
        0,
        4000,
        4000,
    ]


def test_signal_wait_and_global_barrier_have_distinct_meanings():
    (q0, q1), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section(schedule="asap"):
            gate(q0)
            delay(q0.rf, 400 * ns)
            gate(q1, 1600 * ns)
            gate(q0)
            barrier()
            delay(200 * ns)
            gate(q1)
    assert [(e.signal, e.start_mu) for e in drives(lab_for().compile(exp))] == [
        ("q0.rf", 0),
        ("q1.rf", 0),
        ("q0.rf", 6000),
        ("q1.rf", 11000),
    ]


def test_frame_state_is_resolved_at_final_time_and_not_applied_twice():
    (q0, q1), exp = experiment()
    with exp.average(1), exp.realtime():
        rz(q0, 0.2)
        with exp.triggered_sequence(group="g"):
            shift_phase(q0.frame, 0.3)
            with section(schedule="asap"):
                gate(q0)
                gate(q1, 1600 * ns)
                rz(q0, 0.5)
                reset_frame(q0.frame)
                gate(q0)
    compiled = lab_for().compile(exp)
    pulses = drives(compiled)
    assert [e.params["phase"] for e in pulses] == pytest.approx([0.1, 0, 0])
    assert compiled.virtual_z_frames() == pytest.approx({"q0": 0, "q1": 0})
    assert compiled.frame_phases() == pytest.approx({"q0": 0, "q1": 0})
    before = [e.params["phase"] for e in pulses]
    resolve_frames(compiled.points[0].logical_events)
    assert [e.params["phase"] for e in pulses] == pytest.approx(before)


def test_indivisible_operation_preserves_internal_waits():
    @qubit_operation
    def spaced(q):
        gate(q, 400 * ns)
        delay(q.rf, 800 * ns)
        gate(q, 400 * ns)

    (q0, q1), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section(schedule="asap"):
            spaced(q0)
            gate(q1)
            gate(q0)
    assert [(e.signal, e.start_mu) for e in drives(lab_for().compile(exp))] == [
        ("q0.rf", 0),
        ("q1.rf", 0),
        ("q0.rf", 6000),
        ("q0.rf", 8000),
    ]


def test_explicit_parallel_conflict_is_not_silently_serialized():
    (q0, _), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section(schedule="asap"):
            with parallel():
                gate(q0)
                gate(q0)
    with pytest.raises(CompileError, match="overlap|conflict|Conflicting"):
        lab_for().compile(exp)


def test_asap_start_is_used_for_carrier_phase():
    (q0, q1), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section(schedule="asap"):
            gate(q0, 800 * ns)
            gate(q1, 1600 * ns)
            gate(q0, 400 * ns)
    compiled = lab_for().compile(exp)
    program = next(
        e for e in compiled.points[0].physical_events if e.kind == "spectrum_program"
    )
    pulses = program.params["cards"]["A"]["program"]["pulses"]
    assert pulses[1]["phase_rad"] == pytest.approx(2 * math.pi * 0.8)


def test_nested_section_and_repeat_align_events_at_the_actual_parent_origin():
    (q0, _), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section("outer"):
            with repeat(2):
                delay(0.2 * ns)
                with section("middle", schedule="asap"):
                    with section("inner", schedule="asap"):
                        gate(q0)
                delay(0.2 * ns)
            delay(1.4 * ns)
    compiled = lab_for().compile(exp)
    pulses = drives(compiled)
    assert [p.start_mu for p in pulses] == [4, 4008]
    assert pulses[0].params["section_schedule"][-1]["alignment_wait_mu"] == 3
    assert pulses[1].params["section_schedule"][-1]["alignment_wait_mu"] == 2


def test_physical_reservation_orders_a_logical_alias_on_pure_artiq():
    exp = Experiment("reserved TTL", [])
    with exp.average(1), exp.realtime(), section(schedule="asap"):
        reserve("ttl_a", 8 * ns)
        play("a", Square(8 * ns))
        play("b", Square(16 * ns))
    lab = LabConfig(
        DeviceSetup({name: Device(name, "ttl") for name in ("ttl_a", "ttl_b")}),
        SignalMap({"a": "ttl_a", "b": "ttl_b"}),
    )
    compiled = lab.compile(exp)
    assert [(e.signal, e.start_mu) for e in drives(compiled)] == [("b", 0), ("a", 8)]
    assert compiled.points[0].duration_mu == 16


def test_pure_artiq_section_keeps_grid_from_spectrum_configuration():
    exp = Experiment("native grid with unused Spectrum configuration", [])
    with exp.average(1), exp.realtime(), section(schedule="asap"):
        reserve("ttl_a", 8 * ns)
        play("a", Square(8 * ns))
        play("b", Square(16 * ns))
    spectrum = SpectrumConfig(
        {
            "unused": SpectrumGroup(
                trigger_device="ttl_b",
                cards={"A": SpectrumCard("SIM_A", (0,))},
                outputs={"unused_output": SpectrumOutput("A", 0)},
            )
        }
    )
    lab = LabConfig(
        DeviceSetup(
            {name: Device(name, "ttl") for name in ("ttl_a", "ttl_b")},
            spectrum=spectrum,
        ),
        SignalMap({"a": "ttl_a", "b": "ttl_b"}),
        backend="artiq",
    )
    compiled = lab.compile(exp)
    # Preserve the old LCM of the native factor (5) and the TTL device grid (4).
    assert [(e.signal, e.start_mu) for e in drives(compiled)] == [("b", 0), ("a", 20)]
    assert compiled.points[0].duration_mu == 28
    assert drives(compiled)[1].params["section_schedule"][0]["start_grid_mu"] == 20


@pytest.mark.parametrize("frame_first", [False, True])
def test_parallel_frame_write_at_pulse_start_requires_explicit_causality(frame_first):
    (q0, _), exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with parallel():
            if frame_first:
                shift_phase(q0.frame, 0.4)
            gate(q0)
            if not frame_first:
                shift_phase(q0.frame, 0.4)
    with pytest.raises(CompileError, match="same time.*serial branch"):
        lab_for().compile(exp)
