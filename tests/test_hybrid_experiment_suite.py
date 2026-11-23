"""Check the actual independent example programs, gate algebra and packed samples."""

import json
import math
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from taiyidsl import LabConfig
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.backends.artiq_spectrum.export import artiq_subplan

FOLDER = Path(__file__).resolve().parents[1] / "examples" / "artiq_spectrum"
NAMES = ("rabi", "spin_echo", "spin_locking", "allxy")
X = np.array([[0, 1], [1, 0]], dtype=complex)
Y = np.array([[0, -1j], [1j, 0]], dtype=complex)
Z = np.diag([1, -1]).astype(complex)
GROUND = np.array([1, 0], dtype=complex)


@pytest.fixture(scope="module")
def modules():
    # run_path does not modify sys.path; these scripts have no sibling imports.
    return {name: runpy.run_path(str(FOLDER / f"{name}.py")) for name in NAMES}


@pytest.fixture(scope="module")
def compiled(modules):
    lab = LabConfig.load(FOLDER / "lab_simulated.json")
    return {
        name: lab.compile(module["build_experiment"]())
        for name, module in modules.items()
    }


def rf_events(point):
    return [
        event
        for event in point.logical_events
        if event.kind == "play" and event.params.get("route") == "spectrum"
    ]


def program(point):
    groups = [
        event for event in point.physical_events if event.kind == "spectrum_program"
    ]
    return groups[0].params["cards"]["A"]["program"] if groups else None


def rotation(angle, phase):
    return math.cos(angle / 2) * np.eye(2) - 1j * math.sin(angle / 2) * (
        math.cos(phase) * X + math.sin(phase) * Y
    )


def gate_matrix(event):
    # This is explicitly the example's IDEAL area model, not a lab calibration.
    angle = math.pi * event.params["amplitude"] / 0.4
    angle *= event.params["requested_duration_s"] / 200e-6
    return rotation(angle, event.params["phase"])


@pytest.mark.parametrize("name", NAMES)
def test_defaults_preserve_native_preparation_and_one_region_trigger(compiled, name):
    result = compiled[name]
    assert result.provenance["backend"]["name"] == "artiq_spectrum"
    assert result.metadata["offline_only"] is True
    assert result.metadata["calibrated_rotations"] is False
    assert result.metadata["hardware_verified"] is False
    assert result.shots == 2 and result.averaging_mode == "cyclic"
    assert list(result.execution_order()) == [
        (point, shot) for shot in range(2) for point in range(len(result.points))
    ]
    native = artiq_subplan(result)
    for point, native_point in zip(result.points, native.points, strict=True):
        regions = [e for e in point.logical_events if e.kind == "spectrum_region"]
        assert len(regions) == 1
        expected_triggers = int(program(point) is not None)
        triggers = result.setup.spectrum.groups["qubit_awg"].trigger_device_names
        assert triggers == ("awg_trigger_a", "awg_trigger_b")
        for edge in ("ttl_on", "ttl_off"):
            times = [
                [
                    e.start_mu
                    for e in point.physical_events
                    if e.signal == name and e.kind == edge
                ]
                for name in triggers
            ]
            assert all(len(values) == expected_triggers for values in times)
            assert times[0] == times[1]
        assert any(e.signal == "cooling_dds" for e in native_point.physical_events)
        assert any(e.signal == "readout_ttl" for e in native_point.physical_events)
        assert not any(
            e.params.get("route") == "spectrum" for e in native_point.logical_events
        )
        assert not any(e.signal == "nuclear_rf" for e in native_point.physical_events)


def test_rabi_cross_product_is_bound_before_sample_generation(compiled):
    result = compiled["rabi"]
    assert len(result.points) == 18
    for point in result.points:
        (logical,) = rf_events(point)
        (pulse,) = program(point)["pulses"]
        assert logical.params["amplitude"] == point.coordinates["amplitude"]
        assert logical.params["requested_duration_s"] == point.coordinates["duration"]
        assert pulse["amplitude"] == point.coordinates["amplitude"]
        assert pulse["duration_samples"] == round(
            point.coordinates["duration"] * 1.25e9
        )


def test_echo_has_equal_dark_waits_and_refocuses_static_detuning(compiled):
    result = compiled["spin_echo"]
    for point in result.points:
        first, middle, last = rf_events(point)
        wait = point.coordinates["half_wait"]
        assert (
            middle.start_mu - first.end_mu
        ) * result.setup.tick_seconds == pytest.approx(wait)
        assert (
            last.start_mu - middle.end_mu
        ) * result.setup.tick_seconds == pytest.approx(wait)
        assert [e.params["phase"] for e in (first, middle, last)] == [0, math.pi / 2, 0]
        # Static detuning acts during both dark intervals; finite-pulse errors
        # are deliberately not represented by this ideal algebra check.
        for detuning_phase in (0.0, 0.137, 1.91):
            free = np.diag(np.exp(-0.5j * detuning_phase * np.array([1, -1])))
            state = (
                gate_matrix(last)
                @ free
                @ gate_matrix(middle)
                @ free
                @ gate_matrix(first)
                @ GROUND
            )
            assert abs(state[1]) ** 2 == pytest.approx(1)


