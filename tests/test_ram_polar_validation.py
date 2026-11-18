"""Output checks use packed words and logical intent, not effect annotations."""

import importlib.util
import math
from dataclasses import replace
from pathlib import Path

import pytest

from taiyidsl import CompileError, ControlBinding, Experiment, play, rz
from taiyidsl.hardware import DeviceCalibration
from taiyidsl.polar_validation import verify_polar_output

_path = Path(__file__).resolve().parents[1] / "examples/powasf_sequence.py"
_spec = importlib.util.spec_from_file_location("powasf_acceptance", _path)
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)


def _reseal(compiled):
    # Corruption tests deliberately bypass the artifact checksum to exercise
    # the independent output checker. User-facing mutation remains forbidden.
    compiled._integrity_hash = compiled._checksum()
    return compiled


def _banks(compiled):
    seen = set()
    for event in compiled.points[0].physical_events:
        bank = event.params.get("polar_bank")
        if bank is not None and id(bank) not in seen:
            seen.add(id(bank))
            yield bank


def _launches(compiled):
    return [
        event
        for event in compiled.points[0].physical_events
        if event.kind in {"dds_polar_select", "dds_polar_retrigger"}
    ]


@pytest.mark.parametrize("name", list(example.acceptance_cases()))
def test_acceptance_outputs_remain_explicitly_offline(name):
    compiled = example.acceptance_cases()[name]()
    result = verify_polar_output(compiled)
    assert result["status"] == "passed"
    assert result["checked_boundaries"] > 0
    assert result["effect_annotations_used_as_oracle"] is False
    assert result["hardware_verified"] is False
    assert result["real_artiq_compile"] == "not_run_by_this_check"
    for channel in result["points"][0]["channels"]:
        assert channel["profiles"] <= 8
        assert channel["ram_words"] <= 1024
        assert abs(channel["constant_nco_quantization_residual_u32"]) < 65536


@pytest.mark.parametrize("name", list(example.rejection_cases()))
def test_unsupported_controls_are_rejected_without_a_changed_experiment(name):
    with pytest.raises(CompileError):
        example.rejection_cases()[name]()


def test_1024_gates_reuse_the_same_small_transition_library():
    short = verify_polar_output(example.build_sequence(count=8))["points"][0][
        "channels"
    ][0]
    long = verify_polar_output(example.build_sequence(count=1024))["points"][0][
        "channels"
    ][0]
    assert short["ram_words"] == long["ram_words"] == 604
    assert short["profiles"] == long["profiles"] == 4
    assert short["library_sha256"] == long["library_sha256"]
    assert long["launches"] == 1024


def test_effect_annotations_are_not_an_output_oracle():
    compiled = example.build_sequence(count=2, terminal_rz=True)
    for event in compiled.points[0].physical_events:
        if event.kind == "dds_polar_effect":
            event.start_mu += 123456
            event.params["pow"] = 111
            event.params["effect_mu"] += 23456
    assert verify_polar_output(_reseal(compiled))["status"] == "passed"


def test_unchanged_asf_metadata_cannot_hide_a_corrupted_playback_word():
    compiled = example.build_sequence(count=2)
    for bank in _banks(compiled):
        bank[1]["words"][100] ^= 4
    with pytest.raises(CompileError, match="asf output mismatch"):
        verify_polar_output(_reseal(compiled))


def test_missing_phase_compensation_is_seen_before_the_first_gate_even_rf_off():
    compiled = example.build_sequence(count=1, phases=(math.pi / 2,))
    for bank in _banks(compiled):
        target = bank[1]["words"][3] & 0xFFFF0000
        for index in range(3):
            bank[1]["words"][index] = target | (bank[1]["words"][index] & 0xFFFF)
    with pytest.raises(CompileError, match="phase output mismatch.*-12 mu"):
        verify_polar_output(_reseal(compiled))


def test_one_sample_early_xy_phase_change_is_rejected_without_zero_tail_exception():
    compiled = example.build_sequence(count=2)
    profile = _launches(compiled)[1].params["polar_profile"]
    for bank in _banks(compiled):
        target = bank[profile]["words"][3] & 0xFFFF0000
        bank[profile]["words"][2] = target | (bank[profile]["words"][2] & 0xFFFF)
    with pytest.raises(CompileError, match="phase output mismatch.*796 mu"):
        verify_polar_output(_reseal(compiled))


