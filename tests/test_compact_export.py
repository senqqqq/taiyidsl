"""Observable equivalence of compact and debug exports, including custom DSLs."""

import ast
import json
import math
from dataclasses import replace

import pytest

from taiyidsl import (
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    compile_experiment,
    delay,
    measure,
    play,
    rabi,
    ramsey,
    reference_setup,
    us,
)
from taiyidsl.artiq import (
    ExportError,
    generate_source,
    verify_equivalence,
    verify_export,
)


def _pulse_kernels(source):
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(d, ast.Name) and d.id == "kernel" for d in node.decorator_list
        )
        and node.name != "initialize_devices"
    ]


def _compile(exp, qubits, *, legacy=False):
    setup, mapping = reference_setup(qubits)
    if legacy:
        # Keep testing the compact legacy exporter, with its corrected real
        # driver budget. Timed Square currently uses explicit per-point kernels.
        setup.devices = {
            name: replace(device, square_latency_mu=None, nco_mode="legacy_tracking")
            for name, device in setup.devices.items()
        }
    return compile_experiment(exp, setup, mapping)


def _assert_verified(compiled):
    report = verify_equivalence(compiled)
    assert report["status"] == "passed"
    assert len(report["points"]) == len(compiled.points)
    assert all(point["status"] == "passed" for point in report["points"])
    return report


@pytest.mark.parametrize("template", [rabi, ramsey])
@pytest.mark.parametrize("averaging", ["cyclic", "sequential"])
def test_timed_templates_export_each_point_including_zero_point(template, averaging):
    q = Qubit("q0")
    values = [i * 4 * us for i in range(17)]
    compiled = _compile(template(q, values, shots=3, mode=averaging), [q])

    compact = generate_source(compiled)
    expanded = generate_source(compiled, mode="unrolled")

    assert len(_pulse_kernels(compact)) == 17
    assert len(_pulse_kernels(expanded)) == 17
    assert "def point_0(" in compact
    assert compact == expanded
    _assert_verified(compiled)


@pytest.mark.parametrize("averaging", ["cyclic", "sequential"])
def test_custom_multiaxis_sweep_is_parameterized_without_template_metadata(averaging):
    q = Qubit("q0")
    exp = Experiment("user_defined_scan", [q])
    duration = SweepParameter("my_duration", [0, 4, 12], "us")
    amplitude = SweepParameter("my_amplitude", [0.25, 0.75], "dimensionless")
    frequency = SweepParameter("my_frequency", [80, 81], "MHz")

    with exp.average(2, mode=averaging):
        with exp.sweep(duration):
            with exp.sweep(amplitude):
                with exp.sweep(frequency):
                    with exp.realtime():
                        with exp.repeat(2):
                            play(
                                q.raman,
                                Square(
                                    duration, amplitude=amplitude, frequency=frequency
                                ),
                            )
                            delay(q.raman, 2 * us)
                        measure(q)

    compiled = _compile(exp, [q], legacy=True)
    assert exp.metadata == {}
    assert len(compiled.points) == 12
    assert len(_pulse_kernels(generate_source(compiled))) == 1
    assert {p.origin_mu for p in compiled.points} == {0, 3000}
    configs = [
        e for p in compiled.points for e in p.physical_events if e.kind == "dds_config"
    ]
    assert {e.params["frequency"] for e in configs} == {80e6, 81e6}
    assert {e.params["amplitude"] for e in configs} == {0.25, 0.75}
    _assert_verified(compiled)


def test_custom_static_cases_with_incompatible_device_order_use_distinct_kernels():
    a, b = Qubit("a"), Qubit("b")
    exp = Experiment("device_order_is_semantics", [a, b])
    selection = SweepParameter("order", [0, 1], "dimensionless")
    with exp.average(2):
        with exp.sweep(selection):
            with exp.realtime():
                with exp.case(selection, 0):
                    play(a.raman, Square(4 * us))
                    play(b.raman, Square(8 * us))
                with exp.case(selection, 1):
                    play(b.raman, Square(4 * us))
                    play(a.raman, Square(8 * us))
                measure(a, handle="a_state")

    compiled = _compile(exp, [a, b])
    assert len(_pulse_kernels(generate_source(compiled))) == 2
    _assert_verified(compiled)


def test_empty_physical_program_and_nonempty_program_preserve_completion_times():
    q = Qubit("q0")
    exp = Experiment("optional_output", [q])
    duration = SweepParameter("duration", [0, 4], "us")
    with exp.average(2):
        with exp.sweep(duration):
            with exp.realtime():
                play(q.raman, Square(duration))
                delay(q.raman, 2 * us)

    compiled = _compile(exp, [q], legacy=True)
    assert compiled.points[0].physical_events == []
    assert compiled.points[0].duration_mu == 2000
    assert len(_pulse_kernels(generate_source(compiled))) == 1
    _assert_verified(compiled)


