"""The full reference is explicit about stages absent from the example lab."""

import json
import runpy
from pathlib import Path

import pytest

from taiyidsl import LabConfig, OpticsConfig, Qubit
from taiyidsl.experiments.cooling import illuminated_readout

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/yb171_preparation_reference.py"


@pytest.fixture
def reference():
    return runpy.run_path(str(EXAMPLE))


def inputs():
    q = Qubit("q0")
    p = OpticsConfig.load(ROOT / "examples/config/optics/rsc.json")
    lab = LabConfig.load(ROOT / "examples/config/labs/rsc.json", qubit_id=q.uid)
    return q, p, lab


def test_control_segment_has_two_images_and_discloses_external_loading(reference):
    q, p, lab = inputs()
    exp = reference["build_control_segment"](q, p, shots=2, rounds=0)
    compiled = lab.compile(exp)
    events = compiled.points[0].logical_events
    images = [event for event in events if event.kind == "measure"]
    assert [event.params["handle"] for event in images] == ["pre", "post"]
    assert images[0].end_mu < images[1].start_mu
    assert compiled.metadata["sequence_scope"] == "post_loading_segment"
    assert compiled.metadata["physical_validation"] == "not_verified"
    assert "load" in compiled.metadata["external_builders_required"]
    assert compiled.metadata["readout_interpretation"] == "survival"


def test_full_sequence_rejects_missing_builders_before_invoking_any(reference):
    q, p, _ = inputs()
    calls = []
    with pytest.raises(ValueError, match="enter_control.*readout"):
        reference["build_full_sequence"](
            q, p, stage_builders={"load": lambda *args: calls.append("load")}
        )
    assert calls == []


def test_full_sequence_expands_explicit_user_builders_in_order(reference):
    q, p, lab = inputs()
    calls = []

    def load(exp, qubit, parameters):
        calls.append("load")

    def enter_control(exp, qubit, parameters):
        calls.append("enter_control")

    def readout(exp, qubit, parameters, *, handle):
        calls.append("readout")
        illuminated_readout(exp, qubit, parameters, pushout=False, handle=handle)

    exp = reference["build_full_sequence"](
        q,
        p,
        stage_builders={
            "load": load,
            "enter_control": enter_control,
            "readout": readout,
        },
        shots=1,
        rounds=0,
    )
    assert calls == ["load", "enter_control", "readout"]
    compiled = lab.compile(exp)
    assert compiled.metadata["sequence_scope"] == "user_supplied_full_sequence"
    assert compiled.metadata["physical_validation"] == "not_verified"
    # Empty test builders prove composition only, never successful MOT loading.
    assert compiled.metadata["external_builder_validation"] == "not_verified"


def test_reference_manifest_is_serializable_and_independent(reference):
    first = reference["reference_workflow"]()
    second = reference["reference_workflow"]()
    assert "green_mot" in [stage["name"] for stage in first["stages"]]
    assert first["feedback_rearrangement"] is False
    first["stages"].clear()
    assert second["stages"]
    json.dumps(second, allow_nan=False)


def test_main_writes_reference_and_only_supported_compilation(reference, tmp_path):
    reference["main"](tmp_path)
    manifest = json.loads((tmp_path / "workflow.json").read_text(encoding="utf-8"))
    compiled = json.loads(
        (tmp_path / "control_segment.json").read_text(encoding="utf-8")
    )
    assert manifest["implementation"] == "reference_with_external_stages"
    assert compiled["metadata"]["sequence_scope"] == "post_loading_segment"
    assert (tmp_path / "control_segment.html").exists()
    assert not (tmp_path / "full_sequence.py").exists()
