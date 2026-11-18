"""Explicit lifecycle labels for native prefixes around a compiled control window."""

from __future__ import annotations

import html
import json
from fractions import Fraction


def native_lifecycle(compiled):
    """Describe opaque native stages without inventing a full shot time axis."""
    contract = getattr(compiled, "metadata", {}).get("native_artiq")
    if not contract:
        return None
    native_tick = Fraction(str(compiled.setup.spectrum.artiq_tick_seconds))
    control_tick = Fraction(str(compiled.setup.tick_seconds))
    package = contract["package"]
    points = []
    for point, calls in zip(compiled.points, contract["points"], strict=True):
        duration = point.duration_mu * control_tick
        cursor = duration
        timed = []
        for call in calls.get("timed", []):
            start = call["start_mu_from_T0"] * native_tick
            stop = start + call["duration_mu"] * native_tick
            timed.append(
                {
                    "method": call.get("component_method", call["method"]),
                    **({"component": call["component"]} if "component" in call else {}),
                    "args": call["args"],
                    "start_s_from_control_T0": float(start),
                    "end_s_from_control_T0": float(stop),
                    "duration_mu": call["duration_mu"],
                    "duration_s": float(stop - start),
                    "waveform": "opaque_not_simulated",
                    "phase_sensitive": call["phase_sensitive"],
                    "end_boundary": call.get("end_boundary"),
                }
            )
        suffix = []
        for call in calls["suffix"]:
            stop = cursor + call["duration_mu"] * native_tick
            suffix.append(
                {
                    "method": call.get("component_method", call["method"]),
                    **({"component": call["component"]} if "component" in call else {}),
                    "args": call["args"],
                    "start_s_from_control_T0": float(cursor),
                    "end_s_from_control_T0": float(stop),
                    "duration_mu": call["duration_mu"],
                    "duration_s": float(stop - cursor),
                    "waveform": "opaque_not_simulated",
                    "phase_sensitive": call["phase_sensitive"],
                }
            )
            cursor = stop
        item = {
            "index": point.index,
            "prefix": [
                {
                    "method": call.get("component_method", call["method"]),
                    **({"component": call["component"]} if "component" in call else {}),
                    "args": call["args"],
                    "duration_s": None,
                    "time_reference": "before_control_epoch_runtime_duration",
                    "waveform": "opaque_not_simulated",
                }
                for call in calls["prefix"]
            ],
            "control_duration_s": float(duration),
            "suffix": suffix,
            "carrier_references": [
                {
                    "group": event.signal,
                    "mode": event.params.get("phase_reference", "sequence"),
                    "start_s_from_control_T0": (
                        0.0
                        if event.params.get("phase_reference", "sequence") == "shot"
                        else float(event.start_mu * control_tick)
                    ),
                }
                for event in point.logical_events
                if event.kind == "spectrum_region"
            ],
        }
        if timed:
            # Use actual owned pulse events, not the Spectrum region boundary:
            # a region may begin with a deliberately requested zero-output delay.
            pulses = [
                event
                for event in point.logical_events
                if event.kind == "play"
                and event.params.get("region_id")
                and event.duration_mu > 0
            ]
            first = min(pulses, key=lambda event: event.start_mu, default=None)
            last = max(
                calls["timed"],
                key=lambda call: call["start_mu_from_T0"] + call["duration_mu"],
            )
            last_end = (last["start_mu_from_T0"] + last["duration_mu"]) * native_tick
            item.update(
                timed=timed,
                first_spectrum_gate=(
                    {
                        "signal": first.signal,
                        "start_s_from_control_T0": float(first.start_mu * control_tick),
                    }
                    if first is not None
                    else None
                ),
                timed_to_first_gate=(
                    {
                        "method": last["method"],
                        "end_boundary": last.get("end_boundary"),
                        "timed_end_s_from_control_T0": float(last_end),
                        "gap_s": float(first.start_mu * control_tick - last_end),
                    }
                    if first is not None
                    else None
                ),
            )
        points.append(item)
    result = {
        "execution": "one_top_level_kernel_per_shot",
        "time_reference": "control_epoch_after_native_prefix",
        "full_shot_duration_s": None,
        "prefix_in_carrier_reference": False,
        "native_waveforms_simulated": False,
        "native_time_unit_s": float(native_tick),
        "initialize_method": package.get("initialize_method"),
        "finalize_method": package.get("finalize_method"),
        "source_audit_status": contract.get("source_audit", {}).get(
            "status", "not_verified"
        ),
        "points": points,
    }
    if any(point.get("timed") for point in points):
        result["timed_in_carrier_reference"] = any(
            reference["mode"] == "shot"
            for point in points
            if point.get("timed")
            for reference in point["carrier_references"]
        )
    if package.get("kind") == "native_program":
        result["components"] = package["components"]
        result["lifecycle"] = package["lifecycle"]
        result["session_finalize_method"] = package["session_finalize_method"]
    return result


def _text(value):
    return html.escape(str(value))


def _call(call):
    args = ", ".join(json.dumps(arg, ensure_ascii=False) for arg in call["args"])
    owner = f"{call['component']}." if "component" in call else ""
    return _text(f"{owner}{call['method']}({args})")


