"""Different DDS paths share a shot without sharing output state or adding gaps."""

import math
from dataclasses import replace

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
    rz,
)
from taiyidsl.backends.artiq.validation_polar_output import verify_polar_output


def mixed_lab(*, revision=8, asf=True):
    qubits = [Qubit(f"q{i}") for i in range(3 if asf else 2)]
    common = dict(
        nco_mode="continuous", nco_epoch_latency_mu=80, urukul_proto_rev=revision
    )
    devices = {
        "polar": Device(
            "polar",
            "dds",
            bus="spi0",
            update_group="update0",
            **common,
            ram_destination="powasf",
            ram_latency_mu=112,
            ram_polar_phase_latency_mu=100,
            ram_profile_control=f"urukul_rev{revision}",
            ram_profile_latency_mu=112,
            ram_profile_phase_latency_mu=100,
        ),
        "square": Device(
            "square",
            "dds",
            bus="spi1",
            update_group="update1",
            **common,
            square_latency_mu=80,
        ),
    }
    if asf:
        devices["asf"] = Device(
            "asf",
            "dds",
            bus="spi2",
            update_group="update2",
            **common,
            ram_latency_mu=112,
            ram_pow_latency_mu=80,
            ram_phase_update_duration_mu=480,
            ram_rearm_duration_mu=4000,
        )
    return qubits, LabConfig(
        DeviceSetup(devices),
        SignalMap({q.raman: name for q, name in zip(qubits, devices)}),
    )


def envelope(duration=800):
    return FlatTopGaussian(
        0 * ns,
        sigma=duration / 8 * ns,
        cutoff_sigma=4,
        sample_interval=4 * ns,
        amplitude=0.5,
        frequency=83.123456e6,
    )


def square(duration=800):
    return Square(duration * ns, amplitude=0.5, frequency=83.123456e6)


def selected(compiled, kind, name=None):
    return [
        e
        for e in compiled.points[0].physical_events
        if e.kind == kind and (name is None or e.signal == name)
    ]


