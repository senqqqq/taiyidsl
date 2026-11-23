"""Spectrum regions preserve structural timing and explicit frame ownership."""

import math
from fractions import Fraction
from types import SimpleNamespace

import pytest

from taiyidsl import (
    CompileError,
    Device,
    Experiment,
    FlatTopGaussian,
    Qubit,
    SignalMap,
    Square,
    SweepParameter,
    acquire,
    delay,
    ns,
    play,
    reference_lab,
    reset_frame,
    rz,
    shift_phase,
)
from taiyidsl.backends.artiq_spectrum.scheduling import schedule_spectrum
from taiyidsl.config.spectrum import (
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
)
from taiyidsl.dsl import HalfSinePhase, PolynomialPhase, json_value, trigplay


def setup_for(*, extra_mapping=None, signal_frames=None):
    outputs = {f"awg{i}": SpectrumOutput("A", i) for i in range(2)}
    devices = {name: Device(name, "spectrum", grid_mu=4) for name in outputs}
    devices["ttl"] = Device("ttl", "ttl", grid_mu=1)
    devices["dds"] = Device("dds", "dds", grid_mu=4)
    mapping = SignalMap({"q0.rf": "awg0", "clock": "awg1", "light": "ttl"})
    mapping.update(extra_mapping or {})
    group = SpectrumGroup(
        trigger_device="ttl",
        cards={"A": SpectrumCard("SCHEDULER_FIXTURE")},
        outputs=outputs,
    )
    setup = SimpleNamespace(
        devices=devices,
        tick_seconds=0.2e-9,
        protocols={},
        spectrum=SpectrumConfig(
            groups={"g": group},
            signal_frames={"clock": None, **(signal_frames or {})},
            artiq_tick_seconds=1e-9,
        ),
    )

    def signal_grid(signal, setup, mapping):
        if signal not in mapping:
            return 1
        grids = [
            4 if b.device in outputs else setup.devices[b.device].grid_mu * 5
            for b in mapping.bindings(signal)
        ]
        return math.lcm(*grids)

    return setup, mapping, SimpleNamespace(signal_grid=signal_grid)


def schedule(exp, *, coordinates=None, configuration=None, quantize=False):
    setup, mapping, backend = configuration or setup_for()
    body = exp.root.children[0].children
    while len(body) == 1 and body[0].kind == "sweep":
        body = body[0].children
    return schedule_spectrum(
        exp, body, setup, mapping, coordinates or {}, quantize, [], 10000, backend
    )


def experiment():
    q = Qubit("q0")
    return q, Experiment("spectrum_dsl", [q])


def pulses(events):
    return [event for event in events if event.kind == "play"]


def test_default_pulse_serialization_is_unchanged():
    assert set(json_value(Square(8 * ns))) == {
        "duration",
        "amplitude",
        "frequency",
        "phase",
    }
    assert "phase_modulation" not in json_value(FlatTopGaussian(0, 20 * ns))


def test_modulation_is_immutable_and_single_call_overrides_are_absolute():
    coefficients = [0, 0, 3 * math.pi, -2 * math.pi]
    modulation = PolynomialPhase(coefficients)
    coefficients[2] = 99
    q, exp = experiment()
    pulse = Square(8 * ns, amplitude=0.7, phase_modulation=modulation)
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay(q.rf, pulse, amplitude=0, phase_modulation=HalfSinePhase(0.2))
        trigplay(q.rf, pulse)
    events, _ = schedule(exp)
    first, second = pulses(events)
    assert first.params["amplitude"] == 0
    assert first.params["phase_modulation"] == {"kind": "half_sine", "amplitude": 0.2}
    assert second.params["amplitude"] == 0.7
    assert second.params["phase_modulation"]["coefficients"] == pytest.approx(
        [0, 0, 3 * math.pi, -2 * math.pi]
    )
    assert pulse.phase_modulation is modulation


def test_modulation_override_can_clear_a_template_without_mutating_it():
    q, exp = experiment()
    pulse = Square(8 * ns, phase_modulation=HalfSinePhase(0.3))
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay(q.rf, pulse, phase_modulation=None)
        trigplay(q.rf, pulse)
    events, _ = schedule(exp)
    first, second = pulses(events)
    assert "phase_modulation" not in first.params
    assert second.params["phase_modulation"]["amplitude"] == 0.3