def test_spin_locking_axes_zero_reference_and_frame_persistence(compiled, modules):
    restored = compiled["spin_locking"]
    for point in restored.points:
        events = rf_events(point)
        expected = (
            [math.pi / 2, -math.pi / 2]
            if point.coordinates["lock_time"] == 0
            else [math.pi / 2, 0, -math.pi / 2]
        )
        assert [event.params["phase"] for event in events] == expected
        prepared = gate_matrix(events[0]) @ GROUND
        assert np.vdot(prepared, X @ prepared).real == pytest.approx(1)
        state = GROUND.copy()
        for event in events:
            state = gate_matrix(event) @ state
        assert abs(state[0]) ** 2 == pytest.approx(1)
        resets = [e for e in point.logical_events if e.kind == "reset_frame"]
        assert resets[0].params["effective_frame_phase"] == 0
        assert restored.virtual_z_frames(point.index)["q"] == 0

    lab = LabConfig.load(FOLDER / "lab_simulated.json")
    persisted = lab.compile(
        modules["spin_locking"]["build_experiment"](restore_frame=False)
    )
    for before, after in zip(restored.points, persisted.points, strict=True):
        assert persisted.virtual_z_frames(after.index)["q"] == pytest.approx(
            math.pi / 2
        )
        # A terminal frame restoration changes the logical frame, not the RF bytes.
        assert [s["sha256"] for s in program(before)["segments"]] == [
            s["sha256"] for s in program(after)["segments"]
        ]
        assert rf_events(after)[0].params["phase"] == pytest.approx(math.pi / 2)


def test_allxy_exact_order_fixed_identity_slots_and_ideal_staircase(compiled, modules):
    pairs = modules["allxy"]["ALLXY_PAIRS"]
    assert ["".join(pair) for pair in pairs] == [
        "II",
        "XX",
        "YY",
        "XY",
        "YX",
        "xI",
        "yI",
        "xy",
        "yx",
        "xY",
        "yX",
        "Xy",
        "Yx",
        "xX",
        "Xx",
        "yY",
        "Yy",
        "XI",
        "YI",
        "xx",
        "yy",
    ]
    result = compiled["allxy"]
    populations = []
    for point, pair in zip(result.points, pairs, strict=True):
        (region,) = [e for e in point.logical_events if e.kind == "spectrum_region"]
        assert region.duration_mu * result.setup.tick_seconds == pytest.approx(400e-6)
        operations = [
            e
            for e in point.logical_events
            if e.kind in ("play", "scope_delay") and e.params.get("region_id")
        ]
        assert len(operations) == 2
        state = GROUND.copy()
        for index, (gate, event) in enumerate(zip(pair, operations, strict=True)):
            assert event.start_mu == region.start_mu + index * 1_000_000
            assert event.duration_mu == 1_000_000  # 200 us on the 0.2 ns hybrid tick.
            if gate == "I":
                assert event.kind == "scope_delay"
            else:
                assert event.params["amplitude"] == (0.4 if gate.isupper() else 0.2)
                state = gate_matrix(event) @ state
        populations.append(abs(state[1]) ** 2)
    np.testing.assert_allclose(populations, [0] * 5 + [0.5] * 12 + [1] * 4, atol=1e-14)
    assert program(result.points[0]) is None  # II keeps its full dark time, no TTL.


@pytest.mark.parametrize("name", NAMES)
def test_packed_rf_uses_resolved_axis_and_sequence_reference(compiled, name):
    result = compiled[name]
    for point in result.points:
        card_program = program(point)
        if card_program is None:
            continue
        reference = next(
            event.start_mu
            for event in point.logical_events
            if event.kind == "spectrum_region"
        )
        for logical, pulse in zip(
            rf_events(point), card_program["pulses"], strict=True
        ):
            expected_phase = (
                2
                * math.pi
                * 30_000
                * (logical.start_mu - reference)
                * result.setup.tick_seconds
                + logical.params["phase"]
            ) % (2 * math.pi)
            phase_error = math.remainder(pulse["phase_rad"] - expected_phase, math.tau)
            assert phase_error == pytest.approx(0, abs=1e-10)
            start = pulse["start_sample"]
            actual = reconstruct_window(card_program, start, start + 32)[:, 0]
            expected = np.rint(
                32767
                * pulse["amplitude"]
                * np.cos(expected_phase + 2 * math.pi * 30_000 * np.arange(32) / 1.25e9)
            ).astype(np.int16)
            np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("name", NAMES)
def test_main_delegates_to_offline_report_without_sdk(
    modules, name, tmp_path, monkeypatch, capsys
):
    calls = []

    def report(compiled, output, *, source_path):
        calls.append((compiled, output, source_path))
        return {"index": output / "index.html"}

    monkeypatch.setitem(sys.modules, "spcm", None)
    monkeypatch.setitem(
        sys.modules,
        "taiyidsl.reporting.experiment_report",
        SimpleNamespace(export_experiment_report=report),
    )
    assert modules[name]["main"](["--output", str(tmp_path)]) == {
        "index": tmp_path / "index.html"
    }
    assert len(calls) == 1
    assert calls[0][1] == tmp_path
    assert Path(calls[0][2]).resolve() == FOLDER / f"{name}.py"
    assert str((tmp_path / "index.html").resolve()) in capsys.readouterr().out


def test_actual_spin_locking_main_exports_review_bundle(modules, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "spcm", None)
    paths = modules["spin_locking"]["main"](
        ["--output", str(tmp_path), "--persist-frame"]
    )
    for key in ("index", "report", "pulse_sheet", "waveform"):
        assert paths[key].is_file()
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert report["hardware_status"] == "not_verified"
    assert len(report["points"]) == 4
    for point in report["points"]:
        assert point["trigger_count"] == 1
        assert point["terminal_virtual_z_rad"]["q"] == pytest.approx(math.pi / 2)
    assert "spin_locking.py" in (tmp_path / "export" / "compiled.json").read_text(
        encoding="utf-8"
    )
