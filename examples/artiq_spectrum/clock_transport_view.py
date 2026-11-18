"""Offline A/B DAC and C native-DDS inspection on one gate-relative time axis.

The DDS curves are register-command predictions, not sampled RF or measurements.
Finite AWG samples are decoded from their actual segment/step programs. Plotting
never changes a compiled artifact or introduces a software DDS phase ledger.
"""

from __future__ import annotations

import csv
import json
from fractions import Fraction
from pathlib import Path

import numpy as np

from taiyidsl.spectrum_waveforms import reconstruct_window

_COLORS = ("#2563a6", "#d97706", "#008779", "#a84872", "#6b52a3")
_ROW_TITLES = (
    "Compiled event timing",
    "A · RF: final DAC min/max overview",
    "B · clock / Rydberg: final DAC min/max overview",
    "C · DDS command frequency (no RF phase reconstruction)",
    "C · DDS trapping amplitude",
    "Planned atom coordinates inferred from the frequency calibration",
)


def _seconds(value):
    return Fraction(str(value))


def _extrema(values, bins):
    """Retain ordered extrema from each contiguous sample bin, including edges."""
    if len(values) <= 2 * bins:
        return np.arange(len(values), dtype=np.int64)
    width = int(np.ceil(len(values) / bins))
    count = len(values) // width
    blocks = values[: count * width].reshape(count, width)
    offsets = np.arange(count) * width
    indices = np.concatenate(
        (offsets + blocks.argmin(axis=1), offsets + blocks.argmax(axis=1))
    )
    if count * width < len(values):
        tail = values[count * width :]
        indices = np.concatenate(
            (indices, [count * width + tail.argmin(), count * width + tail.argmax()])
        )
    return np.unique(np.concatenate(([0], indices, [len(values) - 1])))


def _dds_rows(program, start_us):
    """Include the old ramp endpoint and the next pin at the same timestamp."""
    rows = []
    for index, frame in enumerate(program["frames"]):
        start = start_us + frame["at_mu"] * 0.0002
        delay = frame.get("ramp_update_delay_mu", 0)
        for tone in frame["tones"]:
            base = {
                "frame": index,
                "time_us": start,
                "stage": "frame_start",
                **tone,
                "set_frequency": frame["set_frequency"],
                "set_amplitude": frame["set_amplitude"],
                "ramp_update_delay_mu": delay,
                "trigger_source": frame["trigger_source"],
            }
            rows.append(base)
            if frame["timer_mu"] is None:
                continue
            if delay:
                rows.append(
                    {**base, "time_us": start + delay * 0.0002, "stage": "ramp_start"}
                )
            seconds = (frame["timer_mu"] - delay) * 0.2e-9
            rows.append(
                {
                    **base,
                    "time_us": start + frame["timer_mu"] * 0.0002,
                    "stage": "ramp_end_before_next_commit",
                    "frequency_hz": tone["frequency_hz"]
                    + tone["frequency_slope_hz_per_s"] * seconds,
                    "amplitude": tone["amplitude"]
                    + tone["amplitude_slope_per_s"] * seconds,
                }
            )
    return rows


