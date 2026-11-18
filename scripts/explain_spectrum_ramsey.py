"""Explain actual hybrid export artifacts; never open an instrument.

Tables decode finite step playback, and RF figures decode packed DAC samples.
All times are relative to the logical shot origin T0, unless labeled otherwise.
"""

import argparse
import csv
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from taiyidsl.spectrum_calibration import resolve_channel_calibration
from taiyidsl.spectrum_waveforms import (
    materialize_segment,
    reconstruct_window,
    validate_program,
)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def table(rows, columns):
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines.extend("| " + " | ".join(str(row[c]) for c in columns) + " |" for row in rows)
    return "\n".join(lines)


def phase_details(compiled, point, group, card_name, pulse):
    """Check exported phase against its logical drive and declared reference.

    Historical exports without reference metadata use the shot origin. A
    continuous carrier retains its exported integrated history; an epoch carrier
    is independently evaluated from frequency and the actual reference tick.
    """
    logical = next(
        event
        for event in point["logical_events"]
        if event["kind"] == "play" and event["logical_ids"] == pulse["logical_id"]
    )
    config = compiled["setup"]["spectrum"]["groups"][group["name"]]
    binding = next(
        binding
        for binding in compiled["signal_map"][logical["signal"]]
        if config["outputs"][binding["device"]]["card"] == card_name
        and config["outputs"][binding["device"]]["channel"] == pulse["channel"]
    )
    reference = group.get("phase_reference", {})
    evolution = pulse.get("phase_evolution", {})
    reference_ticks = evolution.get(
        "carrier_reference_ticks", reference.get("reference_start_ticks", 0)
    )
    tick = Fraction(
        str(reference.get("tick_seconds", compiled["setup"]["tick_seconds"]))
    )
    mode = evolution.get("carrier_reference_kind", reference.get("mode", "shot"))
    if evolution.get("carrier_mode", "epoch") == "epoch":
        cycles = (
            Fraction(str(pulse["frequency_hz"]))
            * (logical["start_mu"] - reference_ticks)
            * tick
        ) % 1
        carrier = float(cycles) * 2 * math.pi
        if "carrier_phase_rad" in evolution:
            assert math.isclose(carrier, evolution["carrier_phase_rad"], abs_tol=1e-10)
    else:
        carrier = evolution["carrier_phase_rad"]
    calibration = resolve_channel_calibration(
        config.get("calibration", {}),
        card_name,
        pulse["channel"],
        pulse["frequency_hz"],
    )
    expected = (
        carrier
        + binding["phase_scale"] * logical["params"]["phase"]
        + binding["phase_offset"]
        + calibration.get("phase_offset_rad", 0)
        + evolution.get("inherited_modulation_phase_rad", 0)
    )
    assert (
        abs((pulse["phase_rad"] - expected + math.pi) % (2 * math.pi) - math.pi) < 1e-9
    )
    params = logical["params"]
    frame = (
        params["phase"] - params["requested_phase"]
        if "requested_phase" in params
        else params.get("frame_phase", 0) - params.get("virtual_z_phase", 0)
    )
    return {
        "reference_mode": mode,
        "reference_us_T0": float(reference_ticks * tick * 10**6),
        "carrier_deg": math.degrees(carrier) % 360,
        "frame_deg": math.degrees(binding["phase_scale"] * frame),
        "frequency_hz": pulse["frequency_hz"],
        "amplitude": pulse["amplitude"],
    }


