"""800 ns/200 ns capability limits must be explicit and may never add a gap."""

import json
import runpy
from pathlib import Path

import pytest

from taiyidsl import CompileError
from taiyidsl.artiq import verify_equivalence

SCRIPT = Path(__file__).parents[1] / "examples" / "short_gate_timing.py"
EXAMPLE = runpy.run_path(str(SCRIPT))
build_sequence = EXAMPLE["build_sequence"]


def events(compiled, kind, signal="dds0"):
    return [
        event
        for event in compiled.points[0].physical_events
        if event.kind == kind and event.signal == signal
    ]


@pytest.mark.parametrize("shape", ["square", "ram"])
@pytest.mark.parametrize("duration_ns", [800, 200])
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("phase_style", ["direct", "frame", "rz"])
def test_xy_current_capability_matrix(shape, duration_ns, channels, phase_style):
    reason = "(?i)(strict|latenc|boundary)" if shape == "ram" else "window"
    with pytest.raises(CompileError, match=reason):
        build_sequence(shape, duration_ns, channels=channels, phase_style=phase_style)


@pytest.mark.parametrize("shape", ["square", "ram"])
@pytest.mark.parametrize("duration_ns", [800, 200])
@pytest.mark.parametrize("channels", [1, 2])
def test_exact_same_phase_baseline_is_not_general_xy_support(
    shape, duration_ns, channels
):
    compiled = build_sequence(shape, duration_ns, channels=channels, pattern="repeat")
    point = compiled.points[0]
    assert point.duration_mu == 3 * duration_ns
    for index in range(channels):
        signal = f"dds{index}"
        assert len(events(compiled, "dds_on", signal)) == 1
        assert len(events(compiled, "dds_off", signal)) == 1
        if shape == "ram":
            launches = events(compiled, "dds_ram_start", signal)
            assert len(launches) == 3
            assert {event.params["ram_replay_action"] for event in launches[1:]} == {
                "retrigger"
            }
            assert not events(compiled, "dds_ram_phase", signal)
        else:
            assert len(events(compiled, "dds_prepare", signal)) == 1
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize(
    "duration_ns, zero_tail_ns, words", [(800, 48, 201), (200, 12, 51)]
)
def test_short_ram_envelope_zero_tail_is_measured_without_padding(
    duration_ns, zero_tail_ns, words
):
    summary = EXAMPLE["ram_tail_summary"](duration_ns)
    assert summary["unchanged_zero_tail_ns"] == zero_tail_ns
    assert summary["resident_words"] == words
    assert summary["zero_tail_covers_declared_skew"] == (duration_ns == 800)


@pytest.mark.parametrize("duration_ns, channels", [(800, 2), (200, 1), (200, 2)])
@pytest.mark.parametrize("phase_style", ["direct", "frame"])
def test_equal_latency_counterfactual_isolates_bus_bandwidth(
    duration_ns, channels, phase_style
):
    if duration_ns == 800:
        compiled = build_sequence(
            "ram",
            duration_ns,
            channels=channels,
            phase_style=phase_style,
            counterfactual_equal_latency=True,
        )
        assert verify_equivalence(compiled)["status"] == "passed"
    else:
        with pytest.raises(CompileError, match="preparation window"):
            build_sequence(
                "ram",
                duration_ns,
                channels=channels,
                phase_style=phase_style,
                counterfactual_equal_latency=True,
            )


@pytest.mark.parametrize("phase_style", ["frame", "rz"])
def test_frame_and_virtual_rz_match_direct_800ns_hardware_sequence(phase_style):
    direct = build_sequence("ram", 800, counterfactual_equal_latency=True)
    framed = build_sequence(
        "ram", 800, phase_style=phase_style, counterfactual_equal_latency=True
    )
    for kind in ("dds_ram_start", "dds_ram_phase", "dds_ram_effect"):
        a, b = events(direct, kind), events(framed, kind)
        assert [(event.start_mu, event.duration_mu) for event in a] == [
            (event.start_mu, event.duration_mu) for event in b
        ]
        assert [event.params["ram_pow"] for event in a] == [
            event.params["ram_pow"] for event in b
        ]


def test_report_preserves_rejections_and_exports_only_supported_declared_cases(
    tmp_path,
):
    report = EXAMPLE["run_matrix"](tmp_path)
    assert len(report["cases"]) == 40
    exported = [row for row in report["cases"] if row["exported"]]
    assert len(exported) == 8
    for row in report["cases"]:
        assert not row["hardware_verified"]
        if row["compile_status"] == "rejected":
            assert row["reason"]
            assert not row["exported"]
        else:
            assert row["actual_duration_mu"] == row["requested_duration_mu"]
            assert row["equivalence_check"] == "passed"
        if row["counterfactual_equal_latency"]:
            assert not row["exported"]
            assert row["envelope"]["declared_carrier_lead_ns"] == 0
            assert row["envelope"]["zero_tail_covers_declared_skew"]
            assert row["timing_contract_status"] == (
                "counterfactual_NOT_physical_calibration"
            )
    for row in exported:
        assert Path(row["source"]).is_file()
        assert row["offline_export_check"] == "passed"
    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved == report
    assert report["driver_cursor_reference"]["square_profile_write_mu"] == 1248
    assert report["driver_cursor_reference"]["ram_pow_write_mu"] == 416
