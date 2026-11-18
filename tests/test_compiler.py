import math
from dataclasses import replace

import pytest

from taiyidsl import (
    CompileError,
    ControlBinding,
    ControlRecipe,
    DeviceCalibration,
    Experiment,
    MHz,
    Qubit,
    Square,
    SweepParameter,
    compile_experiment,
    delay,
    measure,
    ns,
    play,
    rabi,
    rad,
    reference_setup,
    reserve,
    reset_frame,
    shift_phase,
    us,
)


def build(ops, qubits=None):
    qs = qubits or [Qubit("q0")]
    exp = Experiment("test", qs)
    with exp.average(2):
        with exp.realtime():
            ops(exp, *[exp.qubits[q.uid] for q in qs])
    setup, mapping = reference_setup(qs)
    return exp, setup, mapping


def test_average_orders_and_cartesian_axes():
    q = Qubit("q0")
    setup, mapping = reference_setup([q])
    for mode, expected in [
        ("cyclic", [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)]),
        ("sequential", [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)]),
    ]:
        compiled = compile_experiment(
            rabi(q, [0 * us, 4 * us, 8 * us], shots=2, mode=mode), setup, mapping
        )
        assert list(compiled.execution_order()) == expected
    exp = Experiment("2d", [q])
    duration = SweepParameter("d", [1, 2], "us")
    amp = SweepParameter("a", [0.2, 0.4], "dimensionless")
    with exp.average(2):
        with exp.sweep(duration):
            with exp.sweep(amp):
                with exp.realtime():
                    play(q.raman, Square(duration, amplitude=amp))
    compiled = compile_experiment(exp, setup, mapping)
    assert [p.axis_indices for p in compiled.points] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert compiled.axes[0]["values"] == [1e-6, 2e-6]
    assert compiled.axes[0]["values_unit"] == "s"


@pytest.mark.parametrize("align,start0,start1", [("start", 0, 0), ("end", 4000, 0)])
def test_parallel_alignment_and_join(align, start0, start1):
    def ops(exp, a, b):
        with exp.parallel(align):
            with exp.serial():
                play(a.raman, Square(4 * us))
            with exp.serial():
                play(b.raman, Square(8 * us))
        measure(a)

    exp, setup, mapping = build(ops, [Qubit("q0"), Qubit("q1")])
    c = compile_experiment(exp, setup, mapping)
    events = c.points[0].logical_events
    plays = [e for e in events if e.kind == "play"]
    assert {e.signal: e.start_mu for e in plays} == {
        "q0.raman": start0,
        "q1.raman": start1,
    }
    assert next(e.start_mu for e in events if e.kind == "measure") == 8000


def test_exact_duration_fails_instead_of_padding():
    def ops(exp, q):
        with exp.serial(exact_duration=10 * us):
            play(q.raman, Square(4 * us))

    with pytest.raises(CompileError, match="exact_duration"):
        compile_experiment(*build(ops))


def test_zero_pulse_no_switch_and_subminimum_rejected():
    c = compile_experiment(*build(lambda e, q: play(q.raman, Square(0 * ns))))
    assert c.points[0].physical_events == []
    with pytest.raises(CompileError, match="grid|minimum"):
        compile_experiment(*build(lambda e, q: play(q.raman, Square(1 * ns))))


def test_grid_rejection_and_opt_in_quantization():
    args = build(lambda e, q: play(q.readout, Square(9.4 * ns, frequency=0)))
    with pytest.raises(CompileError, match="not representable"):
        compile_experiment(*args)
    c = compile_experiment(*args, allow_quantization=True)
    assert c.points[0].duration_mu == 9
    assert c.diagnostics[0]["kind"] == "time_quantization"


def test_dimensions_and_unbound_parameters_fail():
    with pytest.raises(ValueError, match="Expected time"):
        compile_experiment(*build(lambda e, q: play(q.raman, Square(2 * MHz))))
    p = SweepParameter("unbound", [1], "us")
    with pytest.raises(ValueError, match="Unbound"):
        compile_experiment(*build(lambda e, q: play(q.raman, Square(p))))


def test_identical_adjacent_pulses_merge_but_logical_boundaries_remain():
    def ops(e, q):
        play(q.raman, Square(4 * us))
        play(q.raman, Square(8 * us))

    c = compile_experiment(*build(ops))
    p = c.points[0]
    assert len([e for e in p.logical_events if e.kind == "play"]) == 2
    assert len([e for e in p.physical_events if e.kind == "dds_on"]) == 1
    assert (
        len(next(e for e in p.physical_events if e.kind == "dds_on").logical_ids) == 2
    )


