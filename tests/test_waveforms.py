import math
from dataclasses import replace

import numpy as np
import pytest

from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_equivalence, verify_export
from taiyidsl.compiler import CompileError, compile_experiment
from taiyidsl.config.models import Qubit
from taiyidsl.dsl import (
    Experiment,
    FlatTopGaussian,
    Square,
    SweepParameter,
    delay,
    play,
    reserve,
    shift_phase,
)
from taiyidsl.lab import reference_setup as original_reference_setup
from taiyidsl.units import ns, us


def reference_setup(qubits):
    setup, mapping = original_reference_setup(qubits)
    setup.devices = {
        name: replace(d, nco_mode="legacy_tracking", ram_latency_mu=64)
        if d.kind == "dds"
        else d
        for name, d in setup.devices.items()
    }
    return setup, mapping


def compile_pulse(pulse, **overrides):
    q = Qubit("q0")
    exp = Experiment("pulse", [q])
    with exp.average(2):
        with exp.realtime():
            play(q.raman, pulse, **overrides)
    return compile_experiment(exp, *reference_setup([q]))


def test_play_overrides_do_not_modify_template():
    pulse = Square(10 * us, amplitude=0.8, phase=0.4)
    compiled = compile_pulse(pulse, amplitude=0, phase=0, duration=5 * us)
    event = compiled.points[0].logical_events[0]
    assert event.params["amplitude"] == 0
    assert event.params["phase"] == 0
    assert event.duration_mu == 5000
    assert pulse.amplitude == 0.8
    assert float(pulse.duration) == 10e-6


def test_flat_duration_scan_changes_plateau_not_edges_and_exports():
    q = Qubit("q0")
    flat = SweepParameter("flat", [0, 4, 8], "us")
    pulse = FlatTopGaussian(flat, 0.5 * us, amplitude=0.8)
    exp = Experiment("flat_scan", [q])
    with exp.average(2):
        with exp.sweep(flat):
            with exp.realtime():
                play(q.raman, pulse, amplitude=0.5, phase=math.pi / 2)
    compiled = compile_experiment(exp, *reference_setup([q]))
    programs = [
        next(e for e in p.physical_events if e.kind == "dds_ram_start")
        for p in compiled.points
    ]
    assert [p.duration_mu for p in compiled.points] == [4000, 8000, 12000]
    arrays = [e.params["ram_asf"] for e in programs]
    assert [len(a) for a in arrays] == [201, 401, 601]
    assert all(a[0] == a[-1] == 0 for a in arrays)
    assert all(max(a) == round(0.5 * 0x3FFF) for a in arrays)
    np.testing.assert_array_equal(arrays[0][:101], arrays[2][:101])
    assert verify_export(compiled)["offline_export_check"] == "passed"
    assert verify_equivalence(compiled)["status"] == "passed"
    source = generate_source(compiled)
    assert "write_ram" in source
    assert "ram_enable=1" in source
    assert "phase_autoclear=1" in source


def test_ram_capacity_and_grid_are_not_silently_changed():
    with pytest.raises(CompileError, match="1024"):
        compile_pulse(FlatTopGaussian(30 * us, 0.5 * us))
    with pytest.raises(CompileError, match="sample interval"):
        compile_pulse(FlatTopGaussian(8 * us, 0.5 * us, sample_interval=21 * ns))


def test_invalid_flat_top_parameters():
    for kwargs in (
        {"sigma": 0},
        {"sigma": -1},
        {"cutoff_sigma": 0},
        {"sample_interval": 0},
        {"amplitude": 1.1},
    ):
        params = dict(flat_duration=8 * us, sigma=0.5 * us)
        params.update(kwargs)
        with pytest.raises((TypeError, ValueError)):
            FlatTopGaussian(**params)
    with pytest.raises(TypeError, match="flat_duration"):
        compile_pulse(FlatTopGaussian(8 * us, 0.5 * us), duration=10 * us)


def test_reusing_square_after_overrides_restores_defaults():
    q = Qubit("q0")
    pulse = Square(5 * us, amplitude=0.8)
    exp = Experiment("reuse", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, pulse, amplitude=0.2)
            delay(q.raman, 2 * us)
            play(q.raman, pulse)
    compiled = compile_experiment(exp, *reference_setup([q]))
    assert [
        e.params["amplitude"]
        for e in compiled.points[0].logical_events
        if e.kind == "play"
    ] == [0.2, 0.8]


def test_square_after_gaussian_scan_point_restores_hardware_mode():
    q = Qubit("q0")
    kind = SweepParameter("kind", [0, 1], "")
    exp = Experiment("mixed_modes", [q])
    with exp.average(2):
        with exp.sweep(kind):
            with exp.realtime():
                with exp.case(kind, 0):
                    play(q.raman, FlatTopGaussian(0 * us, 0.5 * us))
                with exp.case(kind, 1):
                    play(q.raman, Square(4 * us))
    compiled = compile_experiment(exp, *reference_setup([q]))
    source = generate_source(compiled)
    assert ".chip_select - 4, 7)" in source
    assert verify_export(compiled)["offline_export_check"] == "passed"
    with pytest.raises(ValueError, match="profile seven"):
        verify_export(
            compiled,
            source=source.replace(".chip_select - 4, 7)", ".chip_select - 4, 0)"),
        )


