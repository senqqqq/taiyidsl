"""Reviewable, offline hybrid experiment reports made from compiled artifacts.

The report never opens a device. Packed files are materialized one segment at a
time, so a long zero loop is not expanded into a wall-clock-sized waveform.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import html
import json
import math
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlencode

from taiyidsl.backends.artiq_spectrum.awg import materialize_segment

from .resources import asset_text
from .spectrum_report import card_execution, execution_html, export_execution_sources


def _json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _text(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value))


def _table(rows):
    if not rows:
        return '<p class="muted">None</p>'
    headings = "".join(f"<th>{_text(k)}</th>" for k in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{_text(v)}</td>" for v in row.values()) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{headings}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _initial_phase(pulse):
    """Total phase at sample zero, including local modulation and phase steps."""
    modulation = pulse.get("phase_modulation")
    local = (
        modulation["coefficients"][0]
        if modulation and modulation["kind"] == "polynomial"
        else 0.0
    )
    return (
        pulse["phase_rad"]
        + pulse.get("phase_scale", 1) * local
        + sum(s["delta_rad"] for s in pulse.get("phase_steps", []) if s["sample"] == 0)
    )


def _decimal_seconds(value):
    return str(Decimal(value.numerator) / Decimal(value.denominator))


def _card_report(item, group, directory):
    program, contract = item["program"], item["contract"]
    directory.mkdir(parents=True, exist_ok=True)
    if program.get("kind") == "dds":
        from .spectrum_dds_report import export_dds_card

        return export_dds_card(item, directory)
    period = Fraction(1, contract["sample_rate_hz"])
    sample_zero = (
        group["trigger_relative_mu"] * Fraction(str(group["trigger_time_unit_s"]))
        + contract["trigger_latency_samples"] * period
    )
    segments = []
    for segment in program["segments"]:
        packed = materialize_segment(program, segment["id"]).tobytes()
        digest = hashlib.sha256(packed).hexdigest()
        if digest != segment["sha256"]:
            raise ValueError("Materialized segment differs from compiled content hash")
        (directory / f"segment_{segment['id']}.bin").write_bytes(packed)
        segments.append(
            {
                "segment": segment["id"],
                "samples_per_channel": segment["sample_count"],
                "duration_us": float(segment["sample_count"] * period * 10**6),
                "bytes": len(packed),
                "content": segment["recipe"]["kind"],
                "sha256": digest,
            }
        )
    lengths = {s["segment"]: s["samples_per_channel"] for s in segments}
    steps, cursor = [], 0
    for step in program["steps"]:
        stop = cursor + lengths[step["segment_id"]] * step["loops"]
        steps.append(
            {
                "step": step["index"],
                "segment": step["segment_id"],
                "loops": step["loops"],
                "start_sample": cursor,
                "stop_sample": stop,
                "start_us_T0": float((sample_zero + cursor * period) * 10**6),
                "stop_us_T0": float((sample_zero + stop * period) * 10**6),
                "next": "END" if step["end"] else step["next_step"],
            }
        )
        cursor = stop
    if cursor != program["physical_duration_samples"]:
        raise ValueError("Step lengths do not reproduce the complete card program")
    pulses = [
        {
            "channel": pulse["channel"],
            "start_sample": pulse["start_sample"],
            "samples": pulse["duration_samples"],
            "start_s_exact": _decimal_seconds(
                sample_zero + pulse["start_sample"] * period
            ),
            "end_s_exact": _decimal_seconds(
                sample_zero
                + (pulse["start_sample"] + pulse["duration_samples"]) * period
            ),
            "start_us_T0": float(
                (sample_zero + pulse["start_sample"] * period) * 10**6
            ),
            "end_us_T0": float(
                (
                    sample_zero
                    + (pulse["start_sample"] + pulse["duration_samples"]) * period
                )
                * 10**6
            ),
            "frequency_Hz": pulse["frequency_hz"],
            "amplitude": pulse["amplitude"],
            "start_phase_deg": math.degrees(_initial_phase(pulse)) % 360,
            "envelope": pulse["envelope"],
            "local_phase_modulation": pulse.get("phase_modulation"),
            "phase_evolution": pulse.get("phase_evolution", {}),
            "carrier_reference": pulse.get("phase_evolution", {}).get(
                "carrier_reference_kind"
            ),
            "carrier_reference_us_T0": (
                float(pulse["phase_evolution"]["carrier_reference_time_s"]) * 1e6
                if "carrier_reference_time_s" in pulse.get("phase_evolution", {})
                else None
            ),
            "operation_path": pulse.get("operation_path", []),
            "section_schedule": pulse.get("section_schedule", []),
        }
        for pulse in program["pulses"]
    ]
    _csv(directory / "segments.csv", segments)
    _csv(directory / "steps.csv", steps)
    _csv(directory / "pulses.csv", pulses)
    execution = card_execution(item)
    (directory / "sdk_calls.txt").write_text(
        execution["upload_listing"], encoding="utf-8"
    )
    return {
        "active_channels": contract["active_channels"],
        "sample_rate_hz": contract["sample_rate_hz"],
        "sample_zero_us_T0": float(sample_zero * 10**6),
        "playback_timing": {
            "logical_samples_including_preroll": program["logical_duration_samples"],
            "physical_samples": program["physical_duration_samples"],
            "terminal_zero_samples": program["tail_samples"],
            "terminal_zero_duration_ns": float(
                program["tail_samples"] * period * 10**9
            ),
            "physical_end_us_T0": float(
                (sample_zero + program["physical_duration_samples"] * period) * 10**6
            ),
            "segment_alignment_samples": 32,
            "minimum_segment_samples": program["card_spec"]["min_segment_samples"],
        },
        "calibration_sha256": program.get("calibration_sha256"),
        "segments": segments,
        "steps": steps,
        "pulses": pulses,
        "memory": program["memory"],
        "spectrum_execution": execution,
    }


def _point_report(compiled, point, manifest_point, output):
    directory = output / f"point_{point.index}"
    directory.mkdir(exist_ok=True)
    tick = Fraction(str(compiled.setup.tick_seconds))
    timing = [
        {
            "operation": e.kind,
            "device": e.signal,
            "start_ns_T0": float((e.start_mu - point.origin_mu) * tick * 10**9),
            "duration_ns": float(e.duration_mu * tick * 10**9),
        }
        for e in point.physical_events
        if e.kind != "spectrum_program"
    ]
    frames = [
        {
            "operation": e.kind,
            "frame": e.signal,
            "time_us_T0": float(e.start_mu * tick * 10**6),
            "parameters": e.params,
        }
        for e in point.logical_events
        if e.kind in ("rz", "shift_phase", "reset_frame", "reset_phase")
    ]
    groups = []
    for index, group in enumerate(manifest_point["groups"]):
        region = next(
            e
            for e in point.logical_events
            if e.kind == "spectrum_region"
            and e.params["region_id"] == group["region_id"]
        )
        # Numeric folders avoid treating user-controlled hardware names as paths.
        cards = {}
        for card_index, (name, item) in enumerate(group["cards"].items()):
            folder = directory / f"group_{index}" / f"card_{card_index}"
            cards[name] = {
                **_card_report(item, group, folder),
                "directory": folder.relative_to(output).as_posix(),
            }
        groups.append(
            {
                "name": group["name"],
                "phase_reference": group["phase_reference"],
                "carrier_reference_us_T0": float(
                    Fraction(
                        str(group["phase_reference"].get("reference_start_ticks", 0))
                    )
                    * Fraction(str(group["phase_reference"]["tick_seconds"]))
                    * 10**6
                ),
                "terminal_frames": group.get("terminal_frames", {}),
                "entry_frames": group.get("entry_frames", {}),
                "calibration_revision": group["calibration"].get("revision"),
                "trigger_device": group["trigger_device"],
                "trigger_devices": group.get("trigger_devices", {}),
                "trigger_routes": group.get("trigger_routes", []),
                "trigger_us_T0": float(
                    group["trigger_relative_mu"]
                    * Fraction(str(group["trigger_time_unit_s"]))
                    * 10**6
                ),
                "cards": cards,
                "logical_gate_end_us_T0": float(region.end_mu * tick * 10**6),
            }
        )
    _csv(directory / "artiq_timing.csv", timing)
    _json(directory / "frame_events.json", frames)
    moves = [
        {
            "start_us": e.start_mu * float(tick) * 1e6,
            "duration_us": e.duration_mu * float(tick) * 1e6,
            **e.params,
        }
        for e in point.logical_events
        if e.kind == "move"
    ]
    if moves:
        from .spectrum_dds_report import export_transport_paths

        _json(directory / "transport.json", moves)
        export_transport_paths(moves, directory)
    return {
        "index": point.index,
        "coordinates": point.coordinates,
        "trigger_count": len(groups),
        "trigger_route_count": sum(len(g["trigger_routes"]) or 1 for g in groups),
        "terminal_frame_rad": compiled.frame_phases(point.index),
        "terminal_virtual_z_rad": compiled.virtual_z_frames(point.index),
        "artiq_timing": timing,
        "frame_events": frames,
        **({"transport": moves} if moves else {}),
        "groups": groups,
        "pulse_overlaps": _pulse_overlaps(groups),
    }


def _pulse_overlaps(groups, limit=1000):
    """Intersect final DAC pulse intervals, with an explicit report size bound."""
    intervals = sorted(
        (
            Fraction(pulse["start_s_exact"]),
            Fraction(pulse["end_s_exact"]),
            f"{group['name']}/{card}/CH{pulse['channel']}/pulse{index}",
        )
        for group in groups
        for card, data in group["cards"].items()
        for index, pulse in enumerate(data.get("pulses", ()))
    )
    active, overlaps = [], []
    for start, end, name in intervals:
        active = [item for item in active if item[1] > start]
        for _, stop, other in active:
            if len(overlaps) == limit:
                return {"intervals": overlaps, "truncated": True, "limit": limit}
            overlaps.append(
                {
                    "left": other,
                    "right": name,
                    "start_us_T0": float(start * 10**6),
                    "end_us_T0": float(min(end, stop) * 10**6),
                }
            )
        active.append((start, end, name))
    return {"intervals": overlaps, "truncated": False, "limit": limit}


def _point_html(point):
    pieces = [
        f'<details class="point" {"open" if point["index"] == 0 else ""}>',
        f"<summary>Point {point['index']} · {_text(point['coordinates'])}",
        f" <span>{point['trigger_count']} synchronous launch, "
        f"{point['trigger_route_count']} TTL pulse(s) / shot</span></summary>",
        '<div class="point-body">',
        f"<p>Terminal frame F [rad]: {_text(point['terminal_frame_rad'])}; "
        "Rz source diagnostic since last reset [rad]: "
        f"{_text(point['terminal_virtual_z_rad'])}</p>",
        "<h3>Zero-duration frame operations</h3>",
        _table(point["frame_events"]),
        "<details><summary>ARTIQ trigger &amp; output timing [ns from T0]</summary>",
        _table(point["artiq_timing"]),
        "</details>",
        "<details><summary>Final DAC pulse overlap intervals</summary>",
        "<p>Intersections of the compiled pulse intervals; "
        "not measured analog timing.</p>",
        _table(point["pulse_overlaps"]["intervals"]),
        (
            "<p>Only the first 1000 intersections are shown.</p>"
            if point["pulse_overlaps"]["truncated"]
            else ""
        ),
        "</details>",
    ]
    if point.get("transport"):
        pieces += [
            "<h3>Planned atom transport</h3>",
            "<p>Each move resolves its source from this compilation's current layout. "
            "Positions are predicted, not measured. "
            "Every shot returns to its initial layout.</p>",
            '<img style="max-width:800px;width:100%" '
            f'src="point_{point["index"]}/transport_paths.svg" '
            'alt="Requested atom paths"/>',
            _table(
                [
                    {
                        "start_us": m["start_us"],
                        "duration_us": m["duration_us"],
                        "qubits": m["qubits"],
                        "from": m["initial_layout"],
                        "to": m["final_layout"],
                    }
                    for m in point["transport"]
                ]
            ),
            f'<a href="point_{point["index"]}/transport.json">'
            "Transport paths and tone knots</a>",
        ]
    if not point["groups"]:
        pieces.append(
            '<p class="note">No active AWG program: no upload or trigger. '
            "Logical delays and terminal frame still belong to this shot.</p>"
        )
    for group in point["groups"]:
        pieces += [
            f"<h3>Group {_text(group['name'])}</h3>",
            f"<p>Logical gate sequence ends at {group['logical_gate_end_us_T0']:.12g} "
            "µs from T0. All cards use this common logical timeline. "
            "AWG segment boundaries do not add delays between gates; zero padding "
            "only extends physical playback after the sequence. Independent "
            "readout may start at the logical end; re-arm/upload waits for card "
            "completion. These are compiled predictions, "
            "not measured synchronization.</p>",
            f"<p>Synchronous trigger routes: "
            f"{_text(group['trigger_devices'] or group['trigger_device'])} at "
            f"{group['trigger_us_T0']:.9g} µs. "
            f"Calibration revision: {_text(group['calibration_revision'])}</p>",
            f"<pre>{_text(group['phase_reference'])}</pre>",
            f"<p>AWG carrier reference = {group['carrier_reference_us_T0']:.12g} µs "
            "from timeline T0. Per-pulse references below also show reset_phase.</p>",
            f"<p>Entry / terminal frame F and source diagnostics: "
            f"{_text(group['entry_frames'])} → {_text(group['terminal_frames'])}. "
            "frame_phase is the complete drive-frame offset; virtual_z is "
            "diagnostic and must not be subtracted from it again. "
            "hardware_phase is a derived compatibility diagnostic.</p>",
        ]
        for name, card in group["cards"].items():
            folder = card["directory"]
            if card.get("mode") == "dds":
                pieces += [
                    f"<h4>Card {_text(name)} · DDS20 trap hold</h4>",
                    "<p>These curves show compiled DDS commands. The card holds the "
                    "endpoint until its next command or until the session closes. "
                    "Qubit phase frames evolve separately. RF samples and measured "
                    "atom positions require hardware acquisition.</p>",
                    _table(card["spectrum_execution"]["configuration"]),
                    f'<img style="width:100%" src="{folder}/frequency.svg" '
                    'alt="Planned DDS frequency"/>',
                    f'<img style="width:100%" src="{folder}/amplitude.svg" '
                    'alt="Planned DDS amplitude"/>',
                    f'<p><a href="{folder}/dds_program.json">DDS command program</a> · '
                    f'<a href="{folder}/dds_trajectory.csv">Trajectory CSV</a></p>',
                    "<details><summary>DDS command frames and quantization</summary>",
                    _table(card["frames"]),
                    f"<pre>{_text(card['quantization'])}</pre>",
                    "</details>",
                ]
                continue
            pieces += [
                f"<h4>Card {_text(name)} · channels "
                f"{_text(card['active_channels'])}</h4>",
                f"<p>Sample 0 = {card['sample_zero_us_T0']:.9g} µs from T0; "
                f"{card['sample_rate_hz'] / 1e9:g} GS/s. "
                f"Memory: {_text(card['memory'])}</p>",
                _table([card["playback_timing"]]),
                "<details><summary>Clock / trigger / output configuration · "
                "values and sources</summary>",
                _table(card["spectrum_execution"]["configuration"]),
                "</details>",
                "<details><summary>Resolved Spectrum calls for waveform uploads and "
                "step writes</summary><p>This listing decodes the point in isolation. "
                "For schema 2, the chunk/bank page shows the resident segment slots, "
                "complete step table and entry indices. The adapter source above "
                "shows the execution checks and call order.</p>",
                _table(card["spectrum_execution"]["uploads"]),
                f'<a href="{folder}/sdk_calls.txt">Download SDK call listing</a>',
                '<pre class="source"><code>'
                + html.escape(card["spectrum_execution"]["upload_listing"])
                + "</code></pre></details>",
                "<p>RF pulse intervals and initial carrier phase:</p>",
                _table(
                    [
                        {k: v for k, v in p.items() if not k.endswith("_s_exact")}
                        for p in card["pulses"]
                    ]
                ),
            ]
            for i, pulse in enumerate(card["pulses"]):
                query = urlencode(
                    {
                        "point": point["index"],
                        "group": group["name"],
                        "card": name,
                        "channel": pulse["channel"],
                        "t0": pulse["start_s_exact"],
                        "t1": pulse["end_s_exact"],
                    }
                )
                pieces.append(
                    f'<a class="pill" href="waveform.html?{html.escape(query)}">'
                    f"Pulse {i + 1} · CH{pulse['channel']} waveform ↗</a>"
                )
            pieces += [
                "<details open><summary>Sequence step table</summary>",
                _table(card["steps"]),
                f'<a href="{folder}/steps.csv">Download steps.csv</a></details>',
                "<details><summary>Stored segments · "
                "packed int16, channel interleaved</summary>",
                _table(card["segments"]),
                f'<a href="{folder}/segments.csv">Download segments.csv</a> · ',
                " · ".join(
                    f'<a href="{folder}/segment_{s["segment"]}.bin">'
                    f"S{s['segment']}.bin</a>"
                    for s in card["segments"]
                ),
                "</details>",
            ]
    return "\n".join(pieces + ["</div></details>"])


def _bank_html(bank):
    pieces = [
        f"<h2>Resident bank {bank['index']}</h2>",
        f"<p>Points: {_text(bank['points'])}; SHA-256: {_text(bank['sha256'])}</p>",
        "<p>The schema 2 plan gives the shared segment slots in upload order "
        "and the complete step table. During execution, the runner records "
        "uploaded, entry_step and bank_sha256 in its receipt. "
        "This offline report contains the plan only.</p>",
    ]
    for card in bank["cards"]:
        folder = card["directory"]
        if card.get("mode") == "dds":
            pieces += [
                f"<h3>{_text(card['group'])} / {_text(card['card'])} "
                "· DDS commands</h3>",
                "<p>Each shot appends one command packet while the previous hold "
                "continues; no AWG bank upload.</p>",
                _table(card["entries"]),
                f'<a href="{folder}/bank.json">DDS point programs</a> · '
                f'<a href="{folder}/sdk_calls.txt">Compiled command parameters</a>',
            ]
            continue
        pieces += [
            f"<h3>{_text(card['group'])} / {_text(card['card'])}</h3>",
            f"<p>TTL: {_text(card['trigger_device'])}; "
            f"memory: {_text(card['memory'])}</p>",
            "<h4>Finite point entries</h4>",
            _table(card["entries"]),
            "<details><summary>Actual bank slots and steps</summary>",
            _table(card["execution"]["uploads"]),
            _table(card["execution"]["step_writes"]),
            f'<a href="{folder}/sdk_calls.txt">Resolved bank upload arguments</a> · '
            f'<a href="{folder}/bank.json">Bank inspection JSON</a>',
            "<p>Final packed DAC segments: "
            + " · ".join(
                f'<a href="{folder}/segment_{s["segment_id"]}.bin">'
                f"S{s['segment_id']}</a>"
                for s in card["execution"]["uploads"]
            )
            + "</p></details>",
        ]
    return "\n".join(pieces)


def _render_report(replacements):
    # Substitute once; never interpret placeholder-like text from user source.
    return re.sub(
        r"__(TITLE|POINT_COUNT|SHOTS|MODE|SOURCE|EXECUTION|POINTS|HASH|NATIVE_LIFECYCLE|PHASE_REFERENCE)__",
        lambda match: replacements[match.group(1)],
        _HTML,
    )


def _report_data(compiled, manifest, points, banks, execution, source_bytes):
    """Assemble review metadata without writing files or rendering HTML."""
    report = {
        "name": compiled.name,
        "compiled_sha256": manifest["compiled_sha256"],
        "source_sha256": (
            hashlib.sha256(source_bytes).hexdigest()
            if source_bytes is not None
            else None
        ),
        "shots_per_point": compiled.shots,
        "averaging_mode": compiled.averaging_mode,
        "phase_convention": "theta = Phi_osc + s*(F + phi_pulse)",
        "shot_initial_frame": {
            "F": 0,
            "carrier_epoch": "per_triggered_sequence",
            "timeline_reference": "shot_logical_T0",
        },
        "hardware_status": "not_verified",
        "execution": execution,
        "host_execution_plan": manifest.get("host_execution_plan", {}),
        "banks": banks,
        "phase_modes": {
            "frame": "F = accumulated shift_phase - Rz since last reset",
            "virtual_z": "source diagnostic since reset; already included in F",
            "oscillator": "Phi_osc = C + M + offset + calibration",
            "pulse": "phi_pulse = base + local_modulation",
            "default_reference": "triggered_sequence entry",
            "shot_reference": "explicit phase_reference='shot'",
            "reset_frame": "clear F only",
            "reset_phase": "clear F and inherited modulation; restart carrier origin",
        },
        "points": points,
    }
    from .native_view import native_lifecycle

    lifecycle = native_lifecycle(compiled)
    if lifecycle:
        report["shot_initial_frame"]["timeline_reference"] = lifecycle["time_reference"]
        report["native_lifecycle"] = lifecycle
        report["native_contract"] = copy.deepcopy(compiled.metadata["native_artiq"])
        report["native_composition"] = manifest.get("native_artiq")
    return report


def export_experiment_report(compiled, output, *, source_path=None):
    """Export an offline hybrid bundle and return its review entry points.

    ``source_path`` is optional and is copied verbatim into ``dsl_source.py``.
    Numeric point/group/card directories contain the final packed segments and
    decoded CSV tables. Viewers are self-contained HTML and need no instrument,
    server or SDK. The original ARTIQ export remains under ``export/artiq``.
    """
    from .native_view import native_lifecycle_html
    from .output_simulator import write_output_simulator
    from .spectrum_bank_report import export_bank_reports
    from .visualization import visualize

    compiled.assert_integrity()
    if compiled.provenance.get("backend", {}).get("name") != "artiq_spectrum":
        raise ValueError("Experiment reports currently require artiq_spectrum")
    output = Path(output)
    source_bytes = Path(source_path).read_bytes() if source_path is not None else None
    source = source_bytes.decode("utf-8") if source_bytes is not None else None
    output.mkdir(parents=True, exist_ok=True)
    exported = compiled.export(output / "export")
    manifest = json.loads(exported["manifest"].read_text(encoding="utf-8"))
    execution = export_execution_sources(output)
    points = [
        _point_report(compiled, point, manifest_point, output)
        for point, manifest_point in zip(
            compiled.points, manifest["points"], strict=True
        )
    ]
    banks = export_bank_reports(manifest, output)
    report = _report_data(compiled, manifest, points, banks, execution, source_bytes)
    lifecycle = report.get("native_lifecycle")
    _json(output / "report.json", report)
    visualize(compiled, output / "pulse_sheet.html", waveform_path="waveform.html")
    write_output_simulator(compiled, output / "waveform.html")
    if source is not None:
        (output / "dsl_source.py").write_bytes(source_bytes)
    source_html = (
        '<a href="dsl_source.py">Download complete source</a>'
        f'<pre class="source"><code>{html.escape(source)}</code></pre>'
        if source is not None
        else "<p>No authoring source supplied. The complete compiled IR is in "
        "export/compiled.json.</p>"
    )
    replacements = {
        "TITLE": _text(compiled.name),
        "POINT_COUNT": str(len(points)),
        "SHOTS": str(compiled.shots),
        "MODE": _text(compiled.averaging_mode),
        "SOURCE": source_html,
        "EXECUTION": execution_html(execution, output),
        "POINTS": "\n".join(map(_point_html, points[:20])),
        "HASH": manifest["compiled_sha256"],
        "NATIVE_LIFECYCLE": native_lifecycle_html(
            compiled, program_path="export/artiq/experiment.py"
        ),
        "PHASE_REFERENCE": (
            "Each shot starts its control timeline at T0 with F = 0 after the native "
            "MOT/cooling prefix. The carrier reference excludes the native prefix. "
            "The timed preparation tail and subsequent delays occupy the control "
            "timeline. By default the AWG carrier starts at triggered_sequence entry. "
            "With phase_reference='shot', the carrier evolves from control T0 through "
            "preparation and delays. "
            if lifecycle
            else "Each shot starts with F = 0 and the card replays the point entry. "
            "By default the AWG carrier starts at triggered_sequence entry. Set "
            "phase_reference='shot' to use the shared logical T0. "
        ),
    }
    pages = []
    for bank in banks:
        for offset in range(0, len(bank["points"]), 20):
            indices = bank["points"][offset : offset + 20]
            name = f"chunk_{bank['index']}_page_{offset // 20}.html"
            body = _bank_html(bank) + "\n".join(_point_html(points[i]) for i in indices)
            page = {
                **replacements,
                "TITLE": f"{_text(compiled.name)} · chunk {bank['index']}",
                "POINT_COUNT": str(len(indices)),
                "SOURCE": '<a href="index.html">Complete source in overview</a>',
                "EXECUTION": (
                    '<p><a href="index.html#execution">'
                    "Runtime source and overview</a></p>"
                ),
                "POINTS": body,
            }
            (output / name).write_text(_render_report(page), encoding="utf-8")
            pages.append(
                f'<a class="pill" href="{name}">Chunk {bank["index"]} · '
                f"points {indices[0]}–{indices[-1]}</a>"
            )
    if pages:
        replacements["POINTS"] = (
            "<h3>Chunk / bank pages</h3><p>Each page lists the upload parameters "
            "for a resident bank and covers up to 20 points. The overview includes "
            "the first 20 points.</p>" + "\n".join(pages) + replacements["POINTS"]
        )
    document = _render_report(replacements)
    (output / "index.html").write_text(document, encoding="utf-8")
    return {
        "index": output / "index.html",
        "report": output / "report.json",
        "pulse_sheet": output / "pulse_sheet.html",
        "waveform": output / "waveform.html",
        "export": exported,
    }


_HTML = asset_text("experiment_report.html")
