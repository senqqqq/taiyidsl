"""Capture compiled notebook results and compare explicitly named experiments.

The notebook executor calls ``capture`` inside its existing kernel after the
lesson finishes. This module is verification tooling, not a tutorial dependency.
Only authoring locations, logical node IDs and operation-name breadcrumbs are
ignored during comparison; region IDs, sample offsets, phase, native source
hashes and DAC hashes remain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 1
NORMALIZATION_VERSION = 2
PROVENANCE_KEYS = frozenset(
    {"source", "source_path", "logical_id", "logical_ids", "operation_path"}
)
USER_DATA_KEYS = frozenset(
    {
        "metadata",
        "parameters",
        "qubits",
        "setup",
        "signal_map",
        "build_kwargs",
        "args",
        "kwargs",
    }
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalize(value, *, user_data=False):
    """Ignore compiler provenance, without editing arbitrary user metadata.

    The scheduler appends ``operation_path`` after scheduling an operation's
    children; lowering copies these names into waveform recipes for reporting.
    Pulse parameters, frame state, placement and waveform hashes are retained.
    """
    if isinstance(value, dict):
        return {
            key: normalize(item, user_data=user_data or key in USER_DATA_KEYS)
            for key, item in value.items()
            if user_data or key not in PROVENANCE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [normalize(item, user_data=user_data) for item in value]
    return value


def snapshot(compiled):
    """Keep compiled physics, configuration and execution contracts explicitly."""
    compiled.assert_integrity()
    data = compiled.to_dict()
    return {
        "axes": data["axes"],
        "shots": data["shots"],
        "averaging_mode": data["averaging_mode"],
        "order": list(compiled.execution_order()),
        "host_execution_plan": data["host_execution_plan"],
        "tick_seconds": compiled.setup.tick_seconds,
        "points": data["points"],
        "setup": data["setup"],
        "signal_map": data["signal_map"],
        "qubits": data["qubits"],
        "parameters": data["parameters"],
        "native_artiq": data["metadata"].get("native_artiq"),
        "transport": data["metadata"].get("transport"),
        "acquisitions": [
            compiled.acquisition_plan(index) for index in range(len(compiled.points))
        ],
    }


def capture(namespace, destination, *, source):
    """Write all directly named compiled globals without executing hardware."""
    from taiyidsl import CompiledExperiment

    experiments = {}
    for label, compiled in tuple(namespace.items()):
        if label.startswith("_") or not isinstance(compiled, CompiledExperiment):
            continue
        semantics = snapshot(compiled)
        experiments[label] = {
            "name": compiled.name,
            "artifact_sha256": compiled.sha256,
            "normalized_sha256": hashlib.sha256(
                canonical(normalize(semantics)).encode("utf-8")
            ).hexdigest(),
            "semantics": semantics,
        }
    if not experiments:
        raise ValueError(
            "No named CompiledExperiment objects found in notebook globals"
        )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "source": str(source),
        "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
        "ignored_authoring_fields": sorted(PROVENANCE_KEYS),
        "scope": (
            "Compiled events/programs, configuration, native calls/source pins, "
            "acquisition plans and execution order; no hardware or ARTIQ compilation. "
            "Experiment names, source IR and general descriptive metadata are not "
            "physical equivalence criteria."
        ),
        "experiments": experiments,
    }
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return {"path": str(destination), "experiments": sorted(experiments)}


def first_difference(left, right, path="$"):
    """Return a compact, exact first mismatch rather than dumping large programs."""
    if type(left) is not type(right):
        return f"{path}: types {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return f"{path}: keys differ {sorted(left.keys() ^ right.keys())}"
        for key in left:
            difference = first_difference(left[key], right[key], f"{path}.{key}")
            if difference:
                return difference
    elif isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: lengths {len(left)} != {len(right)}"
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            difference = first_difference(a, b, f"{path}[{index}]")
            if difference:
                return difference
    elif left != right:
        return f"{path}: {str(left)[:160]!r} != {str(right)[:160]!r}"
    return None


def compare(left, right, pairs):
    """Require every named pair and compare only the documented normalization."""
    for receipt in (left, right):
        if receipt.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported or missing tutorial semantics schema")
    if not pairs:
        raise ValueError(
            "At least one explicit LEFT_LABEL=RIGHT_LABEL pair is required"
        )
    checks = []
    for pair in pairs:
        labels = pair.split("=")
        if len(labels) != 2 or not all(labels):
            raise ValueError(f"Invalid experiment pair: {pair!r}")
        a, b = labels
        if a not in left["experiments"] or b not in right["experiments"]:
            raise ValueError(f"Missing compiled experiment in pair {pair!r}")
        x = normalize(left["experiments"][a]["semantics"])
        y = normalize(right["experiments"][b]["semantics"])
        difference = first_difference(x, y, path=pair)
        checks.append(
            {"pair": pair, "equal": difference is None, "difference": difference}
        )
    return {
        "status": "passed" if all(check["equal"] for check in checks) else "failed",
        "normalization_version": NORMALIZATION_VERSION,
        "capture_normalization_versions": [
            receipt.get("normalization_version", 1) for receipt in (left, right)
        ],
        "ignored_authoring_fields": sorted(PROVENANCE_KEYS),
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--pair", action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare(
        json.loads(args.left.read_text(encoding="utf-8")),
        json.loads(args.right.read_text(encoding="utf-8")),
        args.pair,
    )
    result.update(left=str(args.left), right=str(args.right))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