def test_polynomial_coefficients_bind_to_the_scan_point():
    q, exp = experiment()
    beta = SweepParameter("beta", [0.1, 0.2], "rad")
    with exp.average(1), exp.sweep(beta), exp.realtime():
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(8 * ns, phase_modulation=PolynomialPhase([beta, 1])))
    events, _ = schedule(exp, coordinates={"beta": Fraction("0.2")})
    assert pulses(events)[0].params["phase_modulation"] == {
        "kind": "polynomial",
        "coefficients": [0.2, 1.0],
    }


@pytest.mark.parametrize("modulation", [object(), float("nan"), "sine"])
def test_invalid_modulation_is_not_silently_accepted(modulation):
    with pytest.raises(TypeError, match="phase_modulation"):
        Square(8 * ns, phase_modulation=modulation)


@pytest.mark.parametrize("constructor", [HalfSinePhase, lambda x: PolynomialPhase([x])])
@pytest.mark.parametrize("value", [True, float("nan"), 1 * ns])
def test_phase_modulation_values_are_finite_phase_values(constructor, value):
    with pytest.raises((TypeError, ValueError)):
        constructor(value)


def test_empty_polynomial_is_rejected():
    with pytest.raises(ValueError, match="coefficients"):
        PolynomialPhase([])


def test_region_serial_duration_wait_and_terminal_frame_are_retained():
    q, exp = experiment()
    with exp.average(1), exp.realtime():
        play("light", Square(8 * ns))
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(8 * ns, phase=0.4))
            delay(4 * ns)
            rz(q, 0.2)
            trigplay(q.rf, Square(8 * ns, phase=0.4))
            rz(q, 0.3)
        play("light", Square(4 * ns))
    events, duration = schedule(exp)
    region = next(e for e in events if e.kind == "spectrum_region")
    assert (region.start_mu, region.duration_mu, duration) == (40, 100, 160)
    members = [
        e for e in events if e.params.get("region_id") == region.params["region_id"]
    ]
    wait = next(e for e in members if e.kind == "scope_delay")
    assert wait.resources == ()
    assert wait.start_mu == 80
    spectrum_pulses = [e for e in pulses(events) if e.params.get("route") == "spectrum"]
    assert [e.params["phase"] for e in spectrum_pulses] == pytest.approx([0.4, 0.2])
    terminal = [e for e in members if e.kind == "rz"][-1]
    assert terminal.start_mu == region.end_mu == 140
    assert terminal.params["accumulated_virtual_z"] == pytest.approx(0.5)
    assert terminal.params["effective_frame_phase"] == pytest.approx(-0.5)
    assert region.params["terminal_frames"] == {
        q.uid: {
            "frame_phase": -0.5,
            "hardware_phase": 0.0,
            "virtual_z": 0.5,
            "effective_phase": -0.5,
        }
    }


def test_parallel_join_and_unqualified_wait_do_not_mute_another_channel():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.parallel():
            trigplay("clock", Square(40 * ns))
            with exp.serial():
                delay(8 * ns)
                trigplay(q.rf, Square(8 * ns))
                delay(duration=4 * ns)
        trigplay(q.rf, Square(4 * ns))
    events, duration = schedule(exp)
    assert duration == 220
    assert [(e.signal, e.start_mu, e.duration_mu) for e in pulses(events)] == [
        ("clock", 0, 200),
        (q.rf, 40, 40),
        (q.rf, 200, 20),
    ]


def test_signal_delay_reserves_its_output_and_alias_overlap_is_rejected():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.parallel():
            delay(q.rf, 16 * ns)
            trigplay("alias", Square(8 * ns))
    with pytest.raises(CompileError, match="overlap|[Cc]onflict"):
        schedule(
            exp,
            configuration=setup_for(
                extra_mapping={"alias": "awg0"}, signal_frames={"alias": None}
            ),
        )


def test_explicit_custom_signal_frame_and_explicit_none_override_prefix():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        shift_phase(q.frame, 0.4)
        rz(q, 0.1)
        trigplay("clock", Square(8 * ns, phase=0.2))
        trigplay(q.rf, Square(8 * ns, phase=0.2))
    configuration = setup_for(signal_frames={"clock": q.uid, q.rf: None})
    events, _ = schedule(exp, configuration=configuration)
    assert [e.params["phase"] for e in pulses(events)] == pytest.approx([0.5, 0.2])
    assert [e.params["frame"] for e in pulses(events)] == [q.uid, None]


def test_unassociated_new_signal_is_rejected():
    _, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay("new_drive", Square(8 * ns))
    with pytest.raises(CompileError, match="frame association|signal_frames"):
        schedule(exp, configuration=setup_for(extra_mapping={"new_drive": "awg0"}))