def build_clock_transport_data(
    compiled, *, point=0, max_bins=6000, hold_margin_us=20.0
):
    """Return bounded, JSON-shaped plot data without importing plotting libraries."""
    compiled.assert_integrity()
    if type(point) is not int or not 0 <= point < len(compiled.points):
        raise ValueError("point must select an existing compiled scan point")
    if type(max_bins) is not int or max_bins < 1 or hold_margin_us <= 0:
        raise ValueError("max_bins and hold_margin_us must be positive")
    selected = compiled.points[point]
    regions = [e for e in selected.logical_events if e.kind == "spectrum_region"]
    programs = [e for e in selected.physical_events if e.kind == "spectrum_program"]
    if len(regions) != 1 or len(programs) != 1:
        raise ValueError("This teaching view requires one Spectrum region per shot")
    region, group = regions[0], programs[0].params
    tick = _seconds(compiled.setup.tick_seconds)
    gate_start = region.start_mu * tick
    trigger = group["trigger_relative_mu"] * _seconds(group["trigger_time_unit_s"])
    trigger_us = float((trigger - gate_start) * 1e6)
    duration_us = float(region.duration_mu * tick * 1e6)
    margin = float(hold_margin_us)
    x_range = [min(0.0, trigger_us) - margin, duration_us + margin]
    awg, dds, dds_rows, card_timing = [], [], [], []
    for name, item in group["cards"].items():
        contract, program = item["contract"], item["program"]
        mode = contract.get("mode", "awg")
        if mode == "dds":
            first = trigger + _seconds(contract["dds_trigger_latency_ns"]) / 10**9
            first_us = float((first - gate_start) * 1e6)
            rows = _dds_rows(program, first_us)
            dds_rows.extend({"card": name, **row} for row in rows)
            for initial in program["initial_tones"]:
                core = initial["core"]
                values = [r for r in rows if r["core"] == core]
                dds.append(
                    {
                        "card": name,
                        "channel": initial["channel"],
                        "core": core,
                        "time_us": [v["time_us"] for v in values],
                        "frequency_hz": [v["frequency_hz"] for v in values],
                        "amplitude": [v["amplitude"] for v in values],
                        "initial_hold": initial,
                        "terminal_hold": next(
                            t for t in program["final_tones"] if t["core"] == core
                        ),
                    }
                )
            card_timing.append(
                {
                    "card": name,
                    "mode": mode,
                    "trigger_us": trigger_us,
                    "first_output_or_frame_us": first_us,
                    "finite_program_end_us": first_us
                    + program["frames"][-1]["at_mu"] * 0.0002,
                    "after_program": "nonzero hold until session close",
                    "command_count": program["command_count"],
                    "resident_command_budget": program["resident_command_count"],
                }
            )
            continue
        rate = contract["sample_rate_hz"]
        first = trigger + Fraction(contract["trigger_latency_samples"], rate)
        first_us = float((first - gate_start) * 1e6)
        count = program["physical_duration_samples"]
        if count > 2_000_000:
            raise ValueError("Use the waveform viewer for AWG programs over 1.6 ms")
        samples = reconstruct_window(program, 0, count, max_samples=2_000_000)
        for column, channel in enumerate(contract["active_channels"]):
            indices = _extrema(samples[:, column], max_bins)
            awg.append(
                {
                    "card": name,
                    "channel": channel,
                    "time_us": (first_us + indices.astype(float) * 1e6 / rate).tolist(),
                    "normalized": (samples[indices, column] / 32767).tolist(),
                    "sample_indices": indices.tolist(),
                    "sample_count": count,
                    "sample_rate_hz": rate,
                    "representation": "ordered_bin_min_max_from_final_int16",
                    "reduced": len(indices) < count,
                }
            )
        card_timing.append(
            {
                "card": name,
                "mode": mode,
                "trigger_us": trigger_us,
                "first_output_or_frame_us": first_us,
                "finite_program_end_us": first_us + count * 1e6 / rate,
                "after_program": "finite zero tail; no extrapolated DAC samples",
            }
        )
    if not dds:
        raise ValueError("The teaching view needs a native DDS transport program")
    timing, moves = [], []
    outputs = compiled.setup.spectrum.groups[group["name"]].outputs
    for event in selected.logical_events:
        if event.params.get("region_id") != region.params["region_id"]:
            continue
        start = float((event.start_mu * tick - gate_start) * 1e6)
        stop = float((event.end_mu * tick - gate_start) * 1e6)
        if event.kind == "play":
            names = sorted(
                {
                    outputs[b.device].card
                    for b in compiled.signal_map.bindings(event.signal)
                }
            )
            timing.append(
                {
                    "kind": "play",
                    "card": "/".join(names),
                    "signal": event.signal,
                    "start_us": start,
                    "end_us": stop,
                    "duration_us": stop - start,
                    "label": event.signal,
                    "targets": event.params.get("operation_qubits", []),
                }
            )
        elif event.kind == "move":
            motion_end = start + event.params["motion_duration_mu"] * float(tick) * 1e6
            moves.append({"start_us": start, "end_us": stop, **event.params})
            timing.append(
                {
                    "kind": "move",
                    "card": event.params["card"],
                    "signal": event.signal,
                    "start_us": start,
                    "end_us": motion_end,
                    "duration_us": motion_end - start,
                    "label": f"move {len(moves)}",
                    "targets": event.params["qubits"],
                }
            )
            if stop > motion_end:
                timing.append(
                    {
                        "kind": "settle",
                        "card": event.params["card"],
                        "signal": event.signal,
                        "start_us": motion_end,
                        "end_us": stop,
                        "duration_us": stop - motion_end,
                        "label": "settle / hold",
                        "targets": event.params["qubits"],
                    }
                )
    spatial = []
    transport = compiled.metadata["transport"]
    for uid, binding in transport["bindings"].items():
        for axis in ("x", "y"):
            tone = next(t for t in dds if t["core"] == binding[f"{axis}_core"])
            lookup = sorted(
                (row[1], row[0]) for row in transport[f"{axis}_calibration"]
            )
            frequency = np.asarray(tone["frequency_hz"])
            values = np.interp(frequency, *np.array(lookup).T)
            # An endpoint rounded by the DDS NCO can sit just outside the
            # requested lookup domain. Preserve this tiny realized displacement
            # using the edge slope, rather than clipping it to the requested site.
            for pair, mask in (
                (lookup[:2], frequency < lookup[0][0]),
                (lookup[-2:], frequency > lookup[-1][0]),
            ):
                (fa, xa), (fb, xb) = pair
                values[mask] = xa + (frequency[mask] - fa) * (xb - xa) / (fb - fa)
            values *= 1e6
            spatial.append(
                {
                    "name": f"{uid} {axis}",
                    "time_us": tone["time_us"],
                    "position_um": values.tolist(),
                }
            )
    return {
        "schema_version": 1,
        "point": point,
        "coordinates": selected.coordinates,
        "common_time_origin": "Spectrum region entry",
        "time_unit": "us",
        "x_range_us": x_range,
        "region_start_s_from_shot_T0": float(gate_start),
        "logical_end_us": duration_us,
        "trigger_us": trigger_us,
        "awg": awg,
        "dds": dds,
        "dds_rows": dds_rows,
        "spatial": spatial,
        "timing": timing,
        "card_timing": card_timing,
        "moves": moves,
        "hold_margin_semantics": (
            "schematic constant extension; not a measured shot gap"
        ),
        "wall_clock_time": False,
        "hardware_verified": False,
        "dds_model": (
            "piecewise linear command prediction; no absolute phase or DAC samples"
        ),
    }


