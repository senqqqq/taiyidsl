"""Freeze/compare representative offline timing, phase, samples and host order.

Only writes the explicitly selected JSON report. Does not export or run hardware.
New diagnostic metadata is allowed; previously recorded semantic fields must match.
"""

import argparse
import hashlib
import json
import runpy
from pathlib import Path

from taiyidsl import LabConfig, Qubit, compile_experiment, ramsey, reference_setup, us
from taiyidsl.dsl import json_value
from taiyidsl.spectrum_waveforms import materialize_segment

ROOT = Path(__file__).resolve().parents[1]


def clean(value):
    if isinstance(value, dict):
        return {
            key: clean(item)
            for key, item in value.items()
            if key not in {"source", "logical_id", "logical_ids"}
        }
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return json_value(value)


def capture():
    folder = ROOT / "examples/artiq_spectrum"
    lab = LabConfig.load(folder / "lab_simulated.json")
    cases = {}
    for name in ("ramsey_long_delay", "spin_echo", "greenmot_ramsey"):
        cases[name] = lab.compile(
            runpy.run_path(str(folder / f"{name}.py"))["build_experiment"]()
        )
    q = Qubit("q0")
    cases["artiq_ramsey"] = compile_experiment(
        ramsey(q, [0 * us, 4 * us, 8 * us], shots=2), *reference_setup([q])
    )
    result = {}
    for name, compiled in cases.items():
        points = []
        for point in compiled.points:
            programs = {}
            for event in point.physical_events:
                if event.kind == "spectrum_program":
                    for card, item in event.params["cards"].items():
                        program = item["program"]
                        programs[f"{event.signal}:{card}"] = {
                            "samples": [
                                hashlib.sha256(
                                    materialize_segment(program, s["id"]).tobytes()
                                ).hexdigest()
                                for s in program["segments"]
                            ],
                            "steps": program["steps"],
                        }
            events = {}
            for label, sequence in (
                ("logical", point.logical_events),
                ("physical", point.physical_events),
            ):
                events[label] = [
                    clean(
                        {
                            "kind": event.kind,
                            "signal": event.signal,
                            "start_mu": event.start_mu,
                            "duration_mu": event.duration_mu,
                            "params": event.params,
                        }
                    )
                    for event in sequence
                ]
            points.append(
                {
                    "coordinates": point.coordinates,
                    "duration_mu": point.duration_mu,
                    "origin_mu": point.origin_mu,
                    "frames": compiled.virtual_z_frames(point.index),
                    "programs": programs,
                    **events,
                }
            )
        result[name] = clean(
            {"order": list(compiled.execution_order()), "points": points}
        )
    return result


def compare(expected, actual, path=""):
    if isinstance(expected, dict):
        for key, value in expected.items():
            if key not in actual:
                raise AssertionError(f"Missing semantic field {path}/{key}")
            compare(value, actual[key], f"{path}/{key}")
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            raise AssertionError(f"Changed length at {path}")
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            compare(left, right, f"{path}/{index}")
    elif expected != actual:
        raise AssertionError(f"Changed {path}: {expected!r} -> {actual!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    actual = capture()
    if args.baseline:
        compare(json.loads(args.baseline.read_text(encoding="utf-8")), actual)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(actual, indent=2), encoding="utf-8")
    print(f"{len(actual)} representative experiments: {args.output}")


if __name__ == "__main__":
    main()
