"""Point-bound repeats preserve timing, frames and export behavior."""

import pytest

from taiyidsl import (
    CompileError,
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    compile_experiment,
    delay,
    play,
    rad,
    reference_setup,
    shift_phase,
    us,
)
from taiyidsl.artiq import verify_equivalence


def compile_scan(alias=True, mode="cyclic"):
    q = Qubit("q0")
    exp = Experiment("repeat_scan", [q])
    count = SweepParameter("count", (0, 1, 3), "dimensionless")
    with exp.average(2, mode=mode):
        with exp.sweep(count) as cnt:
            assert cnt is count
            with exp.realtime():
                with exp.repeat(cnt if alias else count):
                    shift_phase(q.frame, 0.25 * rad)
                    delay(q.raman, 10 * us)
                play(q.raman, Square(4 * us))
    return compile_experiment(exp, *reference_setup([q]))


@pytest.mark.parametrize("mode", ["cyclic", "sequential"])
def test_alias_and_direct_reference_have_identical_events(mode):
    a, b = compile_scan(mode=mode), compile_scan(alias=False, mode=mode)
    for n, point, other in zip((0, 1, 3), a.points, b.points):

        def events(p):
            return [
                (e.kind, e.start_mu, e.duration_mu, e.params) for e in p.logical_events
            ]

        assert events(point) == events(other)
        assert point.duration_mu == n * 10_000 + 4_000
        assert point.logical_events[-1].params["phase"] == n * 0.25
    assert verify_equivalence(a)["status"] == "passed"


@pytest.mark.parametrize(
    "value",
    [
        True,
        -1,
        1.5,
        1.0,
        SweepParameter("n", (0, 1.5), "dimensionless"),
        SweepParameter("n", (-1, 1), "dimensionless"),
        SweepParameter("n", (1, 2), "us"),
    ],
)
def test_invalid_counts_fail_before_building_body(value):
    with pytest.raises((TypeError, ValueError)):
        Experiment("bad", []).repeat(value)


@pytest.mark.parametrize("conflict", [False, True])
def test_unbound_or_conflicting_reference_inside_zero_repeat_fails(conflict):
    q = Qubit("q0")
    exp = Experiment("bad_scope", [q])
    declared = SweepParameter("n", (0, 1), "dimensionless")
    other = SweepParameter("n" if conflict else "missing", (2, 3), "dimensionless")
    with exp.average(1):
        with exp.sweep(declared):
            with exp.realtime():
                with exp.repeat(0):
                    with exp.repeat(other):
                        delay(q.raman, 1 * us)
    with pytest.raises(CompileError, match="[Pp]arameter|[Ss]weep"):
        compile_experiment(exp, *reference_setup([q]))


def test_nested_repeats_and_budget():
    q = Qubit("q0")
    exp = Experiment("nested", [q])
    count = SweepParameter("n", (0, 2), "dimensionless")
    with exp.average(1):
        with exp.sweep(count) as cnt:
            with exp.realtime():
                with exp.repeat(cnt):
                    with exp.repeat(3):
                        delay(q.raman, 1 * us)
    setup, mapping = reference_setup([q])
    compiled = compile_experiment(exp, setup, mapping)
    assert [p.duration_mu for p in compiled.points] == [0, 6000]
    with pytest.raises(CompileError, match="budget"):
        compile_experiment(exp, setup, mapping, max_events_per_point=3)


def test_parameter_is_not_a_python_scalar():
    count = SweepParameter("n", (0, 1), "dimensionless")
    with pytest.raises(TypeError, match="[Pp]arameter|[Ss]weep"):
        bool(count)
    with pytest.raises(TypeError, match="[Pp]arameter|[Ss]weep"):
        range(count)
    with pytest.raises(ValueError, match="shots"):
        Experiment("static_shots", []).average(shots=count)


def test_two_count_axes_bind_independently_and_keep_cartesian_order():
    q = Qubit("q0")
    exp = Experiment("two_counts", [q])
    outer = SweepParameter("outer", (0, 2), "dimensionless")
    inner = SweepParameter("inner", (1, 3), "dimensionless")
    with exp.average(1):
        with exp.sweep(outer) as n:
            with exp.sweep(inner) as m:
                with exp.realtime():
                    with exp.repeat(n):
                        with exp.repeat(m):
                            delay(q.raman, 1 * us)
    compiled = compile_experiment(exp, *reference_setup([q]))
    assert [p.axis_indices for p in compiled.points] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert [p.duration_mu for p in compiled.points] == [0, 0, 2000, 6000]


def test_failed_alias_block_rolls_back_and_leaves_builder_usable():
    exp = Experiment("rollback", [])
    count = SweepParameter("n", (0, 1), "dimensionless")
    with pytest.raises(TypeError):
        with exp.average(1):
            with exp.sweep(count) as cnt:
                bool(cnt)
    assert exp.root.children == []
    with exp.average(1):
        with exp.sweep(count) as cnt:
            with exp.realtime():
                with exp.repeat(cnt):
                    pass
