"""uv host to a real, installed Nix worker, with instrument endpoints replaced."""

import os
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from taiyidsl import DeviceSetup, Experiment, LabConfig, Qubit, Square, play, us
from taiyidsl.runtime import worker_env
from taiyidsl.spectrum_identity import hardware_fingerprint


@pytest.mark.skipif(
    not os.getenv("TAIYIDSL_TEST_NIX_PYTHON"), reason="requires the built Nix worker"
)
def test_real_sdk_import_from_jupyter_environment(tmp_path, monkeypatch):
    """Use the session launch environment and real Python SDK; no vendor calls."""
    monkeypatch.setenv("MPLBACKEND", "module://matplotlib_inline.backend_inline")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "mpl"))
    cmd, env = worker_env.launch(
        os.environ["TAIYIDSL_TEST_NIX_PYTHON"], spectrum=True, mode="serve"
    )
    # Exercise the same imports as the connect handler without sending a connect
    # request. Interpose before importing even spcm_core: it queries the driver
    # version at import time. No native Spectrum library or device is needed.
    source = dedent("""\
        import ctypes
        from unittest.mock import Mock, patch

        native = Mock()
        load_library = ctypes.cdll.LoadLibrary
        def load(name):
            return native if name == "libspcm_linux.so" else load_library(name)
        with patch.object(ctypes.cdll, "LoadLibrary", load):
            from spcm import Card
            from spcm_core import SPC_GETDRVVERSION, spcm_hOpen
        from matplotlib import pyplot

        assert spcm_hOpen is native.spcm_hOpen and Card.__name__ == "Card"
        for name, args, _ in native.mock_calls:
            assert name == "spcm_dwGetParam_i64"
            assert args[:2] == (None, SPC_GETDRVVERSION)
        assert pyplot.get_backend().lower() == "agg"
        """)
    result = subprocess.run(
        [cmd[0], "-P", "-c", source],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def build(launcher, calibrated, *, bench=False, phase_reference="sequence"):
    db = {
        "core": {
            "class": "Core",
            "arguments": {"host": "unit-test.invalid", "ref_period": 1e-9},
        },
        "ttl20": {"class": "TTLOut", "arguments": {"channel": 20}},
    }
    spec = {
        "A": {
            "serial_number": "101",
            "active_channels": [0],
            "trigger_latency_samples": 64,
        }
    }
    group = {"cards": ["A"], "trigger": "ttl20"}
    if bench:
        spec["A"]["clock_policy"] = "internal_pll_bench"
    descriptor = {
        "backend": "artiq_spectrum",
        "instruments": {
            "core": {"kind": "artiq", "device": "core"},
            "A": {"kind": "spectrum", "device": "A"},
        },
        "channels": {"out": {"instrument": "A", "channel": 0}},
        "signals": {"q0": {"clock": "out"}},
        "sync_groups": {"g": group},
    }
    q = Qubit("q0", signals={"clock": "q0.clock"})
    pulse = Square(duration=4 * us, amplitude=0.2, frequency=1e6)
    exp = Experiment("fake_devices_only", [q])
    with (
        exp.average(1),
        exp.realtime(),
        exp.triggered_sequence(group="g", phase_reference=phase_reference),
    ):
        play(q.clock, pulse)

    def compile_point():
        setup = DeviceSetup.from_descriptor(descriptor, device_db=db, spec_db=spec)
        lab = LabConfig(setup, qubits=[q], emulation=False, artiq_python=str(launcher))
        return lab, lab.compile(exp)

    lab, compiled = compile_point()
    if calibrated:
        card = next(
            e.params["cards"]["A"]
            for e in compiled.points[0].physical_events
            if e.kind == "spectrum_program"
        )
        # Synthetic calibration strictly for firmware/identity of the SDK double.
        group["calibration"] = {
            "status": "measured",
            "revision": "SDK-DOUBLE-ONLY",
            "card_fingerprints": {
                "A": hardware_fingerprint(
                    card["contract"], {"firmware": "00000001/00000002"}
                )
            },
            "clock_session_ids": {"A": "SIM"},
            "channels": {
                "A:0": {
                    "sample_rate_hz": 1.25e9,
                    "sample_shift_samples": 0,
                    "delay_ns": 0.0,
                    "gain": 1.0,
                    "phase_offset_rad": 0.0,
                    "phase_reference": "after_sample_shift",
                    "carrier_frequency_hz": 1e6,
                    "phase_uncertainty_deg": 0.0,
                    "timing_uncertainty_ns": 0.0,
                }
            },
        }
        lab, compiled = compile_point()
    return lab, compiled


@pytest.mark.skipif(
    not os.getenv("TAIYIDSL_TEST_NIX_PYTHON"), reason="requires the built Nix worker"
)
@pytest.mark.parametrize(
    "case,error",
    [
        ("success", None),
        ("uncalibrated", "measured calibration"),
        ("partial_upload", "injected DMA"),
        ("not_ready", "ready"),
        ("disconnect", "state is unknown"),
        ("bench", None),
        ("bench_partial_upload", "injected DMA"),
        ("bench_drift", "read back"),
        ("bench_wrong_source", "read back"),
    ],
)
def test_nix_session(tmp_path, monkeypatch, case, error):
    worker = Path(os.environ["TAIYIDSL_TEST_NIX_PYTHON"]).absolute()
    double = Path(__file__).with_name("isolated_worker_double.py").resolve()
    launcher = tmp_path / "double"
    launcher.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(str(worker))} -P {shlex.quote(str(double))} "$@"\n'
    )
    launcher.chmod(0o755)
    events = tmp_path / "events"
    monkeypatch.setenv("TAIYIDSL_TEST_CASE", case)
    monkeypatch.setenv("TAIYIDSL_TEST_EVENTS", str(events))
    monkeypatch.chdir(tmp_path)
    bench = case.startswith("bench")
    lab, compiled = build(launcher, not bench and case != "uncalibrated", bench=bench)
    if case == "bench_wrong_source":
        with pytest.raises(RuntimeError, match=error):
            lab.connect()
        assert not {
            "initialize_devices",
            "dma",
            "start",
            "clock_write",
            "point_0",
        }.intersection(events.read_text().splitlines())
        return
    with (
        lab.connect() if bench else lab.connect(clock_session_ids={"A": "SIM"})
    ) as session:
        if error is None:
            for _ in range(2):
                receipt = session.run(compiled)
                assert receipt["completed_shots"] == [{"point": 0, "shot": 0}]
                assert receipt["physical_acceptance"] == "not_verified"
                if bench:
                    card = receipt["spectrum_receipts"][0]["receipt"]["cards"]["A"]
                    assert card["clock_mode"] == "internal_pll"
                    assert card["reference_clock_hz"] is None
                    assert card["shared_reference"] is False
                    assert card["output_basis"] == "setpoints_only"
                    assert card["analog_delay_gain_absolute_phase"] == "not_verified"
        else:
            with pytest.raises(RuntimeError, match=error):
                session.run(compiled)
            with pytest.raises(RuntimeError, match="not connected"):
                session.run(compiled)
    log = events.read_text().splitlines()
    assert (session.directory / "worker_environment.json").is_file()
    assert (
        "SDK/native output belongs in worker.log"
        in (session.directory / "worker.log").read_text()
    )
    assert log.count("point_0") == {"success": 2, "bench": 2, "disconnect": 1}.get(
        case, 0
    )
    assert "clock_write" not in log
    if case != "disconnect":
        assert log[-1] == "closed core"
    if case == "uncalibrated":
        assert not {"initialize_devices", "dma", "start"}.intersection(log)
    if case in {"partial_upload", "bench_partial_upload"}:
        assert "dma" in log and "start" not in log
    if case == "bench_drift":
        assert "initialize_devices" in log and not {"dma", "start"}.intersection(log)
