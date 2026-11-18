"""Continuous accumulation, strict phase deadlines and bounded waveform reuse."""

import math
from dataclasses import replace

import pytest

from taiyidsl import (
    CompileError,
    ControlBinding,
    ControlRecipe,
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


def continuous_lab(count=1, *, lp=80, shared=False, **overrides):
    qubits = [Qubit(f"q{i}") for i in range(count)]
    settings = dict(
        nco_mode="continuous",
        nco_epoch_latency_mu=80,
        square_latency_mu=80,
        ram_latency_mu=112,
        ram_pow_latency_mu=lp,
        ram_phase_update_duration_mu=480,
        ram_rearm_duration_mu=4000,
    )
    settings.update(overrides)
    devices = {
        f"dds{i}": Device(
            f"dds{i}",
            "dds",
            bus=f"spi{0 if shared else i}",
            update_group=f"board{0 if shared else i}",
            **settings,
        )
        for i in range(count)
    }
    return qubits, LabConfig(
        DeviceSetup(devices),
        SignalMap({q.raman: f"dds{i}" for i, q in enumerate(qubits)}),
    )


def pulse(shape="ram", duration=800, *, cutoff=5):
    if shape == "square":
        return Square(duration * ns, amplitude=0.5, frequency=83.123456e6)
    return FlatTopGaussian(
        0 * ns,
        sigma=(duration / (2 * cutoff)) * ns,
        cutoff_sigma=cutoff,
        sample_interval=4 * ns,
        amplitude=0.5,
        frequency=83.123456e6,
    )


def selected(compiled, kind, name=None):
    return [
        event
        for event in compiled.points[0].physical_events
        if event.kind == kind and (name is None or event.signal == name)
    ]


def relative(compiled, event):
    return event.start_mu - compiled.points[0].origin_mu


@pytest.mark.parametrize("shape", ["square", "ram"])
@pytest.mark.parametrize("duration", [200, 800])
def test_arbitrary_ftw_same_axis_replays_have_one_continuous_epoch(shape, duration):
    (q,), lab = continuous_lab()
    exp = Experiment("continuous_replay", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.repeat(8):
                play(q.raman, pulse(shape, duration, cutoff=4), phase=0.37)
    compiled = lab.compile(exp)
    point = compiled.points[0]
    assert point.duration_mu == 8 * duration
    assert len(selected(compiled, "dds_nco_epoch")) == 1
    epoch = selected(compiled, "dds_nco_epoch")[0]
    assert all(
        epoch.end_mu + 8 < event.start_mu
        for event in point.physical_events
        if event.kind not in {"dds_nco_epoch", "dds_nco_epoch_off"}
    )
    effect_kind = "dds_effect" if shape == "square" else "dds_ram_effect"
    states = selected(compiled, effect_kind)
    word = "square_pow" if shape == "square" else "ram_pow"
    assert len({event.params[word] for event in states}) == 1
    if shape == "ram":
        assert [relative(compiled, event) for event in states] == [
            duration * index for index in range(8)
        ]
        assert not selected(compiled, "dds_ram_phase")
        assert {e.params["ram_profile"] for e in states} == {0}
    else:
        assert len(selected(compiled, "dds_prepare")) == 1
    first = states[0].params
    epoch_relative = first["nco_epoch_effect_mu"] - point.origin_mu
    correction = (epoch_relative * first["ftw"]) >> 16
    assert first[word] == (first["pow"] + correction) & 0xFFFF
    # At every requested gate boundary the same 32-bit running phase applies;
    # the accumulator's low 16 bits are never discarded by a per-gate reset.
    for time in range(0, point.duration_mu, duration):
        accumulated = ((time - epoch_relative) * first["ftw"]) % 2**32
        actual = (accumulated + (first[word] << 16)) % 2**32
        expected = (time * first["ftw"] + (first["pow"] << 16)) % 2**32
        error = (actual - expected + 2**31) % 2**32 - 2**31
        assert abs(error) < 2**16


def test_missing_new_epoch_or_pow_latency_never_reuses_legacy_contract():
    for missing in ("nco_epoch_latency_mu", "ram_pow_latency_mu"):
        (q,), lab = continuous_lab(**{missing: None}, ram_phase_latency_mu=80)
        exp = Experiment(f"missing_{missing}", [q])
        with exp.average(1):
            with exp.realtime():
                play(q.raman, pulse())
        with pytest.raises(CompileError, match=missing):
            lab.compile(exp)


@pytest.mark.parametrize("style", ["direct", "shift", "rz"])
def test_strict_ram_phase_boundary_rejects_even_a_sufficient_original_zero_tail(style):
    (q,), lab = continuous_lab(lp=80)
    exp = Experiment("strict_zero_tail", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse())
            if style == "shift":
                shift_phase(q.frame, math.pi / 2)
            elif style == "rz":
                rz(q, -math.pi / 2)
            play(q.raman, pulse(), phase=math.pi / 2 if style == "direct" else 0)
    with pytest.raises(CompileError, match="(?i)(strict|mismatch)"):
        lab.compile(exp)


def test_equal_latency_ram_800ns_parallel_preparation_is_a_simulated_contract():
    qs, lab = continuous_lab(2, lp=112)
    exp = Experiment("equal_latency_simulation", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qs:
                    with exp.serial():
                        for phase in (0, math.pi / 2, 0, math.pi / 2):
                            play(q.raman, pulse(), phase=phase)
    compiled = lab.compile(exp)
    writes = selected(compiled, "dds_ram_phase")
    assert len(writes) == 6
    assert writes[0].signal != writes[1].signal
    assert writes[0].start_mu == writes[1].start_mu
    assert compiled.points[0].duration_mu == 3200
    assert lab.setup.timing_verified is False


@pytest.mark.parametrize("shape", ["square", "ram"])
def test_frequency_change_inside_continuous_domain_is_rejected(shape):
    (q,), lab = continuous_lab(lp=112)
    exp = Experiment("fixed_ftw_only", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse(shape))
            delay(q.raman, 8000 * ns)
            play(q.raman, pulse(shape), frequency=84e6)
    with pytest.raises(CompileError, match="fixed FTW"):
        lab.compile(exp)


def test_continuous_ram_does_not_disable_mode_to_change_resident_profile():
    (q,), lab = continuous_lab()
    exp = Experiment("no_hidden_ftw_source_change", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse())
            delay(q.raman, 8000 * ns)
            play(q.raman, pulse(), amplitude=0.25)
    with pytest.raises(CompileError, match="mode-preserving"):
        lab.compile(exp)


@pytest.mark.parametrize("command", ["rz", "shift", "reset"])
def test_terminal_ram_command_has_exact_carrier_time_without_following_play(command):
    (q,), lab = continuous_lab(lp=80)
    exp = Experiment("terminal_hardware_phase", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse(), phase=0.3)
            if command == "rz":
                rz(q, 0.4)
            elif command == "shift":
                shift_phase(q.frame, 0.4)
            else:
                reset_frame(q.frame)
    compiled = lab.compile(exp)
    updates = [
        event
        for event in selected(compiled, "dds_ram_start")
        if event.params["ram_frame_only"]
    ]
    assert len(updates) == 1
    event = updates[0]
    assert event.params["carrier_effect_mu"] - compiled.points[0].origin_mu == 800
    assert event.params["effect_mu"] - compiled.points[0].origin_mu == 832
    assert selected(compiled, "dds_ram_phase")
    assert len(selected(compiled, "dds_on")) == 1


def test_shared_square_commit_does_not_rewrite_unchanged_channel():
    qs, lab = continuous_lab(2, shared=True)
    exp = Experiment("shared_continuous_hold", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(qs[0].raman, pulse("square"))
                    delay(qs[0].raman, 2400 * ns)
                    play(qs[0].raman, pulse("square"), phase=0.4)
                play(qs[1].raman, pulse("square", 4000))
    compiled = lab.compile(exp)
    assert len(selected(compiled, "dds_prepare", "dds0")) == 2
    assert len(selected(compiled, "dds_prepare", "dds1")) == 1
    assert all(
        event.params["commit_members"] == ["dds0", "dds1"]
        for event in selected(compiled, "dds_commit")
    )


def test_mixed_nco_policies_on_shared_update_are_rejected():
    qs, lab = continuous_lab(2, shared=True)
    lab.setup.devices["dds1"] = replace(
        lab.setup.devices["dds1"], nco_mode="legacy_tracking"
    )
    exp = Experiment("mixed_epoch_contract", qs)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qs:
                    play(q.raman, pulse("square"))
    with pytest.raises(CompileError, match="mix NCO"):
        lab.compile(exp)


def test_rz_cannot_change_an_active_non_drive_alias_on_its_physical_dds():
    (q,), lab = continuous_lab()
    lab.setup.devices["clock"] = Device("clock", "ttl")
    lab.signal_map[q.readout] = "clock"
    lab.signal_map["q0.illumination"] = "dds0"
    exp = Experiment("rz_physical_alias_conflict", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    delay(q.readout, 2000 * ns)
                    rz(q, 0.3)
                play("q0.illumination", pulse("square", 4000))
    with pytest.raises(CompileError, match="(?i)(active|boundary|alias|conflict)"):
        lab.compile(exp)


def test_standalone_rz_rejects_conflicting_default_states_for_one_dds():
    (q,), lab = continuous_lab()
    lab.signal_map[q.rf] = ControlRecipe(
        (ControlBinding("dds0", frequency_offset=1e6),)
    )
    exp = Experiment("rz_ambiguous_held_state", [q])
    with exp.average(1):
        with exp.realtime():
            rz(q, 0.3)
    with pytest.raises(
        CompileError, match="(?i)(initial|default|inconsistent|ambiguous)"
    ):
        lab.compile(exp)
