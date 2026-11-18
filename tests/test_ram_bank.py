"""Resident RAM capacity depends on distinct waveforms, not gate count."""

import math
from dataclasses import replace

import pytest

from taiyidsl import Device, LabConfig, Qubit, reference_lab
from taiyidsl.compiler import CompileError
from taiyidsl.dsl import (
    Experiment,
    FlatTopGaussian,
    Square,
    SweepParameter,
    delay,
    play,
    reserve,
)
from taiyidsl.units import ns, us


def bank_lab(qubit, budget=10000, latency=112):
    lab = reference_lab([qubit])
    name = "urukul0_ch0"
    lab.setup.devices[name] = replace(
        lab.setup.devices[name],
        nco_mode="legacy_tracking",
        ram_latency_mu=latency,
        ram_rearm_duration_mu=budget,
    )
    return lab


def gates(amplitudes, *, phases=None, gap_mu=10112, pulse=None, budget=10000):
    q = Qubit("q0")
    exp = Experiment("resident_gates", [q])
    pulse = pulse or FlatTopGaussian(8 * us, 0.5 * us, sample_interval=80 * ns)
    phases = phases or [0] * len(amplitudes)
    with exp.average(1):
        with exp.realtime():
            for index, (amplitude, phase) in enumerate(zip(amplitudes, phases)):
                if index:
                    delay(q.raman, gap_mu * ns)
                play(q.raman, pulse, amplitude=amplitude, phase=phase)
    return bank_lab(q, budget).compile(exp)


def starts(compiled):
    return [e for e in compiled.points[0].physical_events if e.kind == "dds_ram_start"]


def test_long_xy_sequence_reuses_two_amplitude_waveforms():
    compiled = gates([0.8, 0.8, 0.4, 0.4] * 64, phases=[0, math.pi / 2] * 128)
    events = starts(compiled)
    assert len(events) == 256
    assert [e.params["ram_profile"] for e in events] == [0, 0, 1, 1] * 64
    assert {(e.params["ram_start"], e.params["ram_end"]) for e in events} == {
        (0, 150),
        (151, 301),
    }
    assert events[0].params["ram_asf"] == events[1].params["ram_asf"]
    assert events[0].params["pow"] != events[1].params["pow"]
    assert max(events[0].params["ram_asf"]) == round(0.8 * 0x3FFF)
    assert max(events[2].params["ram_asf"]) == round(0.4 * 0x3FFF)
    rearm = [e for e in compiled.points[0].physical_events if e.kind == "dds_ram_rearm"]
    assert len(rearm) == 255
    assert all(e.duration_mu == 10000 for e in rearm)
    assert all(a.end_mu == b.start_mu for a, b in zip(rearm, events[1:]))
    assert all(e.params["ram_min_gap_mu"] == 10112 for e in events)
    assert all(e.params["ram_matched_latency"] == 1 for e in events)


def test_minimum_gap_is_explicit_and_not_inserted():
    compiled = gates([0.5, 0.5])
    logical = [e for e in compiled.points[0].logical_events if e.kind == "play"]
    assert logical[1].start_mu - logical[0].end_mu == 10112
    with pytest.raises(CompileError, match="at least 10112 mu.*got 10104 mu"):
        gates([0.5, 0.5], gap_mu=10104)
    with pytest.raises(CompileError, match="ram_rearm_duration_mu"):
        gates([0.5, 0.5], budget=None)
    with pytest.raises(CompileError, match="configuration grid"):
        gates([0.5, 0.5], budget=10001)


def test_rearm_budget_cannot_undercut_reference_spi_driver():
    from taiyidsl.artiq import verify_export

    # Rev9 and coarse=8 mu: 3440 mu SPI cursor + 112 mu pipeline, no margin.
    compiled = gates([0.5, 0.5], budget=3440, gap_mu=3552)
    assert verify_export(compiled)["offline_export_check"] == "passed"
    too_short = gates([0.5, 0.5], budget=3432, gap_mu=3544)
    with pytest.raises(ValueError, match="driver SPI cursor minimum"):
        verify_export(too_short)


def test_bank_capacity_counts_unique_scaled_envelopes():
    pulse = FlatTopGaussian(8 * us, 0.5 * us)
    assert len(starts(gates([0.5] * 20, pulse=pulse))) == 20
    with pytest.raises(CompileError, match="Resident RAM bank.*1024"):
        gates([0.5, 0.25], pulse=pulse)


def test_bank_has_at_most_eight_profiles():
    pulse = FlatTopGaussian(0 * ns, 40 * ns)
    compiled = gates([i / 10 for i in range(1, 9)], pulse=pulse)
    assert [e.params["ram_profile"] for e in starts(compiled)] == list(range(8))
    with pytest.raises(CompileError, match="8 profiles"):
        gates([i / 10 for i in range(1, 10)], pulse=pulse)


def test_rearm_respects_submission_reservations():
    q = Qubit("q0")
    exp = Experiment("reserved_rearm", [q])
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse)
            with exp.parallel():
                reserve("kernel_submit", 8 * ns)
                with exp.serial():
                    delay(q.raman, 10112 * ns)
                    play(q.raman, pulse)
    with pytest.raises(CompileError, match="RAM rearm.*reservation"):
        bank_lab(q).compile(exp)


def test_repeated_ram_requires_matched_pipeline_contract():
    q = Qubit("q0")
    exp = Experiment("matched_pipeline", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, FlatTopGaussian(0 * us, 0.5 * us))
            delay(q.raman, 20 * us)
            play(q.raman, FlatTopGaussian(0 * us, 0.5 * us))
    with pytest.raises(CompileError, match="matched-RAM"):
        bank_lab(q, latency=64).compile(exp)


