"""Semantic fingerprints: ignore source locations and structural provenance IDs."""

import hashlib
import json

from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import _verify_source


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def event_value(event):
    return {
        "kind": event.kind,
        "signal": event.signal,
        "start_mu": event.start_mu,
        "duration_mu": event.duration_mu,
        "params": event.params,
        "resources": event.resources,
    }


def fingerprint(compiled):
    report, traces = _verify_source(compiled, generate_source(compiled))
    return {
        "shots": compiled.shots,
        "averaging_mode": compiled.averaging_mode,
        "axes": compiled.axes,
        "host_order": report["host_order"],
        "points": [
            {
                "index": point.index,
                "coordinates": point.coordinates,
                "axis_indices": list(point.axis_indices),
                "duration_mu": point.duration_mu,
                "origin_mu": point.origin_mu,
                "logical_sha256": digest(
                    [
                        event_value(e)
                        for e in point.logical_events
                        if not (e.kind in {"delay", "reserve"} and e.duration_mu == 0)
                    ]
                ),
                "physical_sha256": digest(
                    [event_value(e) for e in point.physical_events]
                ),
                "executed_sha256": digest(trace),
            }
            for point, trace in zip(compiled.points, traces)
        ],
    }