def test_parameter_change_can_prepare_during_previous_output():
    def sequence(gap):
        def ops(e, q):
            play(q.raman, Square(4 * us))
            delay(q.raman, gap)
            play(q.raman, Square(4 * us, phase=math.pi / 2))

        return ops

    for gap in (0 * ns, 504 * ns):
        result = compile_experiment(*build(sequence(gap)))
        assert result.points[0].duration_mu == 8000 + round(float(gap) * 1e9)
    c = compile_experiment(*build(sequence(2 * us)))
    assert [e.start_mu for e in c.points[0].logical_events if e.kind == "play"] == [
        0,
        6000,
    ]
    assert len([e for e in c.points[0].physical_events if e.kind == "dds_prepare"]) == 2


def test_frame_accumulates_across_repeat_without_hardware_get_set():
    def ops(exp, q):
        reset_frame(q.frame)
        with exp.repeat(2):
            shift_phase(q.frame, math.pi / 4 * rad)
            play(q.raman, Square(4 * us))
            delay(q.raman, 2 * us)

    c = compile_experiment(*build(ops))
    assert [
        e.params["phase"] for e in c.points[0].logical_events if e.kind == "play"
    ] == pytest.approx([math.pi / 4, math.pi / 2])


def test_parallel_frame_command_updates_both_playing_channels():
    def ops(exp, q):
        with exp.parallel():
            with exp.serial():
                shift_phase(q.frame, 1)
                play(q.raman, Square(4 * us))
            play(q.rf, Square(4 * us))

    compiled = compile_experiment(*build(ops))
    effects = [e for e in compiled.points[0].physical_events if e.kind == "dds_effect"]
    assert len(effects) == 2
    assert [e.params["phase"] for e in effects] == [1, 1]


def test_shared_output_conflicts_even_for_distinct_logical_signals():
    def ops(exp, q):
        with exp.parallel():
            play(q.raman, Square(4 * us))
            play(q.rf, Square(4 * us))

    exp, setup, mapping = build(ops)
    mapping["q0.rf"] = mapping["q0.raman"]
    with pytest.raises(CompileError, match="Resource conflict"):
        compile_experiment(exp, setup, mapping)


def test_shared_board_initial_configuration_serialized():
    def ops(exp, q):
        with exp.parallel():
            play(q.raman, Square(4 * us))
            play(q.rf, Square(4 * us))

    c = compile_experiment(*build(ops))
    configs = [e for e in c.points[0].physical_events if e.kind == "dds_prepare"]
    assert len(configs) == 2
    assert configs[0].end_mu <= configs[1].start_mu
    assert c.points[0].origin_mu > 2 * 1280 + 80
    epoch = next(e for e in c.points[0].physical_events if e.kind == "dds_nco_epoch")
    assert epoch.start_mu + 8 < configs[0].start_mu


def test_continuous_shared_update_does_not_restage_unchanged_active_channel():
    def ops(exp, q):
        with exp.parallel():
            with exp.serial():
                play(q.raman, Square(4 * us))
                delay(q.raman, 2 * us)
                play(q.raman, Square(4 * us, phase=1))
            play(q.rf, Square(12 * us))

    compiled = compile_experiment(*build(ops))
    effects = [e for e in compiled.points[0].physical_events if e.kind == "dds_effect"]
    held = [e for e in effects if e.signal.endswith("ch1")]
    assert [e.params["phase"] for e in held] == [0]
    assert [e.start_mu - compiled.points[0].origin_mu for e in held] == [0]


def test_composite_recipe_and_calibration_snapshot():
    exp, setup, mapping = build(lambda e, q: play(q.raman, Square(4 * us)))
    mapping["q0.raman"] = ControlRecipe(
        (
            ControlBinding("urukul0_ch0"),
            ControlBinding("urukul0_ch1", frequency_offset=1e6),
        )
    )
    setup.calibration["urukul0_ch1"] = DeviceCalibration(
        phase_offset=0.2, amplitude_scale=0.8
    )
    c = compile_experiment(exp, setup, mapping)
    cfg = [
        e
        for e in c.points[0].physical_events
        if e.kind == "dds_prepare" and e.signal.endswith("ch1")
    ][0]
    assert cfg.params["frequency"] == 81e6
    assert cfg.params["amplitude"] == pytest.approx(0.4)
    assert cfg.params["phase"] == pytest.approx(0.2)
    setup.calibration.clear()
    mapping.clear()
    assert c.setup.calibration
    assert c.signal_map