def _plotly_figure(datasets, selected):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=6,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.13, 0.19, 0.19, 0.18, 0.13, 0.18],
        vertical_spacing=0.035,
        subplot_titles=_ROW_TITLES,
    )
    spans = []
    for data in datasets:
        first = len(fig.data)
        visible = data["point"] == selected
        fig.add_trace(
            go.Scatter(
                x=data["x_range_us"],
                y=["C", "C"],
                mode="lines",
                line={"width": 3, "color": "#a8cfca", "dash": "dot"},
                name="C continuously holds outside moves",
                showlegend=False,
                visible=visible,
            ),
            row=1,
            col=1,
        )
        for event in data["timing"]:
            name = event["card"]
            color = _COLORS[{"A": 0, "B": 1, "C": 2}.get(name, 3)]
            if event["kind"] == "settle":
                color = "#a8cfca"
            fig.add_trace(
                go.Scatter(
                    x=[event["start_us"], event["end_us"]],
                    y=[name, name],
                    mode="lines",
                    line={"width": 14, "color": color},
                    name=event["label"],
                    showlegend=False,
                    visible=visible,
                    text=[event["label"]] * 2,
                    hovertemplate="%{text}<br>%{x:.6f} µs<extra>%{y}</extra>",
                ),
                row=1,
                col=1,
            )
        for trace in data["awg"]:
            row = 2 if trace["card"] == "A" else 3
            color = _COLORS[0 if row == 2 else 1 + trace["channel"]]
            fig.add_trace(
                go.Scatter(
                    x=trace["time_us"],
                    y=trace["normalized"],
                    mode="lines",
                    name=f"{trace['card']} CH{trace['channel']} DAC min/max",
                    line={"color": color, "width": 1},
                    visible=visible,
                    hovertemplate="%{x:.6f} µs<br>%{y:.7f} FS"
                    "<extra>%{fullData.name}</extra>",
                ),
                row=row,
                col=1,
            )
        for i, tone in enumerate(data["dds"]):
            for row, key, scale, unit in (
                (4, "frequency_hz", 1e-6, "MHz"),
                (5, "amplitude", 1, "FS"),
            ):
                name = f"C CH{tone['channel']} tone{tone['core']} {unit}"
                fig.add_trace(
                    go.Scatter(
                        x=tone["time_us"],
                        y=[v * scale for v in tone[key]],
                        mode="lines",
                        name=name,
                        line={"color": _COLORS[i % len(_COLORS)]},
                        visible=visible,
                        hovertemplate=f"%{{x:.6f}} µs<br>%{{y:.8g}} {unit}"
                        "<extra>%{fullData.name}</extra>",
                    ),
                    row=row,
                    col=1,
                )
                # Dashed visual margins indicate the constant state, never a
                # fabricated physical duration between the actual host shots.
                for xs, ys in (
                    (
                        [data["x_range_us"][0], tone["time_us"][0]],
                        [tone["initial_hold"][key] * scale] * 2,
                    ),
                    (
                        [tone["time_us"][-1], data["x_range_us"][1]],
                        [tone["terminal_hold"][key] * scale] * 2,
                    ),
                ):
                    fig.add_trace(
                        go.Scatter(
                            x=xs,
                            y=ys,
                            mode="lines",
                            line={"color": _COLORS[i % len(_COLORS)], "dash": "dot"},
                            name="Hold continuation (illustrative margin)",
                            showlegend=False,
                            visible=visible,
                        ),
                        row=row,
                        col=1,
                    )
        for i, trace in enumerate(data["spatial"]):
            fig.add_trace(
                go.Scatter(
                    x=trace["time_us"],
                    y=trace["position_um"],
                    name=trace["name"],
                    line={"color": _COLORS[i % len(_COLORS)]},
                    visible=visible,
                ),
                row=6,
                col=1,
            )
        spans.append((first, len(fig.data)))
    for row, label in enumerate(
        (
            "Card",
            "DAC / FS",
            "DAC / FS",
            "Frequency / MHz",
            "Amplitude / FS",
            "Position / µm",
        ),
        1,
    ):
        fig.update_yaxes(title_text=label, row=row, col=1)
    chosen = next(data for data in datasets if data["point"] == selected)
    fig.update_xaxes(range=chosen["x_range_us"], showgrid=True)
    fig.update_xaxes(
        title_text="Common time from Spectrum sequence entry / µs", row=6, col=1
    )
    fig.update_yaxes(categoryorder="array", categoryarray=["C", "B", "A"], row=1, col=1)
    for time, color, dash in (
        (0, "#475569", "dot"),
        (chosen["trigger_us"], "#9b1c41", "dash"),
        (chosen["logical_end_us"], "#475569", "dot"),
    ):
        fig.add_vline(x=time, line_color=color, line_dash=dash, line_width=1)
    buttons = []
    for data, (first, last) in zip(datasets, spans, strict=True):
        mask = [first <= i < last for i in range(len(fig.data))]
        label = f"Point {data['point']} · {data['coordinates']}"
        buttons.append(
            {"label": label, "method": "update", "args": [{"visible": mask}]}
        )
    fig.update_layout(
        height=1250,
        width=1180,
        template="plotly_white",
        hovermode="x unified",
        title="Clock Ramsey transport · A/B final DAC, C native DDS commands",
        margin={"t": 130, "r": 35, "b": 85, "l": 95},
        legend={"orientation": "h", "y": -0.07, "x": 0, "font": {"size": 10}},
        updatemenus=[
            {
                "buttons": buttons,
                "active": [d["point"] for d in datasets].index(selected),
                "x": 0,
                "y": 1.09,
                "xanchor": "left",
                "yanchor": "top",
            }
        ],
    )
    return fig


