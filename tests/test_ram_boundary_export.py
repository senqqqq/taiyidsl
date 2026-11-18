"""RAM boundary export checks committed state, real drivers and both code modes."""

import ast
import json
import math
import os
import runpy
import subprocess
from pathlib import Path

import pytest

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    SignalMap,
    ns,
    play,
    reset_frame,
    shift_phase,
    us,
)
from taiyidsl.artiq import generate_source, verify_equivalence, verify_export

ROOT = Path(__file__).resolve().parents[1]
example = runpy.run_path(str(ROOT / "examples" / "ram_boundary_sequence.py"))


def multi_sequence(channels=2, *, terminal_frame=True):
    qubits = [Qubit(f"q{i}") for i in range(channels)]
    devices = {
        f"dds{i}": Device(
            f"dds{i}",
            "dds",
            bus=f"spi{i}",
            update_group=f"board{i}",
            ram_latency_mu=112,
            ram_phase_latency_mu=80,
            ram_rearm_duration_mu=4000,
            ram_phase_update_duration_mu=480,
        )
        for i in range(channels)
    }
    lab = LabConfig(
        DeviceSetup(devices),
        SignalMap({q.raman: f"dds{i}" for i, q in enumerate(qubits)}),
    )
    exp = Experiment("ram_parallel_boundary_export", qubits)
    pulse = FlatTopGaussian(
        3 * us,
        sigma=0.5 * us,
        cutoff_sigma=5,
        sample_interval=20 * ns,
        amplitude=0.5,
        frequency=83e6,
    )
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qubits:
                    with exp.serial():
                        play(q.raman, pulse)
                        shift_phase(q.frame, math.pi / 2)
                        play(q.raman, pulse)
                        if terminal_frame:
                            reset_frame(q.frame)
    return lab.compile(exp)


@pytest.mark.parametrize("mode", ["auto", "unrolled"])
def test_independent_boards_and_terminal_frame_execute_in_both_modes(mode):
    compiled = multi_sequence()
    assert verify_export(compiled, mode=mode)["offline_export_check"] == "passed"
    source = generate_source(compiled, mode=mode)
    assert ".io_update.on()" in source
    assert ".io_update.off()" in source
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("mode", ["auto", "unrolled"])
@pytest.mark.parametrize(
    "mutation", ["missing_pow", "missing_rise", "missing_fall", "wrong_mode"]
)
def test_boundary_export_mutations_are_detected(mode, mutation):
    # An explicitly synthetic equal-latency contract exercises live POW writes;
    # the public example now defaults to a same-axis continuous-NCO train.
    compiled = example["build_sequence"](
        count=4, phase_pattern="xy", counterfactual_equal_latency=True
    )
    source = generate_source(compiled, mode=mode)
    if mutation == "missing_pow":
        if mode == "auto":
            old = "self.device_0.set_pow(int32(events[event_index, 5]))"
        else:
            phase = next(
                e
                for e in compiled.points[0].physical_events
                if e.kind == "dds_ram_phase"
            )
            old = f"self.device_0.set_pow({phase.params['ram_pow']})"
        new = "pass"
    elif mutation == "missing_rise":
        old, new = "self.device_0.io_update.on()", "pass"
    elif mutation == "missing_fall":
        old, new = "self.device_0.io_update.off()", "pass"
    else:
        old, new = "phase_autoclear=0", "phase_autoclear=1"
    assert old in source
    with pytest.raises(ValueError):
        verify_export(compiled, source=source.replace(old, new))


def test_long_table_reuses_one_envelope_and_has_constant_kernel_size():
    short = example["build_sequence"](count=4)
    long = example["build_sequence"](count=1024)

    def kernel_size(source):
        tree = ast.parse(source)
        kernel = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "point_0"
        )
        return len(list(ast.walk(kernel)))

    short_source, long_source = generate_source(short), generate_source(long)
    assert kernel_size(short_source) == kernel_size(long_source)
    assert long_source.count(".write_ram(") == 1
    assert verify_export(long)["offline_export_check"] == "passed"


def test_pow_buffer_does_not_change_committed_carrier_before_update():
    from taiyidsl.artiq import _DriverClock, _MockDDS

    compiled = multi_sequence(channels=1)
    device = compiled.setup.devices["dds0"]
    clock = _DriverClock()
    clock.awaiting_origin = False
    dds = _MockDDS(clock, "dds0", device)
    dds.ram_enabled = dds.committed_ram_enabled = dds.phase_autoclear = 1
    dds.matched_latency_enable = 1
    dds.ram_destination = 2
    dds.committed_zero = True
    dds.selected_profile = 0
    dds.ram_profiles[0] = (0, 2, 1)
    dds.ram_memory[:3] = [0, 123 << 18, 0]
    dds.sw.is_on = True
    dds.committed_pow = 42
    dds.set_pow(1234)
    assert dds.committed_pow == 42
    assert clock.cursor == 416
    assert dds.ram_pow_pending
    dds.io_update.on()
    assert dds.committed_pow == 1234
    assert not dds.ram_pow_pending
    assert clock.ram_effects[0]["carrier_effect_mu"] == 416 + 80
    assert clock.ram_effects[0]["start_mu"] == 416 + 112
    clock.cursor += 8
    dds.io_update.off()
    with pytest.raises(ValueError, match="coarse-cycle low"):
        dds.io_update.on()
    clock.cursor += 8
    dds.io_update.on()


@pytest.mark.skipif(
    os.name != "nt"
    or not (ROOT / ".toolchains/artiq9/clang64/bin/python.exe").exists(),
    reason="Optional installed ARTIQ 9 toolchain is unavailable",
)
@pytest.mark.parametrize("mode", ["auto", "unrolled"])
@pytest.mark.parametrize("channels", [1, 2])
def test_actual_ram_driver_traces_include_preload_phase_and_simultaneous_edges(
    tmp_path, mode, channels
):
    export = tmp_path / mode
    multi_sequence(channels).export_artiq(export, mode=mode)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts/artiq-python.ps1"),
            str(ROOT / "scripts/verify_ram_driver.py"),
            str(export),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    report = json.loads(
        (export / "real_ram_driver_trace.json").read_text(encoding="utf-8")
    )
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    assert report["status"] == "passed"
    assert report["points"][0]["maximum_phase_duration_mu"] == 416
    assert report["points"][0]["launches"] == channels * 3
    assert report["points"][0]["resident_profiles"] == channels
    assert not report["hardware_executed"]
