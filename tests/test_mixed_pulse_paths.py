"""Mixed playback preserves logical boundaries and classifies short-gate limits."""

import importlib.util
import json
from pathlib import Path

import pytest

from taiyidsl import CompileError
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_equivalence, verify_export

_path = Path(__file__).resolve().parents[1] / "examples/mixed_pulse_paths.py"
_spec = importlib.util.spec_from_file_location("mixed_path_examples", _path)
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)


@pytest.mark.parametrize("name", list(example.acceptance_cases()))
def test_mixed_export_examples_preserve_all_three_paths(name):
    compiled = example.acceptance_cases()[name]()
    report = verify_equivalence(compiled)
    assert report["status"] == "passed"
    assert report["hardware"] == "not_verified"
    point = compiled.points[0]
    kinds = {e.kind for e in point.physical_events}
    assert "dds_prepare" in kinds
    assert "dds_polar_effect" in kinds
    if "three_paths" in name:
        assert "dds_ram_effect" in kinds


@pytest.mark.parametrize("name", list(example.rejection_cases()))
def test_short_gate_and_shared_resource_failures_are_not_hidden_by_mixing(name):
    with pytest.raises(CompileError):
        example.rejection_cases()[name]()


@pytest.mark.parametrize("duration", [200, 800])
def test_mixed_single_gates_keep_exact_synchronized_output_and_rf_edges(duration):
    compiled = example.build_mixed(duration_ns=duration, count=1, include_asf=True)
    point = compiled.points[0]
    assert point.duration_mu == duration
    for name, effect_kind in (
        ("polar_dds", "dds_polar_effect"),
        ("square_dds", "dds_effect"),
        ("asf_dds", "dds_ram_effect"),
    ):
        effects = [
            e
            for e in point.physical_events
            if e.signal == name and e.kind == effect_kind
        ]
        assert len(effects) == 1
        assert effects[0].start_mu == point.origin_mu
        switches = [
            (e.kind, e.start_mu - point.origin_mu)
            for e in point.physical_events
            if e.signal == name and e.kind in {"dds_on", "dds_off"}
        ]
        assert switches == [("dds_on", 0), ("dds_off", duration)]
    verify_export(compiled)


def test_200ns_offset_edges_can_be_submitted_inside_independent_profile_spi():
    compiled = example.acceptance_cases()["mixed_200_offset_in_800_xy"]()
    point = compiled.points[0]
    selectors = [e for e in point.physical_events if e.kind == "dds_polar_select"]
    second = selectors[1]
    square_edges = [
        e
        for e in point.physical_events
        if e.signal == "square_dds" and e.kind in {"dds_on", "dds_off"}
    ]
    assert [e.start_mu - point.origin_mu for e in square_edges] == [400, 600]
    assert all(second.start_mu < e.start_mu < second.end_mu for e in square_edges)
    verify_export(compiled)


@pytest.mark.parametrize("revision, duration", [(8, 416), (9, 896)])
def test_200ns_same_profile_replay_is_not_a_claim_of_200ns_phase_switching(
    revision, duration
):
    compiled = example.build_mixed(
        duration_ns=200,
        count=4,
        polar_phases=(0.0,),
        selector=f"urukul_rev{revision}",
        square_duration_ns=800,
        square_count=1,
    )
    point = compiled.points[0]
    selectors = [e for e in point.physical_events if e.kind == "dds_polar_select"]
    retriggers = [e for e in point.physical_events if e.kind == "dds_polar_retrigger"]
    effects = [e for e in point.physical_events if e.kind == "dds_polar_effect"]
    assert len(selectors) == 1
    assert selectors[0].duration_mu == duration
    assert len(retriggers) == 3
    assert [e.start_mu - point.origin_mu for e in effects] == [0, 200, 400, 600]
    assert len({e.params["polar_profile"] for e in effects}) == 1
    assert len({e.params["pow"] for e in effects}) == 1
    with pytest.raises(CompileError, match="896 mu"):
        example.build_mixed(duration_ns=200, count=4, selector="urukul_rev9")


def test_200ns_same_axis_can_still_need_two_transition_profiles():
    # Initial X->Y and later Y->Y require different prefixes. This is not the
    # same-profile control case, even though every requested gate is a Y gate.
    import math

    with pytest.raises(CompileError, match="profile selection"):
        example.build_mixed(duration_ns=200, count=4, polar_phases=(math.pi / 2,))


@pytest.mark.parametrize("standalone", [False, True])
def test_one_qubit_hardware_rz_updates_both_mapped_playback_modes(standalone):
    compiled = example.build_joint_frame(standalone=standalone)
    point = compiled.points[0]
    when = 0 if standalone else 2400
    assert point.duration_mu == when
    commands = [
        e
        for e in point.physical_events
        if e.kind in {"dds_effect", "dds_polar_effect"}
        and e.start_mu == point.origin_mu + when
    ]
    assert {e.signal for e in commands} == {"polar_dds", "square_dds"}
    assert {e.params["command_kind"] for e in commands} == {"rz"}
    assert all(e.params["pow"] == 49152 for e in commands)
    source = generate_source(compiled)
    assert ".write64(" in source
    assert ".cpld.set_profile(" in source
    verify_export(compiled)


def test_exported_contracts_retain_original_playback_modes(tmp_path):
    compiled = example.build_mixed(include_asf=True, duration_ns=200, count=1)
    paths = compiled.export_artiq(tmp_path)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["devices"]["polar_dds"]["ram_destination"] == "powasf"
    assert manifest["devices"]["asf_dds"]["ram_destination"] == "asf"
    assert manifest["devices"]["square_dds"]["square_latency_mu"] == 80
    assert manifest["verification"]["hardware"] == "not_verified"