def test_named_reserve_and_unknown_resource():
    def ops(exp, q):
        with exp.parallel():
            reserve(q.raman, 4 * us)
            play(q.raman, Square(4 * us))

    with pytest.raises(CompileError, match="Resource conflict"):
        compile_experiment(*build(ops))
    with pytest.raises(CompileError, match="Unknown reserved"):
        compile_experiment(*build(lambda e, q: reserve("typo", 4 * us)))


def test_protocol_measures_once_per_handle_per_shot():
    def ops(exp, q):
        measure(q)
        measure(q)

    with pytest.raises(CompileError, match="once per shot"):
        compile_experiment(*build(ops))


def test_builder_failure_rolls_back_and_host_constraints():
    q = Qubit("q0")
    exp = Experiment("test", [q])
    with pytest.raises(RuntimeError):
        with exp.average(2):
            raise RuntimeError("abort")
    with exp.average(1):
        with exp.realtime():
            play(q.raman, Square(4 * us))
    compile_experiment(exp, *reference_setup([q]))
    with pytest.raises(NotImplementedError):
        Experiment("fixed", [q], repetition_period=1)
    exp = Experiment("host", [q])
    with exp.average(1):
        play(q.raman, Square(4 * us))
    with pytest.raises(CompileError, match="inside a realtime"):
        compile_experiment(exp, *reference_setup([q]))


def test_runtime_configuration_boundaries():
    with pytest.raises(ValueError):
        SweepParameter("empty", [], "s")
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            Experiment("bad", [Qubit("q0")]).average(bad)
    exp, setup, mapping = build(lambda e, q: play(q.raman, Square(4 * us)))
    setup.devices["urukul0_ch0"] = replace(
        setup.devices["urukul0_ch0"], max_frequency=70e6
    )
    with pytest.raises(CompileError, match="Frequency outside"):
        compile_experiment(exp, setup, mapping)


def test_fitted_duration_quantization_uses_device_grid_and_is_explicit():
    args = build(lambda e, q: play(q.raman, Square(5001.3 * ns)))
    with pytest.raises(CompileError, match="timing grid"):
        compile_experiment(*args)
    c = compile_experiment(*args, allow_quantization=True)
    assert c.points[0].duration_mu == 5000
    assert (
        next(d for d in c.diagnostics if d["kind"] == "time_quantization")["grid_mu"]
        == 4
    )


def test_mutation_of_compiled_snapshot_rejected_before_export(tmp_path):
    c = compile_experiment(*build(lambda e, q: play(q.raman, Square(4 * us))))
    c.setup.devices["urukul0_ch0"] = replace(
        c.setup.devices["urukul0_ch0"], sysclk_hz=900e6
    )
    with pytest.raises(CompileError, match="modified after"):
        c.export_artiq(tmp_path)


def test_ttl_protocol_adjacent_play_has_no_duplicate_edge():
    def ops(exp, q):
        measure(q)
        play(q.readout, Square(1 * us, amplitude=1, frequency=0))

    c = compile_experiment(*build(ops))
    physical = c.points[0].physical_events
    assert [(e.kind, e.start_mu) for e in physical] == [
        ("ttl_on", 0),
        ("ttl_off", 11000),
    ]


def test_dds_configuration_grid_distinct_from_output_grid():
    def ops(exp, q):
        play(q.raman, Square(8 * ns))
        delay(q.raman, 3004 * ns)
        play(q.raman, Square(8 * ns, phase=0.5))

    c = compile_experiment(*build(ops))
    assert all(
        e.start_mu % 8 == 0
        for e in c.points[0].physical_events
        if e.kind == "dds_prepare"
    )
    assert [
        e.start_mu - c.points[0].origin_mu
        for e in c.points[0].physical_events
        if e.kind == "dds_on"
    ] == [0, 3012]


def test_reference_setup_accepts_hyphenated_qubit_identity():
    q = Qubit("q-one")
    c = compile_experiment(rabi(q, [4 * us]), *reference_setup([q]))
    assert c.qubits[q.uid] == q


def test_source_map_points_to_builder_caller_not_contextlib():
    exp, setup, mapping = build(lambda e, q: play(q.raman, Square(4 * us)))
    c = compile_experiment(exp, setup, mapping)
    assert "contextlib" not in c.ir["children"][0]["source"]
    assert "test_compiler.py" in c.ir["children"][0]["source"]
