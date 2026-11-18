"""Presentation-only native DDS trajectories on the existing control timeline.

These vertices describe register commands and native linear slopes. They are
neither sampled RF nor measured atom motion, and never enter compilation.
"""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction

from taiyidsl.backends.artiq_spectrum.dds import LOGICAL_TICK_SECONDS


def _fraction(value):
    return Fraction(str(value))


def _curves(program, frame0, hold_start, hold_end):
    tick = _fraction(LOGICAL_TICK_SECONDS)
    curves = {}

    def append(tone, time, frame, stage, seconds=0):
        curve = curves.setdefault(
            tone["core"],
            {
                "core": tone["core"],
                "channel": tone["channel"],
                "time_s": [],
                "frequency_hz": [],
                "amplitude": [],
                "frame": [],
                "stage": [],
            },
        )
        curve["time_s"].append(float(time))
        curve["frequency_hz"].append(
            tone["frequency_hz"] + tone.get("frequency_slope_hz_per_s", 0) * seconds
        )
        curve["amplitude"].append(
            tone["amplitude"] + tone.get("amplitude_slope_per_s", 0) * seconds
        )
        curve["frame"].append(frame)
        curve["stage"].append(stage)

    for tone in program["initial_tones"]:
        append(tone, hold_start, -1, "initial_hold")
    for index, frame in enumerate(program["frames"]):
        start = frame0 + frame["at_mu"] * tick
        delay = frame.get("ramp_update_delay_mu", 0)
        for tone in frame["tones"]:
            append(tone, start, index, "frame_start")
            if frame["timer_mu"] is None:
                continue
            if delay:
                append(tone, start + delay * tick, index, "ramp_start")
            # A simultaneous frequency pin delays the first ramp update. Do
            # not integrate through that hold, or hide the pre-pin endpoint.
            seconds = float((frame["timer_mu"] - delay) * tick)
            append(
                tone,
                start + frame["timer_mu"] * tick,
                index,
                "ramp_end_before_next_commit",
                seconds,
            )
    for tone in program["final_tones"]:
        append(tone, hold_end, len(program["frames"]) - 1, "terminal_hold")
    return list(curves.values())


def dds_card_views(compiled, point, *, control_window=None):
    """Yield compact per-region curves with disjoint, bounded hold extensions.

    Local times use the same physical trigger minus point origin as AWG sample
    reconstruction. Known preparation/readout intervals can show hold; unknown
    host waits and shot gaps are never assigned a fictitious duration.
    """
    records = defaultdict(list)
    tick = _fraction(compiled.setup.tick_seconds)
    for event in point.physical_events:
        if event.kind != "spectrum_program":
            continue
        group = event.params
        trigger = (
            group["trigger_physical_mu"] * _fraction(group["trigger_time_unit_s"])
            - point.origin_mu * tick
        )
        for name, card in group["cards"].items():
            contract, program = card["contract"], card["program"]
            if program.get("kind") != "dds":
                continue
            frame0 = trigger + _fraction(contract["dds_trigger_latency_ns"]) / 10**9
            records[group["name"], name].append(
                (frame0, trigger, group, name, contract, program)
            )
    if not records:
        return
    if control_window is None:
        from .native_view import native_lifecycle
        from .viewer_execution import _point_windows

        lifecycle = native_lifecycle(compiled)
        native = (
            next((p for p in lifecycle["points"] if p["index"] == point.index), None)
            if lifecycle
            else None
        )
        control_window, _ = _point_windows(point, tick, native)
    low = Fraction(*map(int, control_window["start_fraction"]))
    high = Fraction(*map(int, control_window["end_fraction"]))
    for regions in records.values():
        regions.sort(key=lambda item: item[0])
        for index, (frame0, trigger, group, name, contract, program) in enumerate(
            regions
        ):
            command_end = frame0 + program["frames"][-1]["at_mu"] * _fraction(
                LOGICAL_TICK_SECONDS
            )
            hold_start = low if index == 0 else frame0
            hold_end = regions[index + 1][0] if index + 1 < len(regions) else high
            if hold_start > frame0 or hold_end < command_end:
                raise ValueError("DDS viewer regions overlap or exceed control bounds")
            yield {
                "group": group["name"],
                "region_id": group["region_id"],
                "card": name,
                "mode": "dds",
                "active_channels": list(contract["active_channels"]),
                "frame0_time_s": float(frame0),
                "trigger_time_s": float(trigger),
                "trigger_latency_ns": contract["dds_trigger_latency_ns"],
                "command_start_s": float(frame0),
                "command_end_s": float(command_end),
                "hold_start_s": float(hold_start),
                "hold_end_s": float(hold_end),
                "model": "quantized_DDS_commands_not_RF_samples",
                "terminal_state": "continuous_hold_until_session_close",
                "curves": _curves(program, frame0, hold_start, hold_end),
            }
