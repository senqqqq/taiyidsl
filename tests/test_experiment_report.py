"""The review bundle must decode the same binary programs that will be uploaded."""

import ast
import copy
import hashlib
import html
import json
import math
import runpy
from pathlib import Path

import pytest

from taiyidsl import (
    Experiment,
    LabConfig,
    PolynomialPhase,
    Qubit,
    Square,
    SweepParameter,
    delay,
    ns,
    reset_frame,
    reset_phase,
    rz,
    trigplay,
    us,
)
from taiyidsl.reporting.spectrum_report import card_execution

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "artiq_spectrum"


def test_report_preserves_ramsey_samples_steps_trigger_and_frame(tmp_path):
    source = EXAMPLES / "ramsey_long_delay.py"
    exp = runpy.run_path(str(source))["build_experiment"](delays_ms=(100,))
    compiled = LabConfig.load(EXAMPLES / "lab_simulated.json").compile(exp)
    result = compiled.export_report(tmp_path, source_path=source)
    report = json.loads(result["report"].read_text(encoding="utf-8"))
    assert report["hardware_status"] == "not_verified"
    assert report["shot_initial_frame"] == {
        "F": 0,
        "carrier_epoch": "per_triggered_sequence",
        "timeline_reference": "shot_logical_T0",
    }
    point = report["points"][0]
    assert point["trigger_count"] == 1  # One synchronous group launch.
    assert point["trigger_route_count"] == 2
    assert point["terminal_virtual_z_rad"]["q"] == pytest.approx(math.pi / 2)
    assert point["terminal_frame_rad"]["q"] == pytest.approx(-math.pi / 2)
    group = point["groups"][0]
    assert group["trigger_us_T0"] == pytest.approx(19.488)
    card = group["cards"]["A"]
    assert card["sample_zero_us_T0"] == pytest.approx(19.888)
    assert [p["start_us_T0"] for p in card["pulses"]] == [20, 100220]
    assert group["phase_reference"]["mode"] == "sequence"
    assert group["carrier_reference_us_T0"] == pytest.approx(20)
    assert card["pulses"][0]["start_phase_deg"] == pytest.approx(0)
    assert card["pulses"][1]["start_phase_deg"] == pytest.approx(270)
    assert max(step["loops"] for step in card["steps"]) == 31249
    for item in group["cards"].values():
        folder = tmp_path / item["directory"]
        for segment in item["segments"]:
            packed = (folder / f"segment_{segment['segment']}.bin").read_bytes()
            assert hashlib.sha256(packed).hexdigest() == segment["sha256"]
            assert len(packed) == segment["bytes"]
        assert (folder / "steps.csv").exists()
    assert (tmp_path / "dsl_source.py").read_bytes() == source.read_bytes()
    page = result["index"].read_text(encoding="utf-8")
    assert "19.488" in page and "31249" in page
    assert 'href="waveform.html?point=0' in page
    assert 'href="export/artiq/experiment.py"' in page
    assert "reset_frame" in page
    assert "reset_phase" in page and "Φ_osc" in page
    assert "reset_frame clears F" in page and "H − V" not in page
    assert result["pulse_sheet"].exists() and result["waveform"].exists()
    execution = report["execution"]
    assert execution["status"] == "code_preview_not_execution"
    sources = execution["sources"]
    runtime_path = tmp_path / sources["spectrum_runtime"]["path"]
    assert (
        runtime_path.read_bytes()
        == (ROOT / "src/taiyidsl/runtime/spectrum/awg.py").read_bytes()
    )
    for name, package_path in {
        "spectrum_dds": "backends/artiq_spectrum/dds.py",
        "spectrum_dds_runtime": "runtime/spectrum/dds.py",
        "spectrum_identity": "spectrum_identity.py",
    }.items():
        assert (tmp_path / sources[name]["path"]).read_bytes() == (
            ROOT / "src/taiyidsl" / package_path
        ).read_bytes()
    for record in sources.values():
        assert (
            hashlib.sha256((tmp_path / record["path"]).read_bytes()).hexdigest()
            == (record["sha256"])
        )
    runtime = runtime_path.read_text(encoding="utf-8").splitlines()
    for span in execution["runtime_methods"].values():
        snippet = "\n".join(runtime[span["start_line"] - 1 : span["end_line"]])
        assert html.escape(snippet) in page
    for name in ("host_bridge", "artiq_kernel"):
        source_text = (tmp_path / sources[name]["path"]).read_text(encoding="utf-8")
        assert html.escape(source_text) in page
    manifest = json.loads(result["export"]["manifest"].read_text(encoding="utf-8"))
    for name, item in manifest["points"][0]["groups"][0]["cards"].items():
        card_view = group["cards"][name]
        resolved = card_view["spectrum_execution"]
        listing = (tmp_path / card_view["directory"] / "sdk_calls.txt").read_text(
            encoding="utf-8"
        )
        assert listing == resolved["upload_listing"]
        ast.parse(listing)  # Syntax only: the inspection listing is never run.
        assert html.escape(listing) in page
        slots = {s["id"]: i for i, s in enumerate(item["program"]["segments"])}
        for actual, expected in zip(
            resolved["step_writes"], item["program"]["steps"], strict=True
        ):
            assert actual == {
                "step_index": expected["index"],
                "next_step_index": expected["next_step"],
                "segment_index": slots[expected["segment_id"]],
                "loops": expected["loops"],
                "flags": "SPCSEQ_END" if expected["end"] else "SPCSEQ_ENDLOOPALWAYS",
            }
        config = {row["setting"]: row for row in resolved["configuration"]}
        assert config["trigger.ext0_level0 [mV]"]["source"] == "adapter default"
        assert config["trigger.delay [samples]"]["value"] == 0
        assert config["trigger_latency_samples"]["value"] == 500
        assert "CARD_ENABLETRIGGER" not in listing  # All uploads precede arm.
    # Noncontiguous logical IDs must still use compact hardware slots.
    item = copy.deepcopy(manifest["points"][0]["groups"][0]["cards"]["A"])
    remap = {s["id"]: 7 + 13 * i for i, s in enumerate(item["program"]["segments"])}
    for segment in item["program"]["segments"]:
        segment["id"] = remap[segment["id"]]
    for step in item["program"]["steps"]:
        step["segment_id"] = remap[step["segment_id"]]
    resolved = card_execution(item)
    assert [u["hardware_slot"] for u in resolved["uploads"]] == list(range(len(remap)))
    assert resolved["step_writes"][0]["segment_index"] == 0
    assert "segment_loader(program, 7)" in resolved["upload_listing"]
    assert "sequence.write_segment(0)" in resolved["upload_listing"]
    compiled.assert_integrity()


