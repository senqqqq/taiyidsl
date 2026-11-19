"""Generated continuous-NCO code preserves the epoch and real phase commits."""

import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest

from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_equivalence, verify_export

ROOT = Path(__file__).resolve().parents[1]
build = runpy.run_path(str(ROOT / "examples" / "short_gate_timing.py"))[
    "build_sequence"
]


@pytest.mark.parametrize("shape", ["square", "ram"])
@pytest.mark.parametrize("duration", [800, 200])
def test_arbitrary_frequency_same_axis_preserves_continuous_epoch(shape, duration):
    compiled = build(shape, duration, channels=2, pattern="repeat")
    assert verify_equivalence(compiled)["status"] == "passed"
    source = generate_source(compiled)
    assert "phase_clear=1, phase_autoclear=0" in source
    assert "phase_clear=0, phase_autoclear=0" in source
    assert "phase_autoclear=1" not in source
    assert (
        len(
            [e for e in compiled.points[0].physical_events if e.kind == "dds_nco_epoch"]
        )
        == 2
    )


@pytest.mark.parametrize(
    "mutation", ["autoclear", "held_clear", "initial_pow", "bus_alias"]
)
def test_continuous_source_corruption_is_detected(mutation):
    compiled = build("ram", 800, channels=2, counterfactual_equal_latency=True)
    source = generate_source(compiled)
    if mutation == "autoclear":
        source = source.replace("phase_autoclear=0", "phase_autoclear=1")
    elif mutation == "held_clear":
        source = source.replace(
            "phase_clear=0, phase_autoclear=0", "phase_clear=1, phase_autoclear=0"
        )
    elif mutation == "initial_pow":
        first = next(
            e for e in compiled.points[0].physical_events if e.kind == "dds_ram_start"
        )
        old = f"set_pow({first.params['nco_initial_pow']})"
        assert old in source
        source = source.replace(
            old, f"set_pow({(first.params['nco_initial_pow'] + 1) & 65535})", 1
        )
    else:
        source = source.replace(
            "        self.result_adapter = None",
            "        self.result_adapter = None\n"
            "        self.device_1.bus.channel = self.device_0.bus.channel",
        )
    with pytest.raises(ValueError):
        verify_export(compiled, source=source)


@pytest.mark.skipif(
    os.name != "nt"
    or not (ROOT / ".toolchains/artiq9/clang64/bin/python.exe").exists(),
    reason="Installed ARTIQ toolchain unavailable",
)
@pytest.mark.parametrize("shape", ["square", "ram"])
@pytest.mark.parametrize("mode", ["auto", "unrolled"])
def test_real_driver_continuous_epoch_and_parallel_preparation(tmp_path, shape, mode):
    compiled = build(
        shape,
        800,
        channels=2,
        pattern="xy" if shape == "ram" else "repeat",
        counterfactual_equal_latency=shape == "ram",
    )
    export = tmp_path / mode
    compiled.export_artiq(export, mode=mode)
    script = "verify_ram_driver.py" if shape == "ram" else "verify_square_driver.py"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts/artiq-python.ps1"),
            str(ROOT / "scripts" / script),
            str(export),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    report_name = (
        "real_ram_driver_trace.json" if shape == "ram" else "real_driver_trace.json"
    )
    report = json.loads((export / report_name).read_text(encoding="utf-8"))
    assert result.returncode == 0, (result.stdout, result.stderr, report.get("error"))
    assert report["points"][0]["continuous_epochs"] == 2
    assert report["hardware_executed"] is False


@pytest.mark.skipif(
    os.name != "nt"
    or not (ROOT / ".toolchains/artiq9/clang64/bin/python.exe").exists(),
    reason="Installed ARTIQ toolchain unavailable",
)
@pytest.mark.parametrize("corrupt", [None, "square", "ram"])
def test_real_driver_mixed_200ns_square_and_800ns_ram_checks_both_paths(
    tmp_path, corrupt
):
    mixed = runpy.run_path(str(ROOT / "examples" / "continuous_nco.py"))[
        "build_mixed_short"
    ]()
    export = tmp_path / "mixed"
    mixed.export_artiq(export)
    if corrupt:
        path = export / "compiled.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        kind = "dds_prepare" if corrupt == "square" else "dds_ram_start"
        event = next(
            e for e in plan["points"][0]["physical_events"] if e["kind"] == kind
        )
        key = "asf" if corrupt == "square" else "ram_pow"
        event["params"][key] += 1
        path.write_text(json.dumps(plan), encoding="utf-8")
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
    if corrupt:
        assert result.returncode != 0
        assert "differs" in report["error"]["message"]
    else:
        assert result.returncode == 0, (
            result.stdout,
            result.stderr,
            report.get("error"),
        )
        assert report["points"][0]["square"]["profile_writes"] == 1
        assert report["points"][0]["phase_preparations"] == 1
        assert report["points"][0]["continuous_epochs"] == 2