def test_unused_qubit_mapping_does_not_require_that_qubit_in_the_experiment():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay(q.rf, Square(8 * ns))
    configuration = setup_for(extra_mapping={"q1.rf": "awg1"})
    assert schedule(exp, configuration=configuration)[1] == 40


def test_used_mapping_cannot_refer_to_a_missing_qubit_frame():
    _, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay("q1.rf", Square(8 * ns))
    with pytest.raises(CompileError, match="Unknown frame association q1"):
        schedule(exp, configuration=setup_for(extra_mapping={"q1.rf": "awg1"}))


def test_reset_clears_the_unified_frame_including_prior_virtual_z():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        shift_phase(q.frame, 0.4)
        rz(q, 0.1)
        trigplay(q.rf, Square(8 * ns, phase=0.2))
        reset_frame(q.frame)
        trigplay(q.rf, Square(8 * ns, phase=0.2))
    events, _ = schedule(exp)
    assert [e.params["phase"] for e in pulses(events)] == pytest.approx([0.5, 0.2])


def test_active_pulse_hardware_frame_is_preserved_for_sample_phase_steps():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.parallel():
            trigplay(q.rf, Square(40 * ns))
            with exp.serial():
                delay(8 * ns)
                shift_phase(q.frame, 0.4)
                delay(8 * ns)
                reset_frame(q.frame)
    events, _ = schedule(exp)
    updates = [e for e in events if e.kind in {"shift_phase", "reset_frame"}]
    assert [(e.start_mu, e.params["effective_frame_phase"]) for e in updates] == [
        (40, 0.4),
        (80, 0),
    ]


def test_virtual_z_parallel_access_uses_explicit_signal_frame():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.parallel():
            trigplay("clock", Square(40 * ns))
            with exp.serial():
                delay(8 * ns)
                rz(q, 0.2)
    with pytest.raises(CompileError, match="serial branch|active qubit"):
        schedule(exp, configuration=setup_for(signal_frames={"clock": q.uid}))


def test_frame_cannot_spill_to_an_artiq_drive():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        rz(q, 0.2)
    with pytest.raises(CompileError, match="ARTIQ|outside.*group|outside.*region"):
        schedule(exp, configuration=setup_for(extra_mapping={q.raman: "dds"}))


def test_spectrum_only_outer_phase_commands_are_logical():
    q, exp = experiment()
    with exp.average(1), exp.realtime():
        rz(q, 0.2)
        with exp.triggered_sequence(group="g"):
            play(q.rf, Square(8 * ns))
        rz(q, 0.3)
    events, _ = schedule(exp)
    commands = [event for event in events if event.kind == "rz"]
    assert [event.params["route"] for event in commands] == [
        "logical_frame",
        "logical_frame",
    ]
    assert pulses(events)[0].params["phase"] == pytest.approx(-0.2)
    assert commands[-1].params["accumulated_virtual_z"] == pytest.approx(0.5)
    region = next(event for event in events if event.kind == "spectrum_region")
    assert region.params["terminal_frames"][q.uid]["virtual_z"] == pytest.approx(0.2)
    for command in (lambda: shift_phase(q.frame, 0.2), lambda: reset_frame(q.frame)):
        q, exp = experiment()
        with exp.average(1), exp.realtime():
            command()
        outer, _ = schedule(exp)
        assert outer[0].params["route"] == "logical_frame"


def test_region_id_survives_sweeps_and_repeat_zero_retains_region():
    q, exp = experiment()
    count = SweepParameter("count", [0, 2], "dimensionless")
    beta = SweepParameter("beta", [0.1, 0.3], "rad")
    pulse = Square(8 * ns, phase_modulation=HalfSinePhase(beta))
    with exp.average(1), exp.sweep(count), exp.sweep(beta), exp.realtime():
        with exp.triggered_sequence(group="g"):
            with exp.repeat(count):
                trigplay(q.rf, pulse)
            rz(q, beta)
    empty, empty_duration = schedule(
        exp, coordinates={"count": Fraction(0), "beta": Fraction("0.1")}
    )
    full, full_duration = schedule(
        exp, coordinates={"count": Fraction(2), "beta": Fraction("0.3")}
    )
    assert (empty_duration, full_duration) == (0, 80)
    assert empty[0].params["region_id"] == full[0].params["region_id"]
    assert [e.params["repeat_indices"] for e in pulses(full)] == [[0], [1]]
    assert all(e.params["phase_modulation"]["amplitude"] == 0.3 for e in pulses(full))


