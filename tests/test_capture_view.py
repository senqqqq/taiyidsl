"""Capture alignment must not fit away the physical error being inspected."""

import copy
from html.parser import HTMLParser

import numpy as np
import pytest
from test_spectrum_phase_modes import phase_lab, sequence

from taiyidsl import Experiment, Qubit, Square, SweepParameter, ns, play
from taiyidsl.capture_view import compare_capture, write_capture_comparison
from taiyidsl.output_simulator import simulate_output


def capture(tmp_path):
    lab = phase_lab()
    exp = sequence(
        lambda q: play(q.rf, Square(800 * ns, amplitude=0.5, frequency=11e6))
    )
    compiled = lab.compile(exp)
    trace = next(
        t
        for t in simulate_output(compiled, duration_s=400e-9)["traces"]
        if t["card"] == "A" and t["channel"] == 0
    )
    data = trace["voltage_v"][10:-10]
    origin = trace["time_s"][10] + 4e-9
    times = origin + np.arange(len(data)) / trace["sample_rate_hz"]
    path = tmp_path / "capture.npz"
    np.savez(path, voltage=data + 0.02, time_s=times)
    metadata = {
        "artifact_sha256": compiled.sha256,
        "point": 0,
        "shot": 0,
        "attempt": 0,
        "run_id": "synthetic_run",
        "group": "g",
        "card": "A",
        "channel": 0,
        "sample_rate_hz": trace["sample_rate_hz"],
        "time_origin_s": float(origin),
        "deskew_ns": 4.0,
        "calibration_revision": lab.setup.spectrum.groups["g"].calibration["revision"],
        "synthetic": True,
    }
    return compiled, path, metadata


def test_fixed_deskew_retains_raw_error_and_exploratory_fit_is_separate(tmp_path):
    artifact, path, meta = capture(tmp_path)
    plain = compare_capture(artifact, path, meta)
    fitted = compare_capture(artifact, path, meta, exploratory_alignment=True)
    assert plain["raw_residual_rms_v"] == pytest.approx(0.02)
    assert fitted["raw_residual_rms_v"] == plain["raw_residual_rms_v"]
    assert fitted["exploratory_fit"]["rms_v"] < 1e-12
    assert fitted["exploratory_fit"]["used_for_acceptance"] is False
    assert fitted["physical_acceptance"] == "not_evaluated"
    np.testing.assert_array_equal(
        plain["predicted_voltage_v"], fitted["predicted_voltage_v"]
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("artifact_sha256", "wrong"),
        ("calibration_revision", "wrong"),
        ("synthetic", "false"),
        ("shot", 900),
        ("channel", 999),
        ("time_origin_s", 20),
    ],
)
def test_mismatched_or_ambiguous_capture_is_rejected(tmp_path, key, value):
    artifact, path, meta = capture(tmp_path)
    meta[key] = value
    with pytest.raises(ValueError):
        compare_capture(artifact, path, meta)


def test_report_is_offline_and_escapes_metadata(tmp_path):
    artifact, path, meta = capture(tmp_path)
    meta = copy.deepcopy(meta)
    meta["run_id"] = "<script>injected()</script>"
    output = write_capture_comparison(artifact, path, meta, tmp_path / "compare.html")
    document = output["html"].read_text(encoding="utf-8")
    assert "SYNTHETIC CAPTURE" in document
    assert "<script>injected()</script>" not in document
    assert "&lt;script&gt;injected()&lt;/script&gt;" in document

    class Scripts(HTMLParser):
        sources = []

        def handle_starttag(self, tag, attrs):
            if tag == "script" and "src" in dict(attrs):
                self.sources.append(dict(attrs)["src"])

    parser = Scripts()
    parser.feed(document)
    assert not any(s.startswith(("https://", "http://")) for s in parser.sources)


def test_disk_capture_uses_global_point_and_final_samples(tmp_path):
    _, _, meta = capture(tmp_path)
    q = Qubit("q")
    phase = SweepParameter("phase", [0, 0.7, 1.4], unit="rad")
    exp = Experiment("capture_chunks", [q])
    with exp.average(1), exp.sweep(phase, chunk_size=1):
        with exp.realtime(), exp.triggered_sequence(group="g"):
            play(q.rf, Square(800 * ns, amplitude=0.5, frequency=11e6, phase=phase))
    lab = phase_lab()
    eager = lab.compile(exp)
    store = lab.compile_stream(exp, directory=tmp_path / "artifact")
    trace = next(
        t
        for t in simulate_output(eager, point=2, duration_s=400e-9)["traces"]
        if t["card"] == "A" and t["channel"] == 0
    )
    path = tmp_path / "last_point.npz"
    np.savez(path, voltage=trace["voltage_v"][10:-10])
    meta.update(
        artifact_sha256=store.sha256,
        point=2,
        time_origin_s=float(trace["time_s"][10]) + 4e-9,
    )
    compared = compare_capture(store, path, meta)
    assert compared["metadata"]["point"] == 2
    assert compared["raw_residual_rms_v"] == 0
