"""Review DDS commands and trajectories without inventing RF sample phases."""

from __future__ import annotations

import csv
import json


def dds_execution(item):
    contract, program = item["contract"], item["program"]
    return {
        "configuration": [
            {"setting": key, "value": contract.get(key), "source": "contract"}
            for key in (
                "mode",
                "serial_number",
                "dds_routing",
                "active_channels",
                "sample_rate_hz",
                "reference_clock_hz",
                "output_range_mv",
                "trigger_device",
                "dds_trigger_latency_ns",
            )
        ],
        "upload_listing": (
            "# Compiled DDS frame parameters; inspection only, not execution.\n"
            "# Exact SDK calls and guards are in runtime/spectrum_dds_runtime.py.\n"
            + json.dumps(program, ensure_ascii=False, indent=2)
            + "\n"
        ),
    }


def _figure(rows, key, label, scale):
    """Plot the command model using a noninteractive backend, without RF phase."""
    from matplotlib.figure import Figure

    figure = Figure(figsize=(10, 3.4), layout="constrained")
    ax = figure.subplots()
    for core in sorted({r["core"] for r in rows}):
        values = [r for r in rows if r["core"] == core]
        ax.plot(
            [r["time_us"] for r in values],
            [r[key] * scale for r in values],
            label=f"tone {core}",
            linewidth=1.3,
        )
    ax.set(xlabel="Time from DDS start (µs)", ylabel=label)
    ax.grid(alpha=0.2)
    ax.legend(loc="best", ncols=3, fontsize=8)
    return figure


def _save_figure(figure, path):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.backends.backend_svg import FigureCanvasSVG

    FigureCanvasSVG(figure).print_svg(path.with_suffix(".svg"))
    FigureCanvasAgg(figure).print_png(path.with_suffix(".png"))


def export_transport_paths(moves, directory):
    """Requested atom paths; physical DDS errors are reported separately."""
    from matplotlib.figure import Figure

    figure = Figure(figsize=(8, 4), layout="constrained")
    ax = figure.subplots()
    for uid in sorted(moves[0]["initial_layout"]):
        positions = [moves[0]["initial_layout"][uid]] + [
            m["final_layout"][uid] for m in moves
        ]
        ax.plot(
            [p["x"] * 1e6 for p in positions],
            [p["y"] * 1e6 for p in positions],
            "o-",
            label=uid,
        )
        for i, p in enumerate(positions[:-1]):
            ax.annotate(
                f"{uid}: {i}",
                (p["x"] * 1e6, p["y"] * 1e6),
                xytext=(4, 7),
                textcoords="offset points",
                fontsize=8,
            )
    ax.set(
        xlabel="x (µm)", ylabel="y (µm)", title="Requested atom paths (not measured)"
    )
    ax.set_aspect("equal", adjustable="datalim")
    ax.margins(x=0.12, y=0.2)
    ax.grid(alpha=0.2)
    ax.legend()
    _save_figure(figure, directory / "transport_paths")


def export_dds_card(item, directory):
    program, contract = item["program"], item["contract"]
    rows = []
    for frame in program["frames"]:
        delay = frame.get("ramp_update_delay_mu", 0)
        for tone in frame["tones"]:
            rows.append({"time_us": frame["at_mu"] * 0.0002, **tone})
            if frame["timer_mu"] is not None:
                if delay:
                    rows.append({"time_us": (frame["at_mu"] + delay) * 0.0002, **tone})
                seconds = (frame["timer_mu"] - delay) * 0.2e-9
                rows.append(
                    {
                        **tone,
                        "time_us": (frame["at_mu"] + frame["timer_mu"]) * 0.0002,
                        "frequency_hz": tone["frequency_hz"]
                        + seconds * tone["frequency_slope_hz_per_s"],
                        "amplitude": tone["amplitude"]
                        + seconds * tone["amplitude_slope_per_s"],
                    }
                )
    (directory / "dds_program.json").write_text(
        json.dumps(program, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (directory / "dds_trajectory.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for name, key, label, scale in (
        ("frequency", "frequency_hz", "Planned DDS frequency (MHz)", 1e-6),
        ("amplitude", "amplitude", "Planned DDS amplitude (full scale)", 1),
    ):
        _save_figure(_figure(rows, key, label, scale), directory / name)
    execution = dds_execution(item)
    (directory / "sdk_calls.txt").write_text(
        execution["upload_listing"], encoding="utf-8"
    )
    return {
        "mode": "dds",
        "active_channels": contract["active_channels"],
        "sample_rate_hz": contract["sample_rate_hz"],
        "command_count": program["command_count"],
        "frames": program["frames"],
        "quantization": program.get("quantization", {}),
        "initial_tones": program["initial_tones"],
        "final_tones": program["final_tones"],
        "spectrum_execution": execution,
        "model": "planned_DDS_commands_not_DAC_samples_or_measurements",
        "terminal_state": "continuous_hold_until_session_close",
    }
