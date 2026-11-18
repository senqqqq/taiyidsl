"""Compare final AWG bytes, step tables, ARTIQ code and persistent frames.

All variants use the same three-point Ramsey and an unmeasured offline profile.
No SDK or instrument is opened; comparison distinguishes physical from logical
equivalence instead of discarding frame differences as metadata.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from taiyidsl import (
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    delay,
    play,
    reset_frame,
    rz,
    trigplay,
    us,
)
from taiyidsl.spectrum_waveforms import materialize_segment, reconstruct_window


def build(variant, *, third_pulse=False):
    q = Qubit("q")
    exp = Experiment("phase_equivalence", [q])
    wait = SweepParameter("dark_time", (0.01, 1, 100), "ms")
    pulse = Square(200 * us, amplitude=0.2, frequency=30000, phase=0)
    with exp.average(2, mode="cyclic"), exp.sweep(wait), exp.realtime():
        play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
        with exp.triggered_sequence(group="qubit_awg"):
            if variant == "explicit_start_reset":
                reset_frame(q.frame)
            trigplay(q.rf, pulse)
            delay(wait)
            if variant in {"phase_plus", "phase_minus"}:
                phase = math.pi / 2 if variant == "phase_plus" else -math.pi / 2
                trigplay(q.rf, pulse, phase=phase)
            else:
                rz(q, math.pi / 2)
                if variant == "reset_after_rz":
                    reset_frame(q.frame)
                trigplay(q.rf, pulse)
                if variant == "virtual_z_restored":
                    rz(q, -math.pi / 2)
            if third_pulse:
                trigplay(q.rf, pulse)
        play("readout", Square(10 * us))
    return exp


def group(point):
    return next(e.params for e in point.physical_events if e.kind == "spectrum_program")


def card_data(program):
    arrays = [materialize_segment(program, seg["id"]) for seg in program["segments"]]
    digests = [hashlib.sha256(data.tobytes()).hexdigest() for data in arrays]
    assert digests == [seg["sha256"] for seg in program["segments"]]
    return arrays, digests


def compare(left, right):
    result = []
    for a, b in zip(left.points, right.points, strict=True):
        ga, gb = group(a), group(b)
        cards = {}
        for name in ga["cards"]:
            pa, pb = ga["cards"][name]["program"], gb["cards"][name]["program"]
            aa, ha = card_data(pa)
            ab, hb = card_data(pb)
            cards[name] = {
                "packed_segments_equal": len(aa) == len(ab)
                and all(np.array_equal(x, y) for x, y in zip(aa, ab, strict=True)),
                "step_tables_equal": pa["steps"] == pb["steps"],
                "left_hashes": ha,
                "right_hashes": hb,
            }
        result.append(
            {
                "point": a.index,
                "dark_time_s": a.coordinates["dark_time"],
                "cards": cards,
                "terminal_frames_equal": ga["terminal_frames"] == gb["terminal_frames"],
                "left_terminal_frames": ga["terminal_frames"],
                "right_terminal_frames": gb["terminal_frames"],
            }
        )
    return result


def second_phase(compiled):
    return [
        math.degrees(group(p)["cards"]["A"]["program"]["pulses"][1]["phase_rad"])
        for p in compiled.points
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/artiq_spectrum/phase_equivalence"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    lab = LabConfig.load(root / "examples/artiq_spectrum/lab_simulated.json")
    variants = (
        "virtual_z",
        "phase_plus",
        "phase_minus",
        "virtual_z_restored",
        "explicit_start_reset",
        "reset_after_rz",
    )
    compiled, sources = {}, {}
    for name in variants:
        compiled[name] = lab.compile(build(name))
        artifact = compiled[name].export(args.output / name)
        assert artifact["verification"]["offline"] == "passed"
        sources[name] = (args.output / name / "artiq/experiment.py").read_text(
            encoding="utf-8"
        )
    comparisons = {
        name: compare(compiled["virtual_z"], compiled[name]) for name in variants[1:]
    }
    for name, rows in comparisons.items():
        for row in rows:
            expected_equal = name != "phase_plus"
            assert row["cards"]["A"]["packed_segments_equal"] is expected_equal
            assert all(card["step_tables_equal"] for card in row["cards"].values())
            assert row["cards"]["B"]["packed_segments_equal"]
            assert row["terminal_frames_equal"] is (
                name in {"explicit_start_reset", "reset_after_rz"}
            )
    assert len(set(sources.values())) == 1, "Unexpected native ARTIQ code difference"

    # A third pulse exposes persistent V versus a local per-play phase override.
    third = {
        name: lab.compile(build(name, third_pulse=True))
        for name in ("virtual_z", "phase_minus", "virtual_z_restored")
    }
    third_report = []
    for index in range(3):
        samples, phases = {}, {}
        for name, experiment in third.items():
            program = group(experiment.points[index])["cards"]["A"]["program"]
            pulse = program["pulses"][2]
            start = pulse["start_sample"]
            samples[name] = reconstruct_window(program, start, start + 250000)
            phases[name] = math.degrees(pulse["phase_rad"])
        assert not np.array_equal(samples["virtual_z"], samples["phase_minus"])
        assert np.array_equal(samples["virtual_z_restored"], samples["phase_minus"])
        third_report.append({"point": index, "third_pulse_phase_deg": phases})

    original = compiled["virtual_z"]
    shot_report = []
    for point, shot in original.execution_order():
        first = next(
            e
            for e in original.points[point].logical_events
            if e.kind == "play" and e.params.get("route") == "spectrum"
        )
        assert first.params["frame_phase"] == first.params["virtual_z_phase"] == 0
        program = group(original.points[point])["cards"]["A"]["program"]
        pulse = program["pulses"][0]
        assert pulse["start_sample"] == 140
        prefix = reconstruct_window(program, 0, 141)
        assert np.all(prefix[:140] == 0) and prefix[140, 0] != 0
        shot_report.append(
            {
                "point": point,
                "shot": shot,
                "initial_H": first.params["frame_phase"],
                "initial_V": first.params["virtual_z_phase"],
                "first_pulse_phase_deg": math.degrees(pulse["phase_rad"]),
                "first_segment_sha256": program["segments"][0]["sha256"],
                "first_rf_sample_code": int(prefix[140, 0]),
                "terminal_V": original.virtual_z_frames(point)["q"],
            }
        )
    report = {
        "status": "passed",
        "hardware": "not_verified",
        "original_delays_ms": [0.01, 1, 100],
        "second_pulse_phase_deg": {k: second_phase(v) for k, v in compiled.items()},
        "comparisons_to_positive_virtual_z": comparisons,
        "native_artiq_source_identical_all_variants": True,
        "third_pulse_probe": third_report,
        "shot_initialization": shot_report,
        "shot_initialization_scope": "compiled replay contract; no hardware executed",
        "trigger_lead_ns": 512,
        "declared_latency_ns": 400,
        "zero_prefix_samples": 140,
        "zero_prefix_ns": 112,
    }
    (args.output / "comparison.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["second_pulse_phase_deg"], indent=2))
    print("PASS: packed bytes, steps, native source, frame persistence and shot starts")


if __name__ == "__main__":
    main()
