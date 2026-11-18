"""Host chunks preserve global coordinates and explicitly chosen averaging order."""

import json

import pytest

from taiyidsl import (
    CompileError,
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    play,
    reference_lab,
    us,
)
from taiyidsl.chunking import point_execution_layout
from taiyidsl.results import ResultSet
from taiyidsl.visualization import _payload


def scan(*, axis=1, chunks=2, chunk_size=None, scope="global", mode="cyclic"):
    q = Qubit("q0")
    exp = Experiment("chunk_order", [q])
    parameters = [
        SweepParameter("outer", [0, 1]),
        SweepParameter("length", [1, 2, 2, 4, 5]),
        SweepParameter("inner", [0, 1]),
    ]
    options = (
        {"chunk_size": chunk_size} if chunk_size is not None else {"chunks": chunks}
    )
    with exp.average(2, mode=mode, scope=scope):
        with exp.sweep(parameters[0], **(options if axis == 0 else {})):
            with exp.sweep(parameters[1], **(options if axis == 1 else {})):
                with exp.sweep(parameters[2], **(options if axis == 2 else {})):
                    with exp.realtime():
                        play(q.raman, Square(4 * us))
    return reference_lab([q]).compile(exp)


def test_middle_axis_chunks_preserve_outer_prefix_and_complete_inner_product():
    compiled = scan()
    assert [c["points"] for c in compiled.host_execution_plan["chunks"]] == [
        list(range(0, 6)),
        list(range(6, 10)),
        list(range(10, 16)),
        list(range(16, 20)),
    ]
    assert list(compiled.execution_order()) == [
        (point, shot) for shot in range(2) for point in range(20)
    ]
    assert compiled.points[2].coordinates == compiled.points[4].coordinates
    assert compiled.points[2].axis_indices != compiled.points[4].axis_indices


@pytest.mark.parametrize("mode", ["cyclic", "sequential"])
def test_chunk_scope_drives_generated_host_order_and_result_coordinates(tmp_path, mode):
    compiled = scan(scope="chunk", mode=mode)
    points = [c["points"] for c in compiled.host_execution_plan["chunks"]]
    expected = (
        [(p, s) for chunk in points for s in range(2) for p in chunk]
        if mode == "cyclic"
        else [(p, s) for chunk in points for p in chunk for s in range(2)]
    )
    assert list(compiled.execution_order()) == expected
    # Export verification independently executes generated standalone host loops.
    exported = compiled.export_artiq(tmp_path)
    manifest = json.loads(exported["manifest"].read_text(encoding="utf-8"))
    assert manifest["verification"]["host_order"]["status"] == "passed"
    results = ResultSet.from_compiled(compiled, handles=["population"])
    assert results.coordinates == [p.coordinates for p in compiled.points]
    assert results.metadata["host_execution_plan"] == compiled.host_execution_plan
    layout = point_execution_layout(compiled.host_execution_plan)
    for ordinal, (point, shot) in enumerate(expected):
        item = layout[point]
        local = (
            shot * item["point_count"] + item["local_point"]
            if mode == "cyclic"
            else item["local_point"] * 2 + shot
        )
        assert item["offset"] + local == ordinal
    assert _payload(compiled)[6]["execution_layout"] == layout[6]


@pytest.mark.parametrize("axis,expected", [(0, [10, 10]), (2, [1] * 20)])
def test_any_host_axis_can_be_chunked(axis, expected):
    compiled = scan(axis=axis)
    assert [
        len(c["points"]) for c in compiled.host_execution_plan["chunks"]
    ] == expected


def test_chunk_size_and_unselected_scope_validation():
    compiled = scan(chunk_size=2)
    assert [len(c["points"]) for c in compiled.host_execution_plan["chunks"]] == [
        4,
        4,
        2,
    ] * 2
    with pytest.raises(CompileError, match="requires a chunked"):
        scan(axis=None, scope="chunk")
    with pytest.raises((CompileError, ValueError), match="exceed"):
        scan(chunks=6)


def test_multiple_chunk_axes_are_rejected():
    q = Qubit("q0")
    exp = Experiment("two_chunk_axes", [q])
    with exp.average(1), exp.sweep(SweepParameter("a", [1, 2]), chunks=1):
        with exp.sweep(SweepParameter("b", [1, 2]), chunk_size=1), exp.realtime():
            play(q.raman, Square(4 * us))
    with pytest.raises(CompileError, match="Only one"):
        reference_lab([q]).compile(exp)