def test_report_retains_frame_only_region_without_fake_launch(tmp_path):
    q = Qubit("q")
    exp = Experiment("frame only </script><img src=x>", [q])
    with exp.average(2):
        with exp.realtime():
            with exp.triggered_sequence(group="qubit_awg"):
                delay(400 * us)
                rz(q, math.pi / 2)
    compiled = LabConfig.load(EXAMPLES / "lab_simulated.json").compile(exp)
    source = tmp_path / "source.py"
    source.write_text("# __POINTS__ __HASH__ <img src=x>", encoding="utf-8")
    result = compiled.export_report(tmp_path, source_path=source)
    point = json.loads(result["report"].read_text(encoding="utf-8"))["points"][0]
    assert point["trigger_count"] == 0
    assert point["terminal_virtual_z_rad"]["q"] == pytest.approx(math.pi / 2)
    assert point["terminal_frame_rad"]["q"] == pytest.approx(-math.pi / 2)
    assert point["frame_events"][0]["time_us_T0"] == 400
    text = result["index"].read_text(encoding="utf-8")
    assert "No active AWG program" in text
    assert "<img src=x>" not in text
    assert "&lt;img src=x&gt;" in text
    assert "# __POINTS__ __HASH__ &lt;img src=x&gt;" in text
    assert point["groups"] == []
    assert not list(tmp_path.glob("point_*/group_*/card_*/sdk_calls.txt"))