def _static_plot(data, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(
        6,
        1,
        sharex=True,
        figsize=(13, 12),
        gridspec_kw={"height_ratios": [0.8, 1.2, 1.2, 1.1, 0.9, 1.1]},
    )
    for ax, title in zip(axes, _ROW_TITLES, strict=True):
        ax.set_title(title, loc="left", fontsize=10)
        ax.grid(alpha=0.18)
        ax.axvline(0, color="#475569", linestyle=":", linewidth=0.8)
        ax.axvline(
            data["logical_end_us"], color="#475569", linestyle=":", linewidth=0.8
        )
        ax.axvline(data["trigger_us"], color="#9b1c41", linestyle="--", linewidth=0.8)
    axes[0].plot(data["x_range_us"], [0, 0], ":", color="#a8cfca", linewidth=3)
    for event in data["timing"]:
        y = {"A": 2, "B": 1, "C": 0}.get(event["card"], 0)
        color = _COLORS[2 - y]
        if event["kind"] == "settle":
            color = "#a8cfca"
        axes[0].plot(
            [event["start_us"], event["end_us"]],
            [y, y],
            linewidth=11,
            color=color,
            solid_capstyle="butt",
        )
        if event["kind"] != "settle":
            axes[0].text(
                (event["start_us"] + event["end_us"]) / 2,
                y + 0.18,
                event["label"],
                ha="center",
                fontsize=8,
            )
    axes[0].set_yticks([0, 1, 2], ["C", "B", "A"])
    axes[0].set_ylim(-0.4, 2.7)
    for trace in data["awg"]:
        index = 1 if trace["card"] == "A" else 2
        axes[index].plot(
            trace["time_us"],
            trace["normalized"],
            linewidth=0.6,
            label=f"{trace['card']} CH{trace['channel']}",
        )
    for i, tone in enumerate(data["dds"]):
        color = _COLORS[i % len(_COLORS)]
        for ax, key, scale in (
            (axes[3], "frequency_hz", 1e-6),
            (axes[4], "amplitude", 1),
        ):
            ax.plot(
                tone["time_us"],
                np.array(tone[key]) * scale,
                color=color,
                label=f"CH{tone['channel']} tone{tone['core']}",
            )
            ax.plot(
                [data["x_range_us"][0], tone["time_us"][0]],
                [tone["initial_hold"][key] * scale] * 2,
                ":",
                color=color,
            )
            ax.plot(
                [tone["time_us"][-1], data["x_range_us"][1]],
                [tone["terminal_hold"][key] * scale] * 2,
                ":",
                color=color,
            )
    for trace in data["spatial"]:
        axes[5].plot(trace["time_us"], trace["position_um"], label=trace["name"])
    for ax, label in zip(
        axes[1:],
        ["DAC / FS", "DAC / FS", "MHz", "Amplitude / FS", "Position / µm"],
        strict=True,
    ):
        ax.set_ylabel(label)
        ax.legend(loc="upper right", fontsize=8, ncol=3)
    axes[-1].set(
        xlabel="Common time from Spectrum sequence entry / µs", xlim=data["x_range_us"]
    )
    fig.suptitle(
        f"Clock Ramsey transport · point {data['point']} · {data['coordinates']}",
        fontsize=13,
    )
    fig.text(
        0.08,
        0.015,
        "A/B: ordered min/max of final DAC samples. "
        "C: command prediction; no absolute RF phase.\n"
        "Dotted hold extensions are illustrative margins; "
        "shot / host gaps have no assigned duration.",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0.045, 1, 0.97), h_pad=0.8)
    fig.savefig(output / "common_timeline.png", dpi=150)
    fig.savefig(output / "common_timeline.svg")
    plt.close(fig)


