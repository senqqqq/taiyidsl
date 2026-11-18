"""Offline initialization example regressions and executable entry-point smoke test."""

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from taiyidsl import (
    Device,
    DeviceSetup,
    Protocol,
    ProtocolStep,
    Qubit,
    SignalMap,
    compile_experiment,
    us,
)
from taiyidsl.artiq import verify_equivalence

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "state_initialization.py"


@pytest.fixture
def initialization_scan():
    build = runpy.run_path(str(EXAMPLE))["build_initialization_scan"]
    config = {
        "schema_version": 2,
        "pump": {
            "signal": "optics.pump_556",
            "frequency_hz": 80_000_000,
            "amplitude": 0.1,
            "duration_s": 0,
            "setup_wait_s": 20e-6,
            "settle_s": 20e-6,
        },
        "pushout": {
            "signal": "optics.pushout_556",
            "frequency_hz": 90_000_000,
            "amplitude": 0.2,
            "duration_s": 100e-6,
            "setup_wait_s": 0,
            "settle_s": 0,
        },
        "camera": {"transfer_wait_s": 1000e-6},
    }
    setup = DeviceSetup(
        devices={
            "urukul0_ch0": Device(
                "urukul0_ch0", "dds", bus="spi0", update_group="urukul0"
            ),
            "urukul0_ch1": Device(
                "urukul0_ch1", "dds", bus="spi0", update_group="urukul0"
            ),
            "ttl0": Device("ttl0", "ttl", grid_mu=1, min_duration_mu=1),
        },
        protocols={
            "image": Protocol((ProtocolStep("{qubit}.readout", 10 * us),)),
        },
    )
    mapping = SignalMap(
        {
            "optics.pump_556": "urukul0_ch0",
            "optics.pushout_556": "urukul0_ch1",
            "q0.readout": "ttl0",
        }
    )
    exp = build(Qubit("q0"), config, durations_us=(0, 20, 60))
    return exp, config, setup, mapping


def test_initialization_reference_and_pushout_preserve_readout_timing(
    initialization_scan,
):
    exp, _, setup, mapping = initialization_scan
    compiled = compile_experiment(exp, setup, mapping)
    assert len(compiled.points) == 6

    for reference, pushout in zip(compiled.points[::2], compiled.points[1::2]):
        assert reference.coordinates["readout_mode"] == 0
        assert pushout.coordinates["readout_mode"] == 1
        assert reference.coordinates["pump_time"] == pushout.coordinates["pump_time"]
        assert reference.duration_mu == pushout.duration_mu
        readout_edges = []
        for point in (reference, pushout):
            readout_edges.append(
                [
                    (event.kind, event.start_mu, event.duration_mu)
                    for event in point.physical_events
                    if event.signal == "ttl0"
                ]
            )
            pump = [
                event
                for event in point.physical_events
                if event.signal == "urukul0_ch0"
            ]
            if point.coordinates["pump_time"] == 0:
                assert pump == []
            else:
                assert any(event.kind == "dds_on" for event in pump)
        assert readout_edges[0] == readout_edges[1]
        assert not any(
            event.signal == "urukul0_ch1" for event in reference.physical_events
        )
        assert any(
            event.signal == "urukul0_ch1" and event.kind == "dds_on"
            for event in pushout.physical_events
        )

    report = verify_equivalence(compiled)
    assert report["status"] == "passed"
    assert report["auto"]["kernel_count"] == 1  # This fixture uses TTL-only outputs.
    assert report["host_order"]["shots_checked"] == 600


def test_initialization_uses_config_copied_at_build_time(initialization_scan):
    exp, config, setup, mapping = initialization_scan
    config["pump"]["frequency_hz"] = 100_000_000
    config["pump"]["amplitude"] = 0.9
    compiled = compile_experiment(exp, setup, mapping)
    assert compiled.parameters["pump"]["frequency_hz"] == 80_000_000
    pulses = [
        event
        for point in compiled.points
        for event in point.logical_events
        if event.kind == "play" and event.signal == "optics.pump_556"
    ]
    assert pulses
    assert all(event.params["frequency"] == 80_000_000 for event in pulses)
    assert all(event.params["amplitude"] == 0.1 for event in pulses)


def test_initialization_entrypoint_generates_verified_offline_artifacts(tmp_path):
    # Its default output is relative to __file__; copy the example and profiles so the
    # subprocess cannot overwrite a user's existing example output directory.
    script = tmp_path / "examples" / EXAMPLE.name
    script.parent.mkdir()
    shutil.copy2(EXAMPLE, script)
    shutil.copytree(EXAMPLE.parent / "config", script.parent / "config")
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = tmp_path / "output" / "state_initialization"
    verifier = script.parent / "verify_optics.py"
    shutil.copy2(EXAMPLE.parent / verifier.name, verifier)
    result = subprocess.run(
        [sys.executable, str(verifier), "--experiment", "initialization"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "verification.json").read_text(encoding="utf-8"))
    compiled = json.loads((output / "compiled.json").read_text(encoding="utf-8"))
    assert len(compiled["points"]) == 12
    assert report["status"] == "passed"
    assert report["auto"]["kernel_count"] == len(compiled["points"])
    assert report["host_order"]["shots_checked"] == 1200
    assert report["artiq_compile"] == report["hardware"] == "not_verified"
    assert (output / "timeline.html").stat().st_size > 0
    assert (output / "artiq" / "experiment.py").is_file()
    manifest = json.loads(
        (output / "artiq" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["verification"]["offline_export_check"] == "passed"