def test_repeating_a_region_instead_of_its_body_is_rejected():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.repeat(2):
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(8 * ns))
    with pytest.raises(CompileError, match="one.*sequence|one.*region"):
        schedule(exp)


def test_mutually_exclusive_region_cases_allow_one_active_region_per_point():
    q, exp = experiment()
    select = SweepParameter("select", [0, 1], "dimensionless")
    with exp.average(1), exp.sweep(select), exp.realtime():
        for value in (0, 1):
            with exp.case(select, value), exp.triggered_sequence(group="g"):
                trigplay(q.rf, Square((value + 1) * 8 * ns))
    first, first_duration = schedule(exp, coordinates={"select": Fraction(0)})
    second, second_duration = schedule(exp, coordinates={"select": Fraction(1)})
    assert (first_duration, second_duration) == (40, 80)
    assert first[0].params["region_id"] != second[0].params["region_id"]


def test_region_can_be_one_explicit_external_parallel_branch():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.parallel(align="end"):
        play("light", Square(40 * ns))
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(8 * ns))
            rz(q, 0.3)
    events, duration = schedule(exp)
    region = next(e for e in events if e.kind == "spectrum_region")
    gate = next(e for e in pulses(events) if e.signal == q.rf)
    terminal = next(e for e in events if e.kind == "rz")
    assert (region.start_mu, region.end_mu, duration) == (160, 200, 200)
    assert gate.start_mu == region.start_mu
    assert terminal.start_mu == region.end_mu
    assert "region_id" not in pulses(events)[0].params


@pytest.mark.parametrize("inactive", [False, True])
def test_artiq_play_in_region_is_rejected_even_in_zero_repeat(inactive):
    _, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with exp.repeat(0 if inactive else 1):
            play("light", Square(8 * ns))
    with pytest.raises(CompileError, match="outside Spectrum group"):
        schedule(exp)


def test_cross_group_signal_delay_is_rejected():
    _, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        delay("light", 8 * ns)
    with pytest.raises(CompileError, match="group"):
        schedule(exp)


def test_play_cannot_silently_route_to_spectrum():
    q, exp = experiment()
    with exp.average(1), exp.realtime():
        play(q.rf, Square(8 * ns))
    with pytest.raises(CompileError, match="trigplay"):
        schedule(exp)


def test_builder_requires_explicit_scope_and_prevents_nested_scopes():
    q, exp = experiment()
    with exp.average(1), exp.realtime():
        with pytest.raises(ValueError, match="triggered_sequence"):
            trigplay(q.rf, Square(8 * ns))
        delay(8 * ns)
        with exp.triggered_sequence(group="g"):
            with pytest.raises(ValueError, match="nest"):
                with exp.triggered_sequence(group="g"):
                    pass
    # The same branch delay is valid on a standalone card. Only compilation
    # knows that this particular backend needs a triggered sequence.
    with pytest.raises(CompileError, match="scope_delay requires"):
        schedule(exp)
    with pytest.raises(CompileError, match="scope_delay"):
        reference_lab([q]).compile(exp)


def test_spectrum_sample_grid_never_quantizes_a_requested_duration():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        trigplay(q.rf, Square(1 * ns))
    with pytest.raises(CompileError, match="grid|representable"):
        schedule(exp, quantize=True)


def test_pure_artiq_does_not_ignore_modulation():
    q, exp = experiment()
    with exp.average(1), exp.realtime():
        play(q.rf, Square(8 * ns, phase_modulation=HalfSinePhase(0.2)))
    with pytest.raises(CompileError, match="modulation|Spectrum"):
        reference_lab([q]).compile(exp)


def test_hybrid_artiq_play_cannot_ignore_modulation():
    _, exp = experiment()
    with exp.average(1), exp.realtime():
        play("light", Square(8 * ns, phase_modulation=HalfSinePhase(0.2)))
    with pytest.raises(CompileError, match="modulation|Spectrum"):
        schedule(exp)


def test_unused_spectrum_inventory_does_not_create_a_region():
    exp = Experiment("ttl_only", [])
    with exp.average(1), exp.realtime():
        play("light", Square(8 * ns))
    events, duration = schedule(exp)
    assert duration == 40
    assert [event.kind for event in events] == ["play"]
    assert "route" not in events[0].params


def test_acquisition_is_rejected_inside_the_card_program():
    q, exp = experiment()
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        acquire("light", duration=8 * ns, handle="image", qubits=[q])
    with pytest.raises(CompileError, match="acquire.*triggered_sequence"):
        schedule(exp)