def export_clock_transport_view(compiled, output_dir, *, point=0):
    """Export an interactive scan selector and a static selected-point overview."""
    import plotly.io as pio

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    selected = build_clock_transport_data(compiled, point=point)
    indices = (
        list(range(len(compiled.points))) if len(compiled.points) <= 16 else [point]
    )
    datasets = [
        selected if i == point else build_clock_transport_data(compiled, point=i)
        for i in indices
    ]
    fig = _plotly_figure(datasets, point)
    html = pio.to_html(
        fig,
        include_plotlyjs=True,
        full_html=True,
        config={"displaylogo": False, "scrollZoom": True},
    )
    notice = (
        '<div style="font:15px system-ui;padding:20px 50px;max-width:1100px">'
        "<b>统一时间轴 / One common time axis.</b> "
        "T=0 is the Spectrum gate-sequence entry; "
        "all cards retain their trigger latency and idle intervals. "
        "The red dashed line is the common trigger. "
        "Zoom or pan any panel to inspect aligned boundaries; "
        "use the point selector to compare RF phases. "
        "<p>A/B show ordered per-bin minima/maxima of the final int16 DAC samples, "
        "not sparse sine synthesis. C shows the native DDS frequency and amplitude "
        "command model, not final RF/DAC samples. Dotted edges extend the known "
        "constant hold state for illustration; they do not assign a duration "
        "to host preparation, shot gaps, or session.run() return. "
        "These outputs are unmeasured.</p>"
        '<p><a href="../report/index.html">Full compiled report</a> · '
        '<a href="../report/waveform.html">Exact A/B DAC viewer</a> · '
        '<a href="../report/pulse_sheet.html">'
        "Full native/control timing</a> · "
        '<a href="common_timeline.json">Plot data / timing contract</a> · '
        '<a href="timing.csv">Event timing CSV</a> · '
        '<a href="dds_commands.csv">DDS command trajectory CSV</a></p></div>'
    )
    html = html.replace("<body>", "<body>" + notice, 1)
    (output / "common_timeline.html").write_text(html, encoding="utf-8")
    data = {
        "selected_point": point,
        "points": datasets,
        "execution_order": [list(item) for item in compiled.execution_order()],
        "shot_gaps": "unknown; not concatenated into a physical timeline",
    }
    (output / "common_timeline.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    _static_plot(selected, output)
    for filename, rows in (
        ("timing.csv", selected["timing"]),
        ("dds_commands.csv", selected["dds_rows"]),
    ):
        with (output / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return {
        "html": output / "common_timeline.html",
        "png": output / "common_timeline.png",
        "svg": output / "common_timeline.svg",
        "data": output / "common_timeline.json",
        "timing_csv": output / "timing.csv",
        "dds_csv": output / "dds_commands.csv",
        "figure": fig,
    }