def test_terminal_rz_cannot_disappear_behind_a_correct_effect_annotation():
    compiled = example.build_sequence(count=2, terminal_rz=True)
    terminal = _launches(compiled)[-1]
    assert terminal.params["polar_frame_only"]
    compiled.points[0].physical_events.remove(terminal)
    with pytest.raises(CompileError, match="phase output mismatch.*1600 mu"):
        verify_polar_output(_reseal(compiled))


def test_standalone_rz_requires_hardware_even_when_every_launch_is_missing():
    compiled = example.build_standalone_rz()
    compiled.points[0].physical_events[:] = [
        event
        for event in compiled.points[0].physical_events
        if not event.kind.startswith("dds_polar_")
    ]
    with pytest.raises(CompileError, match="Missing POWASF hardware launch"):
        verify_polar_output(_reseal(compiled))


@pytest.mark.parametrize("angles", [(0,), (0.3, -0.3)])
def test_quantized_noop_standalone_frames_need_no_hardware_launch(angles):
    (q,), lab = example.laboratory()
    exp = Experiment("polar_noop_frames", [q])
    with exp.average(1), exp.realtime():
        for angle in angles:
            rz(q, angle)
    compiled = lab.compile(exp)
    assert not _launches(compiled)
    assert verify_polar_output(compiled)["status"] == "not_applicable"


def test_rz_sign_error_in_actual_ram_words_is_rejected():
    compiled = example.build_standalone_rz()
    for bank in _banks(compiled):
        bank[1]["words"][-1] ^= 0x80000000
    with pytest.raises(CompileError, match="phase output mismatch"):
        verify_polar_output(_reseal(compiled))


def test_changing_ftw_is_not_hidden_by_phase_annotations():
    compiled = example.build_sequence(count=2)
    _launches(compiled)[1].params["ftw"] += 1
    with pytest.raises(CompileError, match="changing FTW"):
        verify_polar_output(_reseal(compiled))


def test_wrong_fixed_ftw_is_compared_to_the_logical_carrier_frequency():
    compiled = example.build_sequence(count=2)
    for event in _launches(compiled):
        event.params["ftw"] += 1
    with pytest.raises(CompileError, match="FTW differs from the logical carrier"):
        verify_polar_output(_reseal(compiled))


def test_actual_selection_duration_cannot_be_replaced_with_an_instant_commit():
    compiled = example.build_sequence(count=2)
    _launches(compiled)[1].duration_mu = 0
    with pytest.raises(CompileError, match="selector duration"):
        verify_polar_output(_reseal(compiled))


def test_output_comparison_respects_calibration_mapping_and_frame_sign():
    (q,), lab = example.laboratory()
    lab.signal_map[q.raman] = ControlBinding(
        "dds0", phase_scale=-1, phase_offset=0.13, amplitude_scale=0.8
    )
    lab.setup.calibration["dds0"] = DeviceCalibration(
        latency_mu=16, phase_offset=0.17, amplitude_scale=0.9
    )
    exp = Experiment("mapped_polar_frame", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, example.pulse(), phase=0.27)
        rz(q, 0.37)
        play(q.raman, example.pulse(), phase=0.1)
        rz(q, -0.2)
    compiled = lab.compile(exp)
    result = verify_polar_output(compiled)
    assert result["status"] == "passed"
    assert compiled.points[0].duration_mu == 1600


def test_select_and_retrigger_use_their_distinct_absolute_delay_anchors():
    (q,), lab = example.laboratory()
    lab.setup.devices["dds0"] = replace(
        lab.setup.devices["dds0"],
        ram_latency_mu=120,
        ram_polar_phase_latency_mu=108,
    )
    exp = Experiment("distinct_trigger_anchors", [q])
    with exp.average(1), exp.realtime():
        for phase in (0, math.pi / 2, math.pi / 2, math.pi / 2, 0):
            play(q.raman, example.pulse(), phase=phase)
    compiled = lab.compile(exp)
    assert any(e.kind == "dds_polar_retrigger" for e in _launches(compiled))
    assert verify_polar_output(compiled)["status"] == "passed"


