"""Native runtime prefixes must never be plotted as a known full shot duration."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from taiyidsl import (
    Experiment,
    LabConfig,
    NativeKernelPackage,
    Qubit,
    Square,
    SweepParameter,
    delay,
    ms,
    rz,
    trigplay,
    us,
)
from taiyidsl.native_view import native_lifecycle, native_lifecycle_html
from taiyidsl.output_simulator import _payload as output_payload
from taiyidsl.visualization import _payload as sheet_payload

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/artiq_spectrum"


@pytest.fixture(scope="module")
def native_compiled(tmp_path_factory):
    # Keep the earlier prefix-only contract covered independently of evolving
    # GreenMOT examples, which now move real timed work into the control window.
    source = tmp_path_factory.mktemp("native_view") / "native_view_fixture.py"
    source.write_text(
        """from artiq.experiment import *
class Stages(EnvExperiment):
    def build(self):
        self.setattr_device('core')
        self.setattr_device('native_ttl')
    @kernel
    def prepare_MOT(self):
        self.core.break_realtime()
        delay(1 * ms)
    @kernel
    def cooling(self, duration_s):
        delay(duration_s)
    @kernel
    def execute_readout(self):
        delay(100.48 * ms)
""",
        encoding="utf-8",
    )
    package = NativeKernelPackage(
        module="native_view_fixture",
        class_name="Stages",
        sources={"native_view_fixture": source},
        resources=("native_ttl",),
    )
    q = Qubit("q")
    exp = Experiment("legacy_native_view", [q])
    dark = SweepParameter("dark", [0.01, 1], "ms")
    pulse = Square(200 * us, frequency=30_000, amplitude=0.2)
    with exp.average(2):
        with exp.sweep(dark):
            with exp.realtime():
                package.kernel("prepare_MOT", timing="prefix")()
                package.kernel("cooling", timing="prefix")(1 * ms)
                with exp.triggered_sequence(group="qubit_awg"):
                    trigplay(q.rf, pulse)
                    delay(dark)
                    rz(q, 1.5707963267948966)
                    trigplay(q.rf, pulse)
                package.kernel(
                    "execute_readout", timing="suffix", duration=100.48 * ms
                )()
    return LabConfig.load(EXAMPLES / "lab_simulated.json").compile(exp)


def test_native_lifecycle_keeps_unknown_prefix_and_exact_suffix(native_compiled):
    lifecycle = native_lifecycle(native_compiled)
    assert lifecycle["execution"] == "one_top_level_kernel_per_shot"
    assert lifecycle["time_reference"] == "control_epoch_after_native_prefix"
    assert lifecycle["full_shot_duration_s"] is None
    assert lifecycle["prefix_in_carrier_reference"] is False
    assert lifecycle["native_waveforms_simulated"] is False
    first = lifecycle["points"][0]
    assert [call["method"] for call in first["prefix"]] == ["prepare_MOT", "cooling"]
    assert all(call["duration_s"] is None for call in first["prefix"])
    assert first["control_duration_s"] == pytest.approx(410e-6)
    suffix = first["suffix"][0]
    assert suffix["start_s_from_control_T0"] == pytest.approx(410e-6)
    assert suffix["duration_s"] == pytest.approx(100.48e-3)
    assert suffix["duration_mu"] == 100480000
    assert suffix["end_s_from_control_T0"] == pytest.approx(100.89e-3)
    assert suffix["waveform"] == "opaque_not_simulated"
    assert lifecycle["points"][1]["control_duration_s"] == pytest.approx(1.4e-3)


def test_native_sheet_and_simulator_label_the_control_epoch(native_compiled, tmp_path):
    sheet = sheet_payload(native_compiled)
    assert sheet[0]["time_reference"] == "control_epoch_after_native_prefix"
    assert sheet[0]["duration_us"] == pytest.approx(410)
    assert not any(
        event["kind"] == "native_call" for event in sheet[0]["logical_events"]
    )
    page = native_compiled.visualize(tmp_path / "pulse_sheet.html").read_text(
        encoding="utf-8"
    )
    assert "Known span shown" in page
    assert "Full shot duration is unknown" in page
    assert "prepare_MOT() → cooling(0.001)" in page
    assert "execute_readout(): 410–100890 µs" in page
    assert "control epoch T0" in page
    decoded = native_compiled.simulate_output(duration_s=80e-9)
    assert decoded["time_reference"] == "control_epoch_after_native_prefix"
    assert decoded["full_shot_duration_s"] is None
    assert decoded["native_waveforms_simulated"] is False
    payload = output_payload(native_compiled)
    assert payload["native_lifecycle"]["full_shot_duration_s"] is None
    waveform = native_compiled.waveform_simulator()
    assert "Within each shot, all cards use the same control epoch T0;" in " ".join(
        waveform.split()
    )
    assert "native waveforms are opaque" in waveform
    native_compiled.assert_integrity()


def test_native_report_links_composed_program_and_records_source_audit(
    native_compiled, tmp_path
):
    result = native_compiled.export_report(tmp_path)
    report = json.loads(result["report"].read_text(encoding="utf-8"))
    assert report["shot_initial_frame"]["timeline_reference"] == (
        "control_epoch_after_native_prefix"
    )
    assert report["shot_initial_frame"]["carrier_epoch"] == "per_triggered_sequence"
    assert report["native_contract"]["source_audit"]["status"] == "passed"
    assert report["native_composition"]["execution"] == "one_top_level_kernel_per_shot"
    assert report["native_lifecycle"]["full_shot_duration_s"] is None
    page = result["index"].read_text(encoding="utf-8")
    assert "Top-level ARTIQ kernel source" in page
    assert "Source boundary audit: passed" in page
    assert "原生 MOT/cooling prefix 的运行时间不计入载波参考" in page
    assert "包括 cooling 和 dark delay" not in page
    assert 'href="export/artiq/experiment.py"' in page


def test_non_native_report_retains_ordinary_time_reference(tmp_path):
    example = runpy.run_path(str(EXAMPLES / "ramsey_long_delay.py"))
    compiled = LabConfig.load(EXAMPLES / "lab_simulated.json").compile(
        example["build_experiment"](delays_ms=(0.01,))
    )
    assert native_lifecycle(compiled) is None
    assert native_lifecycle_html(compiled) == ""
    assert "native_lifecycle" not in sheet_payload(compiled)[0]
    assert "native_lifecycle" not in output_payload(compiled)
    assert compiled.simulate_output()["time_reference"] == "shot_logical_T0"
    result = compiled.export_report(tmp_path)
    report = json.loads(result["report"].read_text(encoding="utf-8"))
    assert "native_contract" not in report
    assert "native_composition" not in report
    page = result["index"].read_text(encoding="utf-8")
    assert "默认以 triggered_sequence 入口为载波参考" in page
    assert "Native kernel lifecycle" not in page


def _timed_view_input(*, with_gate=True, phase_reference="sequence"):
    calls = {
        "index": 0,
        "prefix": [{"method": "prepare_MOT", "args": []}],
        "timed": [
            {
                "method": "finish_preparation",
                "args": [0.001],
                "start_mu_from_T0": 0,
                "duration_mu": 1_000_000,
                "phase_sensitive": False,
                "end_boundary": "final LDAC rising edge <digital only>",
            }
        ],
        "suffix": [
            {
                "method": "execute_readout",
                "args": [],
                "duration_mu": 100_480_000,
                "phase_sensitive": False,
            }
        ],
    }
    events = [
        SimpleNamespace(
            kind="spectrum_region",
            signal="qubit_awg",
            start_mu=5_000_000,
            duration_mu=2_100_000,
            params={"region_id": "r", "phase_reference": phase_reference},
        )
    ]
    if with_gate:
        events.append(
            SimpleNamespace(
                kind="play",
                signal="q.rf",
                start_mu=5_050_000,
                duration_mu=1_000_000,
                params={"region_id": "r"},
            )
        )
    return SimpleNamespace(
        setup=SimpleNamespace(
            tick_seconds=0.2e-9,
            spectrum=SimpleNamespace(artiq_tick_seconds=1e-9),
        ),
        points=[SimpleNamespace(index=0, duration_mu=7_100_000, logical_events=events)],
        metadata={
            "native_artiq": {
                "package": {},
                "source_audit": {"status": "passed"},
                "points": [calls],
            }
        },
    )


def test_timed_native_interval_uses_rtio_mu_and_gap_uses_first_actual_gate():
    compiled = _timed_view_input()
    lifecycle = native_lifecycle(compiled)
    assert lifecycle["timed_in_carrier_reference"] is False
    point = lifecycle["points"][0]
    timed = point["timed"][0]
    assert timed["duration_s"] == pytest.approx(0.001)
    assert timed["end_s_from_control_T0"] == pytest.approx(0.001)
    assert timed["waveform"] == "opaque_not_simulated"
    assert timed["end_boundary"] == "final LDAC rising edge <digital only>"
    assert point["control_duration_s"] == pytest.approx(0.00142)
    assert point["first_spectrum_gate"]["start_s_from_control_T0"] == pytest.approx(
        0.00101
    )
    assert point["timed_to_first_gate"]["gap_s"] == pytest.approx(10e-6)
    assert point["suffix"][0]["start_s_from_control_T0"] == pytest.approx(0.00142)
    page = native_lifecycle_html(compiled)
    assert "Timed native control from T0" in page
    assert "finish_preparation(0.001): 0–1000 µs; duration 1000 µs" in page
    assert "finish_preparation end → first Spectrum gate: 10 µs" in page
    assert "first gate at 1010 µs from T0" in page
    assert "only for an explicit shot reference" in page
    assert "carrier origin at 1000 µs from control T0" in page
    assert "End boundary: final LDAC rising edge &lt;digital only&gt;" in page


def test_native_view_explicit_shot_reference_includes_timed_preparation():
    compiled = _timed_view_input(phase_reference="shot")
    lifecycle = native_lifecycle(compiled)
    assert lifecycle["timed_in_carrier_reference"] is True
    assert lifecycle["points"][0]["carrier_references"] == [
        {"group": "qubit_awg", "mode": "shot", "start_s_from_control_T0": 0.0}
    ]
    assert "carrier origin at 0 µs" in native_lifecycle_html(compiled)


def test_timed_frame_only_point_does_not_invent_a_gate_or_gap():
    compiled = _timed_view_input(with_gate=False)
    point = native_lifecycle(compiled)["points"][0]
    assert point["first_spectrum_gate"] is None
    assert point["timed_to_first_gate"] is None
    assert "No Spectrum gate in this point" in native_lifecycle_html(compiled)