def native_lifecycle_html(compiled, *, program_path=None):
    """Return an independent, escaped panel; ordinary experiments get no panel."""
    lifecycle = native_lifecycle(compiled)
    if lifecycle is None:
        return ""
    has_timed = any(point.get("timed") for point in lifecycle["points"])
    rows = []
    for point in lifecycle["points"]:
        prefix = " → ".join(_call(call) for call in point["prefix"]) or "None"
        duration_us = point["control_duration_s"] * 1e6
        suffix = (
            "<br>".join(
                f"{_call(call)}: {call['start_s_from_control_T0'] * 1e6:.12g}–"
                f"{call['end_s_from_control_T0'] * 1e6:.12g} µs; opaque native output"
                for call in point["suffix"]
            )
            or "None"
        )
        timed_column = ""
        references = (
            "<br>".join(
                f"{_text(reference['group'])}: {_text(reference['mode'])}; "
                f"carrier origin at {reference['start_s_from_control_T0'] * 1e6:.12g} "
                "µs from control T0"
                for reference in point["carrier_references"]
            )
            or "No Spectrum region"
        )
        if has_timed:
            intervals = (
                "<br>".join(
                    f"{_call(call)}: {call['start_s_from_control_T0'] * 1e6:.12g}–"
                    f"{call['end_s_from_control_T0'] * 1e6:.12g} µs; duration "
                    f"{call['duration_s'] * 1e6:.12g} µs; opaque native output"
                    for call in point.get("timed", [])
                )
                or "None"
            )
            gap = point.get("timed_to_first_gate")
            handoff = (
                f"<br>{_text(gap['method'])} end → first Spectrum gate: "
                f"{gap['gap_s'] * 1e6:.12g} µs; first gate at "
                f"{point['first_spectrum_gate']['start_s_from_control_T0'] * 1e6:.12g}"
                " µs from T0"
                if gap is not None
                else "<br>No Spectrum gate in this point"
            )
            if gap is not None and gap.get("end_boundary"):
                handoff += f"<br>End boundary: {_text(gap['end_boundary'])}"
            timed_column = f"<td>{intervals}{handoff}</td>"
        rows.append(
            f"<tr><td>{point['index']}</td><td>{prefix}<br>runtime duration; "
            "before control T0</td>"
            f"{timed_column}<td>0–{duration_us:.12g} µs<br>control duration: "
            f"{duration_us:.12g} µs</td><td>{suffix}</td><td>{references}</td></tr>"
        )
    link = (
        f'<a href="{_text(program_path)}">Top-level ARTIQ kernel source</a> · '
        if program_path
        else ""
    )
    timed_note = (
        " Timed native operations and the requested gap are inside the control "
        "timeline. They contribute to AWG carrier phase only for an explicit "
        "shot reference; the default carrier origin is the triggered-sequence "
        "entry. No recovery boundary is inserted between timed native control, "
        "gates and readout."
        if has_timed
        else ""
    )
    timed_header = "<th>Timed native control from T0</th>" if has_timed else ""
    component_note = ""
    if "components" in lifecycle:
        component_note = (
            "<p>Independent native components: "
            + _text(", ".join(lifecycle["components"]))
            + ". initialize: once per session; after_shot: after every shot; "
            "finalize: once at session end. Components and initialization state "
            "are retained across chunk bindings.</p>"
        )
    return (
        '<section class="native-lifecycle" aria-label="Native kernel lifecycle" '
        'style="margin:18px 0;padding:16px 20px;border:1px solid #c9d7d3;'
        'border-left:4px solid #087f78;background:#f4f8f5;line-height:1.6">'
        "<strong>Native kernel lifecycle · control epoch T0</strong>"
        "<p>One top-level kernel per shot: native prefix → control epoch → "
        "compiled control → native readout → after-shot cleanup / final wait. "
        "Full shot duration is unknown. Native prefix time is excluded from "
        "the carrier phase reference; native waveforms are opaque and are not "
        "simulated. The plots below show the compiled control window only."
        + timed_note
        + "</p>"
        + component_note
        + '<div style="overflow:auto;max-height:300px"><table '
        'style="width:100%;text-align:left;border-collapse:collapse">'
        "<thead><tr><th>Point</th><th>Native prefix</th>"
        + timed_header
        + "<th>Compiled control from T0</th><th>Native suffix from T0</th>"
        "<th>AWG carrier reference</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div><p>Initialization: "
        + _text(lifecycle["initialize_method"] or "none")
        + ("; after_shot: " if "components" in lifecycle else "; per-shot finalize: ")
        + _text(lifecycle["finalize_method"] or "none")
        + (
            "; session finalize: " + _text(lifecycle["session_finalize_method"])
            if "components" in lifecycle
            else ""
        )
        + " (outside the strict control window; duration not assumed).<br>"
        + link
        + "Source boundary audit: "
        + _text(lifecycle["source_audit_status"])
        + ". Driver timing and physical outputs require separate verification."
        + "</p></section>"
    )
