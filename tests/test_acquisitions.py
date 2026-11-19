"""One camera exposure can be indexed to several fixed array sites."""

import pytest

from taiyidsl import CompileError, Experiment, Qubit, delay, measure, reference_lab, us
from taiyidsl.backends.artiq.verification import verify_equivalence
from taiyidsl.dsl import acquire


def shared_camera():
    qubits = [Qubit("q0"), Qubit("q1")]
    exp = Experiment("shared_camera", qubits)
    with exp.average(2):
        with exp.realtime():
            for handle in ("pre", "post"):
                acquire(
                    "q0.readout",
                    duration=10 * us,
                    handle=handle,
                    qubits=qubits,
                    metadata={"detector": "camera0", "exposure_s": 100e-6},
                )
                delay("q0.readout", 100 * us)
    return exp, reference_lab(qubits)


def test_shared_frame_has_one_trigger_and_multiple_target_sites():
    exp, lab = shared_camera()
    compiled = lab.compile(exp)
    triggers = [e for e in compiled.points[0].logical_events if e.kind == "trigger"]
    assert len(triggers) == 2
    assert [e.start_mu for e in triggers] == [0, 110000]
    assert (
        len([e for e in compiled.points[0].physical_events if e.kind == "ttl_on"]) == 2
    )
    plan = compiled.acquisition_plan()
    assert [a["handle"] for a in plan] == ["pre", "post"]
    assert plan[0]["qubits"] == ["q0", "q1"]
    assert plan[0]["duration_s"] == pytest.approx(10e-6)
    assert plan[0]["metadata"]["exposure_s"] == 100e-6
    plan[0]["metadata"]["detector"] = "mutated"
    assert compiled.acquisition_plan()[0]["metadata"]["detector"] == "camera0"
    assert verify_equivalence(compiled)["status"] == "passed"


def test_shared_acquisition_cannot_overwrite_single_site_measurement():
    q = Qubit("q0")
    exp = Experiment("duplicate", [q])
    with exp.average(1):
        with exp.realtime():
            acquire(q.readout, duration=10 * us, handle="post", qubits=[q])
            measure(q, handle="post")
    with pytest.raises(CompileError, match="once per shot"):
        reference_lab([q]).compile(exp)


def test_unknown_site_fails_before_export():
    q = Qubit("q0")
    exp = Experiment("unknown_site", [q])
    with exp.average(1):
        with exp.realtime():
            acquire(q.readout, duration=10 * us, handle="post", qubits=["missing"])
    with pytest.raises(CompileError, match="[Qq]ubit|[Ss]ite"):
        reference_lab([q]).compile(exp)


@pytest.mark.parametrize("point", [-1, 1, True])
def test_acquisition_plan_rejects_invalid_point(point):
    exp, lab = shared_camera()
    with pytest.raises(IndexError):
        lab.compile(exp).acquisition_plan(point)


@pytest.mark.parametrize("legacy", [False, True])
def test_adjacent_detector_triggers_require_explicit_low_time(legacy):
    q = Qubit("q0")
    exp = Experiment("adjacent_frames", [q])
    with exp.average(1):
        with exp.realtime():
            for handle in ("pre", "post"):
                if legacy:
                    measure(q, handle=handle)
                else:
                    acquire(q.readout, duration=10 * us, handle=handle, qubits=[q])
    with pytest.raises(CompileError, match="[Tt]rigger|[Gg]ap|[Ll]ow"):
        reference_lab([q]).compile(exp)
