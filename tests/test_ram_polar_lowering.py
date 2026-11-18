"""Bounded POWASF transition banks and exact hardware-frame deadlines."""

import math

import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    delay,
    ns,
    play,
    reset_frame,
    rz,
    shift_phase,
)
from taiyidsl.waveforms import ad9910_amplitude_ram


def polar_lab(count=1, *, shared=False, **overrides):
    settings = dict(
        nco_mode="continuous",
        nco_epoch_latency_mu=80,
        ram_destination="powasf",
        ram_latency_mu=112,
        ram_polar_phase_latency_mu=100,
        ram_profile_latency_mu=112,
        ram_profile_phase_latency_mu=100,
        ram_profile_control="urukul_rev8",
    )
    settings.update(overrides)
    qubits = [Qubit(f"q{i}") for i in range(count)]
    devices = {
        f"dds{i}": Device(
            f"dds{i}",
            "dds",
            bus=f"spi{0 if shared else i}",
            update_group=f"update{i}",
            **settings,
        )
        for i in range(count)
    }
    return qubits, LabConfig(
        DeviceSetup(devices),
        SignalMap({q.raman: f"dds{i}" for i, q in enumerate(qubits)}),
    )


def shaped(duration=800, sample=4):
    return FlatTopGaussian(
        0 * ns,
        sigma=duration / 8 * ns,
        cutoff_sigma=4,
        sample_interval=sample * ns,
        amplitude=0.5,
        frequency=83.123456e6,
    )


def effects(compiled):
    return [
        e for e in compiled.points[0].physical_events if e.kind == "dds_polar_effect"
    ]


def test_800ns_xy_has_unchanged_asf_and_constant_size_transition_library():
    (q,), lab = polar_lab()
    exp = Experiment("polar_xy", [q])
    with exp.average(1), exp.realtime(), exp.repeat(64):
        play(q.raman, shaped(), phase=0)
        play(q.raman, shaped(), phase=math.pi / 2)
    compiled = lab.compile(exp)
    point, states = compiled.points[0], effects(compiled)
    assert point.duration_mu == 128 * 800
    assert [e.start_mu - point.origin_mu for e in states] == list(
        range(0, 128 * 800, 800)
    )
    bank = states[0].params["polar_bank"]
    assert len(bank) == 4
    assert sum(len(p["words"]) for p in bank) == 604
    expected = ad9910_amplitude_ram(states[0].params, 1e9, 800e-9)["ram_asf"]
    assert all(p["asf"] == expected for p in bank[1:])
    assert all([(word >> 2) & 0x3FFF for word in p["words"]] == p["asf"] for p in bank)
    for state in states:
        p = state.params
        entry = bank[p["polar_profile"]]
        assert entry["pow_samples"][:3] == [p["incoming_pow"]] * 3
        assert entry["pow_samples"][3:] == [p["ram_pow"]] * 198
        assert p["polar_commit_mu"] + p["polar_phase_latency_mu"] + 12 == state.start_mu
    assert len([e for e in point.physical_events if e.kind == "dds_on"]) == 1
    assert len([e for e in point.physical_events if e.kind == "dds_off"]) == 1
    assert len([e for e in point.physical_events if e.kind == "dds_nco_epoch"]) == 1
    first = states[0].params
    correction = ((first["nco_epoch_effect_mu"] - point.origin_mu) * first["ftw"]) >> 16
    assert bank[0]["pow_samples"] == [correction & 0xFFFF]
    assert all(
        p.params["ram_pow"] == (p.params["pow"] + correction) & 0xFFFF for p in states
    )


def test_rev9_profile_throughput_is_separate_from_phase_alignment():
    (q,), lab = polar_lab(ram_profile_control="urukul_rev9")
    exp = Experiment("slow_selector", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, shaped(), phase=0)
        play(q.raman, shaped(), phase=math.pi / 2)
    with pytest.raises(CompileError, match="896 mu.*urukul_rev9"):
        lab.compile(exp)


