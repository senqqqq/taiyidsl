"""Circuit layers are full boundaries; targeted barriers remain local."""

import pytest
from test_section_scheduling import drives, gate, lab_for

from taiyidsl import CompileError, Experiment, Qubit, add, barrier, layer, ns, section


def build(body):
    q0, q1 = Qubit("q0"), Qubit("q1")
    exp = Experiment("layers", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        with section(schedule="asap"):
            body(q0, q1)
    return exp


def test_targeted_barrier_does_not_join_unrelated_long_gate():
    def body(q0, q1):
        gate(q0, 400 * ns)
        gate(q1, 1600 * ns)
        barrier(q0)
        gate(q0, 400 * ns)

    events = drives(lab_for().compile(build(body)))
    assert [(e.signal, e.start_mu) for e in events] == [
        ("q0.rf", 0),
        ("q1.rf", 0),
        ("q0.rf", 2000),
    ]


def test_layer_is_a_full_boundary_even_with_disjoint_targets():
    def body(q0, q1):
        gate(q0, 400 * ns)
        with layer(q1):
            gate(q1, 800 * ns)
        with layer(q0):
            gate(q0, 400 * ns)

    assert [e.start_mu for e in drives(lab_for().compile(build(body)))] == [
        0,
        2000,
        6000,
    ]


def test_layer_end_alignment_and_reuse():
    q0, q1 = Qubit("q0"), Qubit("q1")
    with layer(q0, q1, align="end") as saved:
        gate(q0, 400 * ns)
        gate(q1, 800 * ns)
    exp = Experiment("reuse_layer", [q0, q1])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        add(saved)
        add(saved)
    events = drives(lab_for().compile(exp))
    assert [e.start_mu for e in events] == [0, 2000, 4000, 6000]
    assert [e.signal for e in events] == ["q1.rf", "q0.rf", "q1.rf", "q0.rf"]


@pytest.mark.parametrize("kind", ["same_qubit", "alias", "undeclared"])
def test_layer_rejects_branch_ownership_conflicts(kind):
    def body(q0, q1):
        targets = (q0,) if kind == "undeclared" else (q0, q1)
        with layer(*targets):
            gate(q0)
            gate(q0 if kind == "same_qubit" else q1)

    with pytest.raises(CompileError, match="Layer"):
        lab_for(alias=kind == "alias").compile(build(body))


def test_wait_reasons_and_final_dac_overlap_report(tmp_path):
    import json

    def body(q0, q1):
        gate(q0, 400 * ns)
        gate(q1, 1600 * ns)
        barrier(q0)
        gate(q0, 400 * ns)
        with layer(q0):
            gate(q0, 400 * ns)

    compiled = lab_for().compile(build(body))
    events = drives(compiled)
    waiting = events[2].params["section_schedule"][0]
    assert "qubit:q0" in waiting["blocking_resources"]
    assert "qubit:q1" not in waiting["blocking_resources"]
    assert events[3].params["section_schedule"][0]["boundary"] == "layer"
    exported = compiled.export_report(tmp_path)
    report = json.loads(exported["report"].read_text(encoding="utf-8"))
    intervals = report["points"][0]["pulse_overlaps"]
    assert not intervals["truncated"]
    assert len(intervals["intervals"]) == 2
    assert [p["end_us_T0"] - p["start_us_T0"] for p in intervals["intervals"]] == (
        pytest.approx([0.4, 0.4])
    )