def inspect_export(export, output):
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    compiled = json.loads((export / "compiled.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(
        json.dumps(compiled, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    assert digest == manifest["compiled_sha256"], "Mismatched compiled snapshot"
    tick_ns = Fraction(str(compiled["setup"]["tick_seconds"])) * 10**9
    points = []
    for point, physical in zip(manifest["points"], compiled["points"], strict=True):
        group = point["groups"][0]
        assert group["name"] == "qubit_awg"
        point_dir = output / f"point_{point['index']}"
        point_dir.mkdir(parents=True, exist_ok=True)
        timing = [
            {
                "operation": e["kind"],
                "device": e["signal"],
                "start_ns_T0": float((e["start_mu"] - physical["origin_mu"]) * tick_ns),
                "duration_ns": float(e["duration_mu"] * tick_ns),
            }
            for e in physical["physical_events"]
            if e["kind"] != "spectrum_program"
        ]
        assert (
            sum(
                e["operation"] == "ttl_on" and e["device"] == "awg_trigger"
                for e in timing
            )
            == 1
        )
        write_csv(point_dir / "artiq_timing.csv", timing)
        cards = {}
        for name, item in group["cards"].items():
            program, contract = item["program"], item["contract"]
            validation = validate_program(program)
            period_ns = Fraction(10**9, contract["sample_rate_hz"])
            program_start_ns = (
                Fraction(group["trigger_relative_mu"])
                * Fraction(str(group["trigger_time_unit_s"]))
                * 10**9
                + contract["trigger_latency_samples"] * period_ns
            )
            directory = point_dir / name
            directory.mkdir(exist_ok=True)
            segments = []
            for segment in program["segments"]:
                packed = materialize_segment(program, segment["id"]).tobytes()
                assert hashlib.sha256(packed).hexdigest() == segment["sha256"]
                (directory / f"segment_{segment['id']}.bin").write_bytes(packed)
                segments.append(
                    {
                        "id": segment["id"],
                        "samples_per_channel": segment["sample_count"],
                        "duration_us": float(
                            segment["sample_count"] * period_ns / 1000
                        ),
                        "bytes": len(packed),
                        "content": segment["recipe"]["kind"],
                        "sha256": segment["sha256"],
                    }
                )
            lookup = {entry["id"]: entry for entry in segments}
            steps, cursor = [], 0
            for step in program["steps"]:
                count = (
                    lookup[step["segment_id"]]["samples_per_channel"] * step["loops"]
                )
                steps.append(
                    {
                        "step": step["index"],
                        "segment": step["segment_id"],
                        "loops": step["loops"],
                        "start_sample": cursor,
                        "stop_sample": cursor + count,
                        "start_us_T0": float(
                            (program_start_ns + cursor * period_ns) / 1000
                        ),
                        "stop_us_T0": float(
                            (program_start_ns + (cursor + count) * period_ns) / 1000
                        ),
                        "next": "END" if step["end"] else step["next_step"],
                    }
                )
                cursor += count
            assert cursor == program["physical_duration_samples"]
            pulses = []
            for pulse in program["pulses"]:
                start_ns = program_start_ns + pulse["start_sample"] * period_ns
                end_ns = start_ns + pulse["duration_samples"] * period_ns
                pulses.append(
                    {
                        "channel": pulse["channel"],
                        "start_us_T0": float(start_ns / 1000),
                        "end_us_T0": float(end_ns / 1000),
                        "phase_deg": math.degrees(pulse["phase_rad"]) % 360,
                        **phase_details(compiled, physical, group, name, pulse),
                    }
                )
            write_csv(directory / "segments.csv", segments)
            write_csv(directory / "steps.csv", steps)
            cards[name] = {
                "segments": segments,
                "steps": steps,
                "pulses": pulses,
                "program_start_us_T0": float(program_start_ns / 1000),
                "memory": program["memory"],
                "validation": validation,
                "active_channels": contract["active_channels"],
                "sample_rate_hz": contract["sample_rate_hz"],
            }
        p1, p2 = cards["A"]["pulses"]
        assert math.isclose(
            p2["start_us_T0"] - p1["end_us_T0"],
            point["coordinates"]["dark_time"] * 1e6,
            abs_tol=1e-9,
        )
        points.append(
            {
                "index": point["index"],
                "dark_time_ms": point["coordinates"]["dark_time"] * 1000,
                "artiq": timing,
                "cards": cards,
                "native_origin_mu": physical["origin_mu"] // 5,
                "native_origin_ns": float(physical["origin_mu"] * tick_ns),
                "epoch": point["epoch"],
                "terminal_frames": group["terminal_frames"],
            }
        )
    return manifest, points


def draw_figure(manifest, points, output):
    chosen = max(points, key=lambda p: p["dark_time_ms"])
    card = chosen["cards"]["A"]
    first, second = card["pulses"]
    exported = manifest["points"][chosen["index"]]["groups"][0]["cards"]["A"]
    program = exported["program"]
    pulse = program["pulses"][1]
    assert pulse["envelope"]["kind"] == "square" and not pulse["phase_modulation"]
    assert not pulse["phase_steps"], "This Ramsey figure expects constant pulse phase"
    rate = exported["contract"]["sample_rate_hz"]
    start = pulse["start_sample"]
    # Decode at most 100 us of actual DAC samples, never the full idle.
    count = min(pulse["duration_samples"], round(100e-6 * rate))
    samples = reconstruct_window(program, start, start + count)[:, 0] / 32767
    local_us = np.arange(count) / rate * 1e6
    phase = math.radians(second["phase_deg"])
    advance = 2 * np.pi * second["frequency_hz"] * local_us / 1e6
    expected = second["amplitude"] * np.cos(phase + advance)
    assert np.max(np.abs(samples - expected)) <= 0.5001 / 32767
    without_frame = second["amplitude"] * np.cos(
        phase - math.radians(second["frame_deg"]) + advance
    )

    def edge(kind, device):
        return next(
            event["start_ns_T0"] / 1000
            for event in chosen["artiq"]
            if event["operation"] == kind and event["device"] == device
        )

    trigger_on, trigger_off = (
        edge(kind, "awg_trigger") for kind in ("ttl_on", "ttl_off")
    )
    cooling_on, cooling_off = (
        edge(kind, "cooling_dds") for kind in ("dds_on", "dds_off")
    )
    readout_on, readout_off = (
        edge(kind, "readout_ttl") for kind in ("ttl_on", "ttl_off")
    )
    program_start = card["program_start_us_T0"]
    trigger_limits = (trigger_on - 0.088, first["start_us_T0"] + 0.05)
    lead_samples = program["pulses"][0]["start_sample"]

    plt.rcParams.update(
        {"font.size": 10, "axes.spines.top": False, "axes.spines.right": False}
    )
    fig = plt.figure(figsize=(14, 8.5), layout="constrained")
    grid = fig.add_gridspec(2, 2, height_ratios=(1.2, 1))
    axes = [fig.add_subplot(grid[0, i]) for i in range(2)]
    windows = [
        (-4, first["end_us_T0"] + 25),
        (second["start_us_T0"] - 25, second["end_us_T0"] + 15),
    ]
    colors = {"artiq": "#2869a5", "rf": "#16764b", "a": "#7851a9", "b": "#818997"}
    rows = [
        ("ARTIQ cooling", 5),
        ("ARTIQ trigger", 4),
        ("A CH0 RF envelope", 3),
        ("A sequence steps", 2),
        ("B CH0/1 zero", 1),
        ("ARTIQ readout", 0),
    ]
    for ax, limits in zip(axes, windows, strict=True):
        ax.set_xlim(*limits)
        ax.set_ylim(-0.5, 5.7)
        ax.set_yticks([v for _, v in rows], [name for name, _ in rows])
        ax.set_xlabel("Time relative to logical T0 (us); broken axis above")
        ax.ticklabel_format(axis="x", style="plain", useOffset=False)
        ax.grid(axis="x", alpha=0.18)
        ax.barh(
            5,
            cooling_off - cooling_on,
            left=cooling_on,
            height=0.45,
            color=colors["artiq"],
        )
        ax.plot([trigger_on], [4], marker="^", color=colors["artiq"], markersize=8)
        for pulse in card["pulses"]:
            ax.barh(
                3,
                pulse["end_us_T0"] - pulse["start_us_T0"],
                left=pulse["start_us_T0"],
                height=0.45,
                color=colors["rf"],
            )
        for name, row, color in [("A", 2, colors["a"]), ("B", 1, colors["b"])]:
            for step in chosen["cards"][name]["steps"]:
                ax.barh(
                    row,
                    step["stop_us_T0"] - step["start_us_T0"],
                    left=step["start_us_T0"],
                    height=0.45,
                    color=color,
                    alpha=0.35 + 0.15 * (step["step"] % 2),
                )
                left, right = (
                    max(limits[0], step["start_us_T0"]),
                    min(limits[1], step["stop_us_T0"]),
                )
                if right - left > 45:
                    ax.text(
                        (left + right) / 2,
                        row,
                        f"S{step['segment']} x{step['loops']:,}",
                        ha="center",
                        va="center",
                    )
        ax.barh(
            0,
            readout_off - readout_on,
            left=readout_on,
            height=0.45,
            color=colors["artiq"],
        )
    axes[0].set_title(
        f"First pulse: {first['phase_deg']:.0f} deg at its first sample", loc="left"
    )
    axes[1].set_title(
        f"Second pulse: {second['phase_deg']:.0f} deg; "
        f"frame contribution {second['frame_deg']:g} deg",
        loc="left",
    )
    axes[1].set_yticklabels([])
    # Explicit breaks: the white interval is omitted time, not an inserted gap.
    for ax, x in [(axes[0], 1), (axes[1], 0)]:
        ax.plot(
            (x - 0.01, x + 0.01),
            (-0.015, 0.015),
            transform=ax.transAxes,
            color="0.3",
            clip_on=False,
        )

    trigger = fig.add_subplot(grid[1, 0])
    trigger.plot(
        [
            trigger_limits[0],
            trigger_on,
            trigger_on,
            trigger_off,
            trigger_off,
            trigger_limits[1],
        ],
        [0, 0, 1, 1, 0, 0],
        color=colors["artiq"],
        label="ARTIQ common TTL",
    )
    trigger.axvline(
        program_start,
        color=colors["a"],
        linestyle="--",
        label=f"DAC program sample 0: {program_start:g} us",
    )
    trigger.axvline(
        first["start_us_T0"],
        color=colors["rf"],
        label=f"First RF sample: {first['start_us_T0']:g} us",
    )
    trigger.annotate(
        f"{(trigger_off - trigger_on) * 1000:g} ns TTL",
        xy=((trigger_on + trigger_off) / 2, 1),
        xytext=(trigger_off + 0.048, 1.25),
        arrowprops={"arrowstyle": "-", "color": "0.4"},
    )
    trigger.annotate(
        f"{lead_samples} zero samples = {lead_samples / rate * 1e9:g} ns",
        xy=((program_start + first["start_us_T0"]) / 2, 0.3),
        xytext=(trigger_off + 0.128, 0.55),
        arrowprops={"arrowstyle": "-", "color": "0.4"},
    )
    trigger.set(
        xlim=trigger_limits,
        ylim=(-0.1, 1.45),
        xlabel="Time relative to logical T0 (us)",
        ylabel="TTL logical level",
        title=(
            "Start timing: declared "
            f"{exported['contract']['trigger_latency_samples'] / rate * 1e9:g} "
            "ns latency (unmeasured)"
        ),
    )
    trigger.grid(alpha=0.18)
    trigger.legend(loc="lower left", bbox_to_anchor=(0, -0.52), frameon=False)

    rf = fig.add_subplot(grid[1, 1])
    rf.plot(
        local_us[::80],
        samples[::80],
        color=colors["rf"],
        label="Decoded packed samples, with frame F",
    )
    rf.plot(
        local_us[::80],
        without_frame[::80],
        color="0.5",
        linestyle="--",
        label="Calculated reference with F = 0",
    )
    rf.set(
        xlim=(0, count / rate * 1e6),
        xlabel="Time after second pulse start (us)",
        ylabel="Normalized DAC amplitude",
        title=(
            f"Second pulse: reference {second['reference_mode']} "
            f"at T0 + {second['reference_us_T0']:g} us"
        ),
    )
    rf.grid(alpha=0.18)
    rf.legend(loc="lower left", bbox_to_anchor=(0, -0.43), frameon=False)
    fig.suptitle(
        f"Compiled Ramsey: {first['end_us_T0'] - first['start_us_T0']:g} us pulse / "
        f"{chosen['dark_time_ms']:g} ms dark time / frame / "
        f"{second['end_us_T0'] - second['start_us_T0']:g} us pulse",
        fontsize=14,
    )
    fig.savefig(output / "ramsey_timing.png", dpi=155)
    fig.savefig(output / "ramsey_timing.svg")
    plt.close(fig)


def write_walkthrough(points, export, output):
    lines = [
        "# Ramsey 实际编译产物",
        "所有时间相对逻辑 shot 原点 T0。样本周期 0.8 ns；"
        "half-open 区间 [start, stop)。",
        "图与表来自实际导出，不是手工设计的预期分段。模拟序列号/延迟未标定，未运行硬件。",
        "![时序与实际重建 RF](ramsey_timing.png)",
        "## 扫描与相位",
        table(
            [
                {
                    "dark_ms": p["dark_time_ms"],
                    "P1_start_us": p["cards"]["A"]["pulses"][0]["start_us_T0"],
                    "P2_start_us": p["cards"]["A"]["pulses"][1]["start_us_T0"],
                    "P1_deg": round(p["cards"]["A"]["pulses"][0]["phase_deg"], 8),
                    "P2_deg": round(p["cards"]["A"]["pulses"][1]["phase_deg"], 8),
                    "reference": p["cards"]["A"]["pulses"][1]["reference_mode"],
                    "reference_us_T0": p["cards"]["A"]["pulses"][1]["reference_us_T0"],
                }
                for p in points
            ],
            [
                "dark_ms",
                "P1_start_us",
                "P2_start_us",
                "P1_deg",
                "P2_deg",
                "reference",
                "reference_us_T0",
            ],
        ),
        "P1/P2 的 phase 指第一个 RF sample 的载波相位，"
        "不是包络相位或物理 qubit 自由进动相位。",
        "载波参考从实际 pulse.phase_evolution 与 group.phase_reference 读取。"
        "sequence 参考序列入口，shot 参考逻辑 shot T0；显式 reset_phase 可建立新参考。"
        "旧产物缺少参考字段时按其历史约定使用 shot T0。",
        "epoch 载波按 C = 2π f × (逻辑播放时刻 − 载波参考时刻) 验算；"
        "continuous 使用导出中已积分的 C。"
        "实际起相为 C + M + s × (base + F) + binding offset + calibration。"
        "脚本从逻辑事件和配置独立组合这些量，再核对最终 phase_rad 与 packed samples。"
        "Rz(θ) 使 F 减 θ；reset_frame 清 F，reset_phase 同时清 C/M。"
        "等待计入载波演化，没有在暗区实际播放 NCO。",
        "## 表格约定",
        "S0 等编号是每张卡当前扫描点的 segment ID，不是跨卡共享的波形。"
        "END 时 next_step=0 不再执行；不是循环回 S0。",
        "samples_per_channel 是每通道样本数；"
        "bytes 包含启用通道的 int16 交织数据。A CH1 关闭；"
        "本 Ramsey 的 B 两路保持零输出，"
        "但仍 arm 并执行同一公共触发下的有限零程序。",
        "memory.resident_bytes 是实际唯一段数据；"
        "allocated_bytes 是按最大段长度与槽位数算出的容量需求，"
        "slot_bytes 是硬件等分后的每槽可用容量，不代表上传字节数。",
    ]
    for point in points:
        lines.extend(
            [
                f"## 扫描点 {point['index']}：delay = {point['dark_time_ms']} ms",
                "### ARTIQ 物理事件",
                table(
                    point["artiq"],
                    ["operation", "device", "start_ns_T0", "duration_ns"],
                ),
            ]
        )
        for name, card in point["cards"].items():
            lines.extend(
                [
                    f"### 卡 {name}",
                    f"程序 sample 0 = T0 + {card['program_start_us_T0']} µs；"
                    f"内存：`{card['memory']}`。",
                    table(
                        card["segments"],
                        [
                            "id",
                            "samples_per_channel",
                            "duration_us",
                            "bytes",
                            "content",
                        ],
                    ),
                    table(
                        card["steps"],
                        [
                            "step",
                            "segment",
                            "loops",
                            "start_sample",
                            "stop_sample",
                            "start_us_T0",
                            "stop_us_T0",
                            "next",
                        ],
                    ),
                ]
            )
    chosen = max(points, key=lambda point: point["dark_time_ms"])
    card = chosen["cards"]["A"]
    first, second = card["pulses"]
    gap = second["start_us_T0"] - first["end_us_T0"]
    tail_ns = (card["steps"][-1]["stop_us_T0"] - second["end_us_T0"]) * 1000
    lines.extend(
        [
            f"## {chosen['dark_time_ms']:g} ms 点的边界核算",
            f"两门之间 {gap:g} µs，对应 {round(gap * card['sample_rate_hz'] / 1e6)} "
            f"个样本。有限程序结束于第二门之后 {tail_ns:g} ns；"
            "段对齐的末尾零不计入门宽，不改变读出起点。",
            f"卡 A 程序 sample 0 在 T0 + {card['program_start_us_T0']:g} µs，"
            f"第一门在 T0 + {first['start_us_T0']:g} µs。"
            "预先触发与 cooling 末尾重叠是声明 latency 补偿，不是门内 ARTIQ 控制。",
            f"此点实际载波参考为 {second['reference_mode']}，位于 T0 + "
            f"{second['reference_us_T0']:g} µs；"
            "不能把图表时间坐标 T0 自动当作载波原点。"
            f"导出 native pre-roll 偏移为 {chosen['native_origin_ns']:g} ns，"
            "它与载波参考分别记录。生成代码的 epoch 对齐参数以导出为准。",
            f"原始 manifest：`{(export / 'manifest.json').resolve()}`。"
            "每卡每点CSV及逐段`.bin`位于本报告同级目录。",
        ]
    )
    (output / "walkthrough.md").write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path("output/artiq_spectrum/ramsey_long_delay")
    parser.add_argument("--export", type=Path, default=default / "export")
    parser.add_argument("--output", type=Path, default=default)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest, points = inspect_export(args.export, args.output)
    draw_figure(manifest, points, args.output)
    write_walkthrough(points, args.export, args.output)
    (args.output / "decoded.json").write_text(
        json.dumps(points, indent=2), encoding="utf-8"
    )
    print(f"Verified packed samples and finite step tables: {args.output.resolve()}")


if __name__ == "__main__":
    main()