def test_bank_mode_does_not_change_with_number_of_plays():
    single, multiple = gates([0.5]), gates([0.5, 0.5])
    assert starts(single)[0].params["ram_matched_latency"] == 1
    assert starts(single)[0].params["ram_min_gap_mu"] == 10112
    assert {e.params["ram_matched_latency"] for e in starts(multiple)} == {1}
    assert starts(gates([0.5], budget=None))[0].params["ram_matched_latency"] == 0


def test_rearm_does_not_swallow_an_independent_output_edge():
    q = Qubit("q0")
    exp = Experiment("edge_inside_rearm", [q])
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(q.raman, pulse)
                    delay(q.raman, 10112 * ns)
                    play(q.raman, pulse)
                with exp.serial():
                    delay(q.readout, 5 * us)
                    play(q.readout, Square(1 * us, amplitude=1, frequency=0))
    with pytest.raises(CompileError, match="edge overlaps.*RAM rearm"):
        bank_lab(q).compile(exp)


def test_overlapping_rearms_on_independent_boards_are_rejected():
    qubits = [Qubit("q0"), Qubit("q1")]
    exp = Experiment("blocking_rearms", qubits)
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for index, q in enumerate(qubits):
                    with exp.serial():
                        delay(q.raman, index * 8 * ns)
                        play(q.raman, pulse)
                        delay(q.raman, 20 * us)
                        play(q.raman, pulse)
    lab = reference_lab(qubits)
    for name, device in tuple(lab.setup.devices.items()):
        if device.kind == "dds":
            lab.setup.devices[name] = replace(
                device,
                nco_mode="legacy_tracking",
                ram_latency_mu=112,
                ram_rearm_duration_mu=10000,
            )
    with pytest.raises(CompileError, match="Resource conflict on kernel_submit"):
        lab.compile(exp)


def test_repeated_banks_reloaded_per_point_and_square_state_restored():
    from taiyidsl.artiq import verify_equivalence

    q = Qubit("q0")
    exp = Experiment("bank_scan_and_square", [q])
    mode = SweepParameter("mode", [0, 1, 2], "")
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(2):
        with exp.sweep(mode):
            with exp.realtime():
                for branch, amplitude in ((0, 0.8), (1, 0.3)):
                    with exp.case(mode, branch):
                        play(q.raman, pulse, amplitude=amplitude, phase=0.1)
                        delay(q.raman, 10112 * ns)
                        play(q.raman, pulse, amplitude=amplitude / 2, phase=1.2)
                with exp.case(mode, 2):
                    play(q.raman, Square(4 * us, amplitude=0.5))
    compiled = bank_lab(q).compile(exp)
    result = verify_equivalence(compiled)
    assert result["status"] == "passed"


def test_replay_phase_tracking_accounts_for_preroll_and_device_latency():
    from taiyidsl.artiq import verify_export

    q = Qubit("q0")
    exp = Experiment("tracking_after_preroll", [q])
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse, frequency=83e6, phase=0.1)
            delay(q.raman, 10112 * ns)
            play(q.raman, pulse, frequency=87e6, phase=0.7)
    lab = bank_lab(q)
    lab.setup.devices["urukul0_ch0"] = replace(
        lab.setup.devices["urukul0_ch0"],
        config_grid_mu=16,
        config_duration_mu=1008,
        latency_mu=32,
    )
    compiled = lab.compile(exp)
    events = starts(compiled)
    assert [e.params["ram_profile"] for e in events] == [0, 0]
    assert compiled.points[0].origin_mu == 144
    for event in events:
        launch_from_reference = event.start_mu - event.params["ref_time_mu"]
        expected = (
            event.params["pow"] + ((launch_from_reference * event.params["ftw"]) >> 16)
        ) & 0xFFFF
        assert event.params["ram_pow"] == expected
        assert event.start_mu % 16 == 0
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_shared_bus_square_configuration_avoids_ram_rearm_and_launch():
    from taiyidsl.artiq import verify_export

    q0, q1 = Qubit("q0"), Qubit("q1")
    exp = Experiment("shared_bus_rearm", [q0, q1])
    pulse = FlatTopGaussian(0 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                with exp.serial():
                    play(q0.raman, pulse)
                    delay(q0.raman, 10112 * ns)
                    play(q0.raman, pulse)
                with exp.serial():
                    delay(q1.raman, 14008 * ns)
                    play(q1.raman, Square(4 * us))
    lab = reference_lab([q0, q1])
    ram = lab.setup.devices["urukul0_ch0"]
    lab.setup.devices[ram.name] = replace(
        ram,
        nco_mode="legacy_tracking",
        ram_latency_mu=112,
        ram_rearm_duration_mu=10000,
    )
    lab.setup.devices["urukul1_ch0"] = replace(
        lab.setup.devices["urukul1_ch0"],
        nco_mode="legacy_tracking",
        bus=ram.bus,
        square_latency_mu=None,
    )
    compiled = lab.compile(exp)
    events = compiled.points[0].physical_events
    configuration = next(e for e in events if e.kind == "dds_config")
    rearm = next(e for e in events if e.kind == "dds_ram_rearm")
    assert configuration.end_mu == rearm.start_mu
    assert verify_export(compiled)["offline_export_check"] == "passed"


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "10000"])
def test_invalid_rearm_budget(value):
    with pytest.raises(ValueError, match="ram_rearm_duration_mu"):
        Device("dds0", "dds", ram_rearm_duration_mu=value)


@pytest.mark.parametrize("value", [None, 10000])
def test_rearm_budget_config_roundtrip(value):
    lab = bank_lab(Qubit("q0"), value)
    loaded = LabConfig.from_dict(lab.to_dict())
    assert loaded.setup.devices["urukul0_ch0"].ram_rearm_duration_mu == value