def test_changing_preroll_and_phase_references_share_a_kernel():
    q = Qubit("q0")
    exp = Experiment("phase_and_initial_idle", [q])
    lead = SweepParameter("lead", [0, 0.5, 2], "us")
    phase = SweepParameter("phase", [0, math.pi / 2, math.pi], "rad")
    with exp.average(2):
        with exp.sweep(lead):
            with exp.sweep(phase):
                with exp.realtime():
                    delay(q.raman, lead)
                    play(q.raman, Square(4 * us, phase=phase))
                    measure(q)

    compiled = _compile(exp, [q], legacy=True)
    assert len(_pulse_kernels(generate_source(compiled))) == 1
    assert len({p.origin_mu for p in compiled.points}) == 3
    configs = [
        e for p in compiled.points for e in p.physical_events if e.kind == "dds_config"
    ]
    assert len({e.params["ref_time_mu"] for e in configs}) == 3
    _assert_verified(compiled)


def test_public_compiled_export_accepts_both_modes_and_saves_report(tmp_path):
    q = Qubit("q0")
    compiled = _compile(ramsey(q, [0 * us, 4 * us, 12 * us], shots=2), [q])
    auto = compiled.export_artiq(tmp_path / "auto")
    debug = compiled.export_artiq(tmp_path / "debug", mode="unrolled")
    for exported in (auto, debug):
        report = json.loads(exported["manifest"].read_text(encoding="utf-8"))
        assert report["verification"]["offline_export_check"] == "passed"
        assert report["verification"]["artiq_compile"] == "not_verified"
        assert report["verification"]["hardware"] == "not_verified"
        assert (
            verify_export(compiled, exported["source"])["offline_export_check"]
            == "passed"
        )
    assert len(_pulse_kernels(auto["source"].read_text(encoding="utf-8"))) == 3
    assert len(_pulse_kernels(debug["source"].read_text(encoding="utf-8"))) == 3


def test_unknown_export_mode_is_rejected():
    q = Qubit("q0")
    compiled = _compile(rabi(q, [4 * us]), [q])
    with pytest.raises((ValueError, ExportError), match="mode"):
        generate_source(compiled, mode="invented")


def test_compact_codegen_does_not_hide_mutated_output_edge():
    q = Qubit("q0")
    compiled = _compile(ramsey(q, [0 * us, 4 * us, 12 * us]), [q])
    source = generate_source(compiled)
    changed = source.replace(".sw.on()", ".sw.off()", 1)
    assert source != changed
    with pytest.raises(ExportError, match="differ"):
        verify_export(compiled, source=changed)


def test_compact_parameter_row_mutation_is_detected():
    q = Qubit("q0")
    exp = Experiment("legacy_parameter_rows", [q])
    duration = SweepParameter("duration", [4, 12], "us")
    with exp.average(2):
        with exp.sweep(duration):
            with exp.realtime():
                play(q.raman, Square(duration))
    compiled = _compile(exp, [q], legacy=True)
    tree = ast.parse(generate_source(compiled))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Attribute) and t.attr == "_point_values"
            for t in node.targets
        ):
            continue
        assert isinstance(node.value, ast.List)
        row = node.value.elts[-1]
        assert isinstance(row, ast.Call)
        values = row.args[0]
        assert isinstance(values, ast.List)
        assert values.elts
        # Keep dtype/int64 intact while corrupting the actual table the exported
        # host dispatcher passes to the final point's shared kernel.
        values.elts = [
            ast.BinOp(value, ast.Add(), ast.Constant(8)) for value in values.elts
        ]
        found = True
        break
    assert found, "Compact source must contain its host parameter table"
    changed = ast.unparse(ast.fix_missing_locations(tree))
    with pytest.raises(ExportError):
        verify_export(compiled, source=changed)


def test_generated_host_shot_order_is_checked():
    q = Qubit("q0")
    compiled = _compile(ramsey(q, [4 * us, 12 * us], shots=2), [q])
    source = generate_source(compiled)
    changed = source.replace("self._run_shot(point, shot)", "self._run_shot(point, 0)")
    assert source != changed
    with pytest.raises(ExportError):
        verify_export(compiled, source=changed)


def test_kernel_completion_time_is_checked_even_when_output_events_match():
    q = Qubit("q0")
    compiled = _compile(rabi(q, [4 * us, 12 * us], shots=2), [q])
    tree = ast.parse(generate_source(compiled))
    kernel = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "point_0"
    )
    times = [
        n
        for n in ast.walk(kernel)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "at_mu"
    ]
    # The final at_mu waits out the complete point, including otherwise idle tail.
    # Changing only this call leaves every emitted DDS/TTL event unchanged.
    times[-1].args[0] = ast.BinOp(times[-1].args[0], ast.Add(), ast.Constant(8))
    changed = ast.unparse(ast.fix_missing_locations(tree))
    with pytest.raises(ExportError):
        verify_export(compiled, source=changed)