@pytest.mark.parametrize("duration", [200, 800])
@pytest.mark.parametrize("revision", [8, 9])
def test_isolated_single_gates_use_all_three_paths_at_the_requested_boundary(
    duration,
    revision,
):
    qubits, lab = mixed_lab(revision=revision)
    exp = Experiment("mixed_single", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        play(qubits[0].raman, envelope(duration))
        play(qubits[1].raman, square(duration))
        play(qubits[2].raman, envelope(duration))
    compiled = lab.compile(exp)
    point = compiled.points[0]
    assert point.duration_mu == duration
    assert {e.signal for e in selected(compiled, "dds_nco_epoch")} == {
        "polar",
        "square",
        "asf",
    }
    assert {e.start_mu - point.origin_mu for e in selected(compiled, "dds_on")} == {0}
    assert {e.start_mu - point.origin_mu for e in selected(compiled, "dds_off")} == {
        duration,
    }
    assert (
        selected(compiled, "dds_polar_effect", "polar")[0].start_mu == point.origin_mu
    )
    assert selected(compiled, "dds_effect", "square")[0].start_mu == point.origin_mu
    assert selected(compiled, "dds_ram_effect", "asf")[0].start_mu == point.origin_mu
    assert verify_polar_output(compiled)["status"] == "passed"


def test_800ns_xy_and_same_axis_other_channels_keep_all_gate_boundaries():
    qubits, lab = mixed_lab()
    exp = Experiment("mixed_800_sequences", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial():
            for index in range(8):
                play(qubits[0].raman, envelope(), phase=index % 2 * math.pi / 2)
        with exp.serial(), exp.repeat(8):
            play(qubits[1].raman, square())
        with exp.serial(), exp.repeat(8):
            play(qubits[2].raman, envelope())
    compiled = lab.compile(exp)
    point = compiled.points[0]
    assert point.duration_mu == 6400
    assert [
        e.start_mu - point.origin_mu for e in selected(compiled, "dds_polar_effect")
    ] == list(range(0, 6400, 800))
    assert [
        e.start_mu - point.origin_mu for e in selected(compiled, "dds_ram_effect")
    ] == list(range(0, 6400, 800))
    assert len(selected(compiled, "dds_prepare")) == 1
    assert len(selected(compiled, "dds_on")) == len(selected(compiled, "dds_off")) == 3
    assert verify_polar_output(compiled)["status"] == "passed"


@pytest.mark.parametrize("revision", [8, 9])
def test_200ns_repetition_is_possible_only_for_same_resident_profile(revision):
    qubits, lab = mixed_lab(revision=revision)
    exp = Experiment("mixed_same_profile_200", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        for index, q in enumerate(qubits):
            with exp.serial(), exp.repeat(8):
                play(q.raman, square(200) if index == 1 else envelope(200))
    compiled = lab.compile(exp)
    point = compiled.points[0]
    assert point.duration_mu == 1600
    assert len(selected(compiled, "dds_polar_select")) == 1
    assert len(selected(compiled, "dds_polar_retrigger")) == 7
    assert len(selected(compiled, "dds_prepare")) == 1
    assert len(selected(compiled, "dds_ram_phase")) == 0
    assert len(selected(compiled, "dds_ram_start")) == 8
    assert verify_polar_output(compiled)["status"] == "passed"


@pytest.mark.parametrize("revision", [8, 9])
@pytest.mark.parametrize("phases", [(0, math.pi / 2), (0.37, 0.37)])
def test_200ns_profile_transition_is_not_mistaken_for_free_retrigger(revision, phases):
    qubits, lab = mixed_lab(revision=revision, asf=False)
    exp = Experiment("profile_bandwidth_200", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial():
            for phase in phases:
                play(qubits[0].raman, envelope(200), phase=phase)
        play(qubits[1].raman, square(800))
    with pytest.raises(CompileError, match="profile selection needs"):
        lab.compile(exp)


def test_square_preparation_can_cross_another_paths_edges_and_selector():
    qubits, lab = mixed_lab(asf=False)
    exp = Experiment("cross_path_prepare", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial():
            play(qubits[0].raman, envelope())
            play(qubits[0].raman, envelope(), phase=math.pi / 2)
        with exp.serial():
            delay(qubits[1].raman, 800 * ns)
            play(qubits[1].raman, square(200))
    compiled = lab.compile(exp)
    point = compiled.points[0]
    prepare = selected(compiled, "dds_prepare")[0]
    selector = selected(compiled, "dds_polar_select")[1]
    assert prepare.start_mu < point.origin_mu < prepare.end_mu
    assert prepare.start_mu < selector.end_mu and selector.start_mu < prepare.end_mu
    assert selected(compiled, "dds_effect")[0].start_mu - point.origin_mu == 800
    assert selected(compiled, "dds_off", "square")[0].start_mu - point.origin_mu == 1000


@pytest.mark.parametrize("resource", ["update_group", "bus"])
def test_mixed_channels_do_not_relax_shared_physical_controls(resource):
    qubits, lab = mixed_lab(asf=False)
    lab.setup.devices["square"] = replace(
        lab.setup.devices["square"],
        **{resource: getattr(lab.setup.devices["polar"], resource)},
    )
    exp = Experiment("shared_mixed_resources", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        play(qubits[0].raman, envelope())
        play(qubits[1].raman, square(200))
    with pytest.raises(CompileError, match="IO_UPDATE|shared profile pins"):
        lab.compile(exp)


def test_native_square_still_rejects_800ns_axis_change_in_a_mixed_shot():
    qubits, lab = mixed_lab(asf=False)
    exp = Experiment("mixed_square_bandwidth", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial(), exp.repeat(2):
            play(qubits[0].raman, envelope())
        with exp.serial():
            play(qubits[1].raman, square())
            play(qubits[1].raman, square(), phase=math.pi / 2)
    with pytest.raises(CompileError, match="preparation window"):
        lab.compile(exp)


def test_rz_updates_mixed_raman_and_rf_paths_on_one_qubit():
    qubits, lab = mixed_lab(asf=False)
    q = qubits[0]
    lab.signal_map[q.rf] = "square"
    exp = Experiment("mixed_rz_targets", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
        with exp.parallel():
            play(q.raman, envelope(200))
            play(q.rf, square(200))
    compiled = lab.compile(exp)
    assert selected(compiled, "dds_polar_effect")[0].params["pow"] == 49152
    assert selected(compiled, "dds_effect")[0].params["pow"] == 49152
    assert verify_polar_output(compiled)["status"] == "passed"