@pytest.mark.parametrize("duration", [200, 800])
def test_two_independent_spis_preserve_synchronized_single_gate(duration):
    qubits, lab = polar_lab(2)
    exp = Experiment("two_polar_boards", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        for q in qubits:
            play(q.raman, shaped(duration), phase=0.3)
    compiled = lab.compile(exp)
    assert len(effects(compiled)) == 2
    assert len({e.start_mu for e in effects(compiled)}) == 1
    selects = [
        e for e in compiled.points[0].physical_events if e.kind == "dds_polar_select"
    ]
    assert len({e.start_mu for e in selects}) == 1
    assert all(e.duration_mu == 416 for e in selects)


def test_shared_rev8_profile_pins_are_rejected():
    qubits, lab = polar_lab(2, shared=True)
    exp = Experiment("shared_profiles", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        for q in qubits:
            play(q.raman, shaped())
    with pytest.raises(CompileError, match="shared profile pins"):
        lab.compile(exp)


def test_independent_and_terminal_rz_have_hardware_hold_and_correct_sign():
    (q,), lab = polar_lab()
    exp = Experiment("polar_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
        delay(q.raman, 800 * ns)
        play(q.raman, shaped())
        rz(q, -math.pi / 4)
        delay(q.raman, 800 * ns)
        shift_phase(q.frame, math.pi / 8)
        delay(q.raman, 800 * ns)
        reset_frame(q.frame)
    compiled = lab.compile(exp)
    states = effects(compiled)
    assert [p.params["pow"] for p in states] == [49152, 49152, 57344, 61440, 0]
    assert [p.start_mu - compiled.points[0].origin_mu for p in states] == [
        0,
        800,
        1600,
        2400,
        3200,
    ]
    for event in states:
        if not event.params["polar_frame_only"]:
            continue
        entry = event.params["polar_bank"][event.params["polar_profile"]]
        assert entry["asf"] == [0] * 4
        assert entry["mode"] == 1
        assert entry["pow_samples"][-1] == event.params["ram_pow"]
    assert states[0].params["polar_bank"][0]["mode"] == 0


def test_terminal_rz_without_any_play_is_not_software_only():
    (q,), lab = polar_lab()
    exp = Experiment("rz_only", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
    compiled = lab.compile(exp)
    assert len(effects(compiled)) == 1
    assert effects(compiled)[0].params["pow"] == 49152
    assert any(e.kind == "dds_polar_select" for e in compiled.points[0].physical_events)
    assert not any(e.kind == "dds_on" for e in compiled.points[0].physical_events)


def test_same_time_rz_and_play_merge_once():
    (q,), lab = polar_lab()
    exp = Experiment("merged_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
        play(q.raman, shaped())
    compiled = lab.compile(exp)
    assert len(effects(compiled)) == 1
    assert effects(compiled)[0].params["pow"] == 49152
    assert not effects(compiled)[0].params["polar_frame_only"]


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"ram_polar_phase_latency_mu": None}, "requires explicit"),
        ({"ram_profile_phase_latency_mu": 96}, "equal nonnegative"),
        ({"nco_mode": "legacy_tracking"}, "requires nco_mode"),
    ],
)
def test_missing_or_inconsistent_contract_rejected(settings, message):
    (q,), lab = polar_lab(**settings)
    exp = Experiment("invalid_contract", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, shaped())
    with pytest.raises(CompileError, match=message):
        lab.compile(exp)


def test_nonintegral_compensation_does_not_resample_envelope():
    (q,), lab = polar_lab()
    exp = Experiment("sample_mismatch", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, shaped(sample=20))
    with pytest.raises(CompileError, match="original sample interval"):
        lab.compile(exp)


def test_native_square_is_not_silently_converted_to_ram():
    (q,), lab = polar_lab()
    exp = Experiment("square_polar", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, Square(800 * ns, frequency=83e6))
    with pytest.raises(CompileError, match="Square/RAM mixing"):
        lab.compile(exp)


@pytest.mark.parametrize("duration, count", [(200, 8), (800, 6)])
def test_resident_profile_or_word_capacity_is_not_hidden_by_concatenation(
    duration, count
):
    (q,), lab = polar_lab()
    exp = Experiment("polar_capacity", [q])
    with exp.average(1), exp.realtime():
        for index in range(count):
            play(q.raman, shaped(duration), phase=index * 0.2)
            if duration == 200:
                # This explicit experimental idle isolates the profile-count
                # limit from the acknowledged 200 ns selector throughput limit.
                delay(q.raman, 600 * ns)
    with pytest.raises(CompileError, match="8 profiles or 1024 words"):
        lab.compile(exp)


def test_arbitrary_independent_rz_angles_consume_bounded_hold_profiles():
    (q,), lab = polar_lab()
    exp = Experiment("rz_capacity", [q])
    with exp.average(1), exp.realtime():
        for _ in range(8):
            rz(q, 0.2)
            delay(q.raman, 800 * ns)
    with pytest.raises(CompileError, match="8 profiles or 1024 words"):
        lab.compile(exp)


def test_shared_update_group_rejected_even_with_independent_spi():
    q0, q1 = Qubit("q0"), Qubit("q1")
    _, original = polar_lab(2)
    from dataclasses import replace

    devices = {
        name: replace(device, update_group="shared")
        for name, device in original.setup.devices.items()
    }
    lab = LabConfig(
        DeviceSetup(devices), SignalMap({q0.raman: "dds0", q1.raman: "dds1"})
    )
    exp = Experiment("shared_update", [q0, q1])
    with exp.average(1), exp.realtime(), exp.parallel():
        play(q0.raman, shaped())
        play(q1.raman, shaped())
    with pytest.raises(CompileError, match="exclusive active IO_UPDATE"):
        lab.compile(exp)


def test_all_mapped_rz_targets_get_real_hardware_state():
    (q, _), original = polar_lab(2)
    lab = LabConfig(original.setup, SignalMap({q.raman: "dds0", q.rf: "dds1"}))
    exp = Experiment("all_rz_targets", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, shaped())
        rz(q, math.pi / 2)
    compiled = lab.compile(exp)
    terminal = [e for e in effects(compiled) if e.params["polar_frame_only"]]
    assert {e.signal for e in terminal} == {"dds0", "dds1"}
    assert all(e.params["pow"] == 49152 for e in terminal)
    assert len({e.start_mu for e in terminal}) == 1
    assert not any(
        e.kind == "dds_on" and e.signal == "dds1"
        for e in compiled.points[0].physical_events
    )


def test_future_gate_does_not_leak_its_axis_before_independent_rz():
    (q,), lab = polar_lab()
    exp = Experiment("no_future_axis", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
        delay(q.raman, 800 * ns)
        play(q.raman, shaped(), phase=math.pi / 4)
    compiled = lab.compile(exp)
    before, gate = effects(compiled)
    assert before.params["pow"] == 49152
    assert gate.params["pow"] == 57344
    correction = before.params["nco_epoch_pow"]
    initial = before.params["polar_bank"][0]
    assert initial["pow_samples"] == [correction]
    entry = before.params["polar_bank"][before.params["polar_profile"]]
    assert entry["pow_samples"][:3] == [correction] * 3


def test_too_close_rz_then_gate_rejects_early_phase_exposure():
    (q,), lab = polar_lab()
    exp = Experiment("nearby_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
        delay(q.raman, 8 * ns)
        play(q.raman, shaped())
    with pytest.raises(CompileError, match="future phase"):
        lab.compile(exp)


def test_fixed_ftw_domain_rejects_frequency_change():
    (q,), lab = polar_lab()
    exp = Experiment("polar_frequency_change", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, shaped())
        play(q.raman, shaped(), frequency=90e6)
    with pytest.raises(CompileError, match="fixed FTW"):
        lab.compile(exp)


def test_rev9_same_profile_retrigger_needs_no_profile_selection_bandwidth():
    (q,), lab = polar_lab(ram_profile_control="urukul_rev9")
    exp = Experiment("same_axis_rev9", [q])
    with exp.average(1), exp.realtime(), exp.repeat(8):
        play(q.raman, shaped(), phase=0)
    compiled = lab.compile(exp)
    events = compiled.points[0].physical_events
    assert len([e for e in events if e.kind == "dds_polar_select"]) == 1
    assert len([e for e in events if e.kind == "dds_polar_retrigger"]) == 7
    assert compiled.points[0].duration_mu == 6400


@pytest.mark.parametrize(
    "settings",
    [
        {"ram_latency_mu": 104, "ram_polar_phase_latency_mu": 92},
        {"ram_latency_mu": 112, "ram_polar_phase_latency_mu": 92},
    ],
)
def test_io_update_latencies_cover_matched_ram_pipeline_minima(settings):
    (q,), lab = polar_lab(**settings)
    exp = Experiment("short_pipeline", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, shaped())
    with pytest.raises(CompileError, match="106 envelope and 94 phase"):
        lab.compile(exp)


def test_explicit_equal_pipeline_contract_needs_no_phase_prefix():
    (q,), lab = polar_lab(
        ram_polar_phase_latency_mu=112, ram_profile_phase_latency_mu=112
    )
    exp = Experiment("equal_pipeline", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, shaped(), phase=0)
        play(q.raman, shaped(), phase=math.pi / 2)
    compiled = lab.compile(exp)
    for state in effects(compiled):
        p = state.params
        assert p["polar_phase_prefix_samples"] == 0
        entry = p["polar_bank"][p["polar_profile"]]
        assert entry["pow_samples"] == [p["ram_pow"]] * len(entry["asf"])


def test_selector_and_io_update_have_independent_absolute_latencies():
    (q,), lab = polar_lab(ram_latency_mu=120, ram_polar_phase_latency_mu=108)
    exp = Experiment("different_trigger_latencies", [q])
    with exp.average(1), exp.realtime(), exp.repeat(4):
        play(q.raman, shaped(), phase=0)
    compiled = lab.compile(exp)
    states = effects(compiled)
    assert [p.params["polar_amplitude_latency_mu"] for p in states] == [
        112,
        120,
        120,
        120,
    ]
    assert [e.start_mu - compiled.points[0].origin_mu for e in states] == [
        0,
        800,
        1600,
        2400,
    ]


def test_same_time_cumulative_frames_commit_final_state_and_preserve_ids():
    (q,), lab = polar_lab()
    exp = Experiment("merged_frames", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 4)
        rz(q, math.pi / 4)
        shift_phase(q.frame, math.pi / 8)
        reset_frame(q.frame)
        rz(q, math.pi / 8)
    compiled = lab.compile(exp)
    states = effects(compiled)
    assert len(states) == 1
    assert states[0].params["pow"] == 61440
    assert len(states[0].logical_ids) == 5
