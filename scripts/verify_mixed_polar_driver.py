"""Audit mixed DDS exports with real driver bodies and virtual RTIO only.

Reuses the POWASF tracing executor and each path's independent comparisons.
No device communication, hardware input or real kernel execution is permitted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(filename)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


polar = _load("verify_polar_driver.py", "_mixed_polar_trace")
ram = _load("verify_ram_driver.py", "_mixed_asf_trace")
common = polar.common
_compare_polar = polar.compare_trace


def _point(point, names):
    return {
        **point,
        "physical_events": [
            e for e in point["physical_events"] if e["signal"] in names
        ],
    }


def _writes(writes, names):
    result = []
    for write in writes:
        if write["device"] not in names:
            continue
        values = write["values"]
        result.append(
            {
                **write,
                **(
                    {"value": values[0]}
                    if len(values) == 1
                    else {"hi": values[0], "lo": values[1]}
                ),
            }
        )
    return result


def compare_trace(point, writes, edges, records, selections, banks, targets, origin):
    """Verify each DDS mode, then retain one globally ordered RTIO trace."""
    events = point["physical_events"]
    polar_names = {e["signal"] for e in events if "polar_bank" in e["params"]}
    square_names = {e["signal"] for e in events if e["kind"] == "dds_prepare"}
    asf_names = {e["signal"] for e in events if e["kind"] == "dds_ram_start"}
    ttl_names = {name for name, item in targets.items() if "output" in item}
    if not polar_names or not square_names | asf_names:
        raise ValueError("Mixed audit requires POWASF and another DDS path")
    if polar_names & (square_names | asf_names) or square_names & asf_names:
        raise ValueError("Mixed audit cannot switch a single DDS playback mode")
    if set(targets) != polar_names | square_names | asf_names | ttl_names:
        raise ValueError("Mixed trace contains an unclassified output")
    polar_targets = polar_names | ttl_names
    result = _compare_polar(
        _point(point, polar_targets),
        [w for w in writes if w["device"] in polar_names],
        edges,
        [r for r in records if r["device"] in polar_names],
        [s for s in selections if s["device"] in polar_names],
        {key: b for key, b in banks.items() if key[0] in polar_names},
        {name: item for name, item in targets.items() if name in polar_targets},
        origin,
    )
    if any(
        s["device"] not in polar_names and s["start_mu"] >= origin for s in selections
    ):
        raise ValueError("Unexpected timed profile selection on nonpolar DDS")

    # The shared tracer captures actual write64 arguments; reconstruct the
    # profile register at each commit from completed writes at that timestamp.
    # Do not use future writes merely because CPU submission rewound at_mu.
    nonpolar = square_names | asf_names
    commits = []
    for record in records:
        if record["device"] not in nonpolar or record["kind"] != "io_update":
            continue
        copied = {**record, "state": dict(record["state"])}
        if record["device"] in square_names:
            profiles = [
                w
                for w in writes
                if w["device"] == record["device"]
                and w["address"] == 0x15
                and len(w["values"]) == 2
                and w["end_mu"] <= record["timestamp_mu"]
            ]
            if profiles:
                latest = max(profiles, key=lambda w: w["end_mu"])
                copied["state"]["21"] = dict(zip(("hi", "lo"), latest["values"]))
        commits.append(copied)

    if square_names:
        subset = _point(point, square_names)
        transaction = _writes(writes, square_names)
        if any(
            w["start_mu"] >= origin and (w["address"] != 0x15 or len(w["values"]) != 2)
            for w in transaction
        ):
            raise ValueError("Unexpected timed register write on Square DDS")
        result["square"] = common.compare_trace(
            subset,
            transaction,
            edges,
            {n: t for n, t in targets.items() if n in square_names},
            origin=origin,
        )
        for commit in commits:
            if commit["device"] not in square_names or commit["timestamp_mu"] < origin:
                continue
            if commit["profile"] != 7:
                raise ValueError(
                    "Mixed Square selected a different single-tone profile"
                )
            state = commit["state"]
            cfr1, cfr2 = state.get("0", 0), state.get("1", 0)
            if cfr1 & ((1 << 31) | (1 << 13) | (1 << 11)):
                raise ValueError("Mixed Square commit changed mode or cleared NCO")
            if not cfr2 & (1 << 7) or not cfr2 & (1 << 24):
                raise ValueError("Mixed Square commit lacks matched profile amplitude")
        result["square"]["continuous_epochs"] = common.validate_nco_epochs(
            subset, [r for r in commits if r["device"] in square_names], origin
        )
    if asf_names:
        subset = _point(point, asf_names)
        transaction = _writes(writes, asf_names)
        if any(
            w["start_mu"] >= origin and (w["address"] != 8 or len(w["values"]) != 1)
            for w in transaction
        ):
            raise ValueError("Unexpected timed register write on ASF RAM DDS")
        selected_commits = [r for r in commits if r["device"] in asf_names]
        result["asf"] = ram.compare_trace(
            subset,
            transaction,
            edges,
            selected_commits,
            {n: t for n, t in targets.items() if n in asf_names},
            {key: bank for key, bank in banks.items() if key[0] in asf_names},
            origin=origin,
        )
        result["asf"]["continuous_epochs"] = common.validate_nco_epochs(
            subset, selected_commits, origin
        )
    result["continuous_epochs"] += len(nonpolar)
    result["paths"] = {
        "powasf": sorted(polar_names),
        "square": sorted(square_names),
        "asf": sorted(asf_names),
    }
    return result


def verify_driver(export, *, coarse_mu=8):
    # Compose the existing, hardware-forbidden tracing engine with comparisons
    # for the mixed plan. Restore its hook even when validation raises.
    previous = polar.compare_trace
    polar.compare_trace = compare_trace
    try:
        report = polar.verify_driver(export, coarse_mu=coarse_mu)
    finally:
        polar.compare_trace = previous
    report["validation"] = "real_mixed_dds_driver_python_trace"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--coarse-mu", type=int, default=8)
    args = parser.parse_args()
    report = verify_driver(args.export, coarse_mu=args.coarse_mu)
    path = args.output or args.export / "real_mixed_driver_trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(path),
                "error": report.get("error"),
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