def test_report_start_phase_includes_local_modulation_constant(tmp_path):
    q = Qubit("q")
    exp = Experiment("nonzero local initial phase", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.triggered_sequence(group="qubit_awg"):
                trigplay(
                    q.rf,
                    Square(
                        400 * ns,
                        amplitude=0.2,
                        frequency=30000,
                        phase_modulation=PolynomialPhase((math.pi / 3, math.pi)),
                    ),
                )
    compiled = LabConfig.load(EXAMPLES / "lab_simulated.json").compile(exp)
    result = compiled.export_report(tmp_path)
    report = json.loads(result["report"].read_text(encoding="utf-8"))
    card = report["points"][0]["groups"][0]["cards"]["A"]
    phase = card["pulses"][0]["start_phase_deg"]
    assert phase == pytest.approx(60)
    first = compiled.simulate_output(start_s=0, duration_s=0.8e-9)["traces"][0]
    assert first["dac_codes"][0] == round(32767 * 0.2 * math.cos(math.radians(phase)))


def test_large_chunk_report_splits_point_tables_without_losing_frames(tmp_path):
    q = Qubit("q")
    angle = SweepParameter("angle", list(range(23)), "rad")
    exp = Experiment("paged frame-only scan", [q])
    with exp.average(1, scope="chunk"), exp.sweep(angle, chunks=1), exp.realtime():
        with exp.triggered_sequence(group="qubit_awg"):
            rz(q, angle)
    compiled = LabConfig.load(EXAMPLES / "lab_simulated.json").compile(exp)
    result = compiled.export_report(tmp_path)
    overview = result["index"].read_text(encoding="utf-8")
    first = (tmp_path / "chunk_0_page_0.html").read_text(encoding="utf-8")
    last = (tmp_path / "chunk_0_page_1.html").read_text(encoding="utf-8")
    assert "<summary>Point 19 ·" in overview and "<summary>Point 20 ·" not in overview
    assert "<summary>Point 19 ·" in first and "<summary>Point 20 ·" not in first
    assert "<summary>Point 22 ·" in last and "<summary>Point 0 ·" not in last
    report = json.loads(result["report"].read_text(encoding="utf-8"))
    assert report["points"][22]["terminal_virtual_z_rad"]["q"] == 22
    assert report["points"][22]["terminal_frame_rad"]["q"] == -22


def test_report_distinguishes_frame_reset_and_new_carrier_origin(tmp_path):
    q = Qubit("q")
    exp = Experiment("phase resets", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        trigplay(q.rf, Square(400 * ns, amplitude=0.2, frequency=30_000))
        rz(q, 0.4)
        reset_frame(q.frame)
        delay(400 * ns)
        reset_phase(q.frame)
        trigplay(q.rf, Square(400 * ns, amplitude=0.2, frequency=30_000))
    compiled = LabConfig.load(EXAMPLES / "lab_simulated.json").compile(exp)
    result = compiled.export_report(tmp_path)
    report = json.loads(result["report"].read_text(encoding="utf-8"))
    point = report["points"][0]
    assert point["terminal_frame_rad"]["q"] == 0
    assert point["terminal_virtual_z_rad"]["q"] == 0
    assert [e["operation"] for e in point["frame_events"]] == [
        "rz",
        "reset_frame",
        "reset_phase",
    ]
    second = point["groups"][0]["cards"]["A"]["pulses"][1]
    assert second["carrier_reference"] == "explicit_reset_phase"
    assert second["carrier_reference_us_T0"] == pytest.approx(0.8)
    assert second["start_phase_deg"] == pytest.approx(0)
