"""Complete event fingerprints across the OpticsConfig simplification.

Full fingerprints omit only source locations and structural logical IDs. The
historical logical comparison additionally projects newer explanatory metadata
back to the frozen schema. Zero-duration logical events remain significant;
initialization is independently verified and included in the comparison.
"""

import hashlib
import json

from taiyidsl.artiq import _verify_source, generate_source, verify_equivalence


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
        "resources": list(event.resources),
    }


def _logical_baseline_value(event):
    """Project only post-baseline provenance and redundant phase inputs.

    Effective ``phase``, Rz ``angle``, timestamps, durations, resources and all
    pulse parameters remain checked. Raw play phase and shift/reset increments
    were added for resolving frames after scheduling; their effective result is
    already present in the retained phase field. A reset's exact zero used to
    serialize as int 0, whereas the final frame resolver emits float 0.0.
    The unified resolver also reports zero Rz diagnostics on reset events;
    omit those exact zeros only, retaining any unexpected nonzero diagnostic.
    """
    omitted = {"frame_phase", "previous_phase", "initial_states", "operation_path"}
    if event.kind == "play":
        omitted.add("requested_phase")
    if event.kind in {"shift_phase", "reset_frame"}:
        omitted.add("angle")
    if event.kind == "reset_frame":
        for key in ("accumulated_virtual_z", "drive_phase_offset"):
            if event.params.get(key) == 0:
                omitted.add(key)
    value = event_value(event)
    value["params"] = {
        key: item for key, item in event.params.items() if key not in omitted
    }
    if event.kind == "reset_frame" and value["params"].get("phase") == 0:
        value["params"]["phase"] = 0
    return value


def fingerprint(compiled):
    report, traces = _verify_source(compiled, generate_source(compiled))
    return {
        "shots": compiled.shots,
        "averaging_mode": compiled.averaging_mode,
        "axes": compiled.axes,
        "host_order": report["host_order"],
        "initialization": report["initialization"],
        "points": [
            {
                "index": point.index,
                "coordinates": point.coordinates,
                "axis_indices": list(point.axis_indices),
                "duration_mu": point.duration_mu,
                "origin_mu": point.origin_mu,
                "logical_event_count": len(point.logical_events),
                "physical_event_count": len(point.physical_events),
                "logical_sha256": digest(
                    [event_value(e) for e in point.logical_events]
                ),
                "physical_sha256": digest(
                    [event_value(e) for e in point.physical_events]
                ),
                "executed_sha256": digest(trace),
                "acquisitions": [
                    event_value(e) for e in point.logical_events if e.kind == "measure"
                ],
            }
            for point, trace in zip(compiled.points, traces)
        ],
    }


def assert_logical_baseline(compiled, baseline, *, ignore_zero_delays=False):
    """Keep frozen experiment semantics while independently checking backend 6.

    The new prepare/commit/effect events intentionally replace the old driver
    schedule. Its hashes and pre-roll are therefore not suitable baselines.
    New explanatory metadata and redundant raw frame inputs are projected out;
    effective phase and every timing/resource field remain in the logical digest.
    """
    actual = fingerprint(compiled)
    for key in ("shots", "averaging_mode", "axes", "host_order"):
        assert actual[key] == baseline[key], key
    assert len(actual["points"]) == len(baseline["points"])
    for point, record, expected in zip(
        compiled.points, actual["points"], baseline["points"]
    ):
        for key in ("index", "coordinates", "axis_indices", "duration_mu"):
            assert record[key] == expected[key], (point.index, key)
        if "logical_event_count" in expected:
            assert record["logical_event_count"] == expected["logical_event_count"]
        if "acquisitions" in expected:
            assert [
                _logical_baseline_value(event)
                for event in point.logical_events
                if event.kind == "measure"
            ] == expected["acquisitions"]
        projected = []
        for event in point.logical_events:
            if (
                ignore_zero_delays
                and event.kind in {"delay", "reserve"}
                and event.duration_mu == 0
            ):
                continue
            projected.append(_logical_baseline_value(event))
        assert digest(projected) == expected["logical_sha256"], point.index
    assert verify_equivalence(compiled)["status"] == "passed"
    return actual