def test_verifier_keeps_compiled_snapshot_integrity_enforcement():
    compiled = example.build_sequence(count=1)
    compiled.points[0].physical_events[0].start_mu += 8
    with pytest.raises(CompileError, match="modified after compilation"):
        verify_polar_output(compiled)


def _driver_trace():
    """Small trace oracle independent of a real ARTIQ installation."""
    origin = 200000
    entries = [
        dict(profile=0, start=0, end=0, step=1, mode=0, words=[0]),
        dict(profile=1, start=1, end=4, step=1, mode=1, words=[0, 0, 0, 0]),
    ]
    params = dict(
        polar_bank=entries,
        polar_profile=1,
        polar_initial_profile=0,
        nco_epoch_release_mu=0,
        nco_initial_pow=0,
        ftw=123,
    )
    events = [
        dict(kind="dds_nco_epoch", start_mu=0, duration_mu=0, signal="dds0", params={}),
        dict(
            kind="dds_nco_epoch_off",
            start_mu=8,
            duration_mu=0,
            signal="dds0",
            params={},
        ),
        dict(
            kind="dds_polar_select",
            start_mu=128,
            duration_mu=416,
            signal="dds0",
            params=params,
        ),
        dict(kind="dds_on", start_mu=656, duration_mu=0, signal="dds0", params={}),
        dict(kind="dds_off", start_mu=856, duration_mu=0, signal="dds0", params={}),
    ]
    state = {"0": (1 << 31) | (3 << 29), "1": 1 << 7, "7": 123}
    records = [
        dict(
            timestamp_mu=origin - 8,
            device="dds0",
            kind="io_update",
            profile=0,
            state={**state, "0": state["0"] | (1 << 11)},
        ),
        dict(
            timestamp_mu=origin, device="dds0", kind="io_update", profile=0, state=state
        ),
        dict(
            timestamp_mu=origin + 544,
            device="dds0",
            kind="profile_select",
            profile=1,
            state=dict(state),
        ),
    ]
    banks = {
        ("dds0", entry["profile"]): {
            "words": list(entry["words"]),
            "profile_words": (
                (entry["step"] << 8) | (entry["end"] >> 2),
                (entry["start"] << 14) | ((entry["end"] & 3) << 30) | entry["mode"],
            ),
            "end_mu": origin - 16,
        }
        for entry in entries
    }
    return dict(
        point={"index": 0, "physical_events": events},
        writes=[],
        edges=[
            dict(timestamp_mu=origin + time, target=target, value=value, timed=True)
            for time, target, value in (
                (0, 512, 1),
                (8, 512, 0),
                (656, 768, 1),
                (856, 768, 0),
            )
        ],
        records=records,
        selections=[
            dict(device="dds0", start_mu=origin + 128, end_mu=origin + 544, profile=1)
        ],
        banks=banks,
        targets={"dds0": {"update": 512, "switch": 768}},
        origin=origin,
    )


@pytest.mark.parametrize(
    "fault", [None, "selector", "payload", "clear", "extra_update"]
)
def test_real_driver_audit_rejects_bad_actual_traces_without_hardware(fault):
    path = Path(__file__).resolve().parents[1] / "scripts/verify_polar_driver.py"
    spec = importlib.util.spec_from_file_location("polar_real_audit", path)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    trace = _driver_trace()
    if fault == "selector":
        trace["selections"][0]["end_mu"] += 8
    elif fault == "payload":
        trace["banks"][("dds0", 1)]["words"][3] = 65536
    elif fault == "clear":
        trace["records"][-1]["state"]["0"] |= 1 << 11
    elif fault == "extra_update":
        trace["edges"].append(
            dict(timestamp_mu=trace["origin"] + 544, target=512, value=1, timed=True)
        )
    if fault is None:
        assert audit.compare_trace(**trace)["selector_durations_mu"] == [416]
    else:
        with pytest.raises(ValueError):
            audit.compare_trace(**trace)