def test_ram_mock_detects_wrong_amplitude_and_phase_programming():
    compiled = compile_pulse(FlatTopGaussian(8 * us, 0.5 * us), phase=1)
    source = generate_source(compiled)
    broken = source.replace(".set_pow(", ".set_ftw(")
    with pytest.raises(ValueError, match="differ from compiled"):
        verify_export(compiled, source=broken)
    broken = source.replace("ram_enable=1", "ram_enable=0")
    with pytest.raises(ValueError, match="enable ASF"):
        verify_export(compiled, source=broken)


def test_ram_hardware_frame_is_rejected_until_implemented():
    q = Qubit("q0")
    exp = Experiment("ram_phase", [q])
    pulse = FlatTopGaussian(8 * us, 0.5 * us)
    with exp.average(1):
        with exp.realtime():
            delay(q.raman, 2 * us)
            shift_phase(q.frame, 0.3)
            play(q.raman, pulse, phase=0.2, flat_duration=4 * us)
    setup, mapping = reference_setup([q])
    # Isolate the legacy RAM phase contract from shared-update-group rejection.
    del mapping[q.rf]
    with pytest.raises(CompileError, match="RAM frame updates are unsupported"):
        compile_experiment(exp, setup, mapping)
    assert float(pulse.flat_duration) == 8e-6
    # A constant per-pulse RAM phase remains supported.
    compiled = compile_pulse(pulse, phase=0.5, flat_duration=4 * us)
    assert compiled.points[0].duration_mu == 8000
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_repeated_ram_and_shared_update_group_are_explicitly_rejected():
    q = Qubit("q0")
    for second_signal in (q.raman, q.rf):
        exp = Experiment("unsupported_ram_composition", [q])
        with exp.average(1):
            with exp.realtime():
                play(q.raman, FlatTopGaussian(8 * us, 0.5 * us))
                delay(q.raman, 2 * us)
                play(second_signal, Square(4 * us))
        with pytest.raises(CompileError, match="exclusive active IO_UPDATE"):
            compile_experiment(exp, *reference_setup([q]))


def test_ram_on_ttl_is_rejected():
    q = Qubit("q0")
    exp = Experiment("ttl_shape", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.readout, FlatTopGaussian(8 * us, 0.5 * us))
    with pytest.raises(CompileError, match="not TTL"):
        compile_experiment(exp, *reference_setup([q]))


def test_ram_launch_respects_explicit_submission_reservation():
    q = Qubit("q0")
    exp = Experiment("reserved_ram", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                reserve("kernel_submit", 8 * ns)
                with exp.serial():
                    delay(q.raman, 64 * ns)
                    play(q.raman, FlatTopGaussian(8 * us, 0.5 * us))
    with pytest.raises(CompileError, match="reservation"):
        compile_experiment(exp, *reference_setup([q]))


def test_ram_target_checks_reject_unmodelled_update_timing():
    from taiyidsl.backends.artiq._contracts import ExportError

    compiled = compile_pulse(FlatTopGaussian(8 * us, 0.5 * us))
    source = generate_source(compiled)
    for broken in (
        source.replace(
            "self.result_adapter = None",
            "self.result_adapter = None\n"
            "        self.device_0.sync_data.io_update_delay = 1",
        ),
        source.replace(
            "self.result_adapter = None",
            "self.result_adapter = None\n        self.device_0.io_update = None",
        ),
        source.replace(
            "self.result_adapter = None",
            "self.result_adapter = None\n        self.device_0.chip_select = 3",
        ),
    ):
        with pytest.raises((ValueError, ExportError)):
            verify_export(compiled, source=broken)


def test_ram_requires_explicit_pipeline_contract_and_gates_complete_envelope():
    q = Qubit("q0")
    exp = Experiment("latency", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, FlatTopGaussian(8 * us, 0.5 * us))
    legacy_setup, mapping = original_reference_setup([q])
    legacy_setup.devices = {
        name: replace(device, nco_mode="legacy_tracking")
        for name, device in legacy_setup.devices.items()
    }
    with pytest.raises(CompileError, match="ram_latency_mu"):
        compile_experiment(exp, legacy_setup, mapping)
    compiled = compile_experiment(exp, *reference_setup([q]))
    point = compiled.points[0]
    events = {e.kind: e for e in point.physical_events}
    assert events["dds_ram_start"].start_mu == 0
    assert events["dds_on"].start_mu == 64
    assert events["dds_off"].start_mu == 12064
    source = generate_source(compiled)
    with pytest.raises(ValueError, match="pipeline latency|certified submission plan"):
        verify_export(
            compiled, source=source.replace("at_mu(t0 + 64)", "at_mu(t0 + 56)")
        )
