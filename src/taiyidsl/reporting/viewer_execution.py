"""Compact, presentation-only bounds for concatenated execution windows.

The compiler does not know host waits or variable native preparation times.
These bounds therefore describe accumulated *known* windows, not elapsed wall
clock time. Every event keeps its original local timestamp and carrier phase.
"""

from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

from ..host_plan import execution_plan_for, point_execution_layout
from .native_view import native_lifecycle


def _fraction(value):
    return Fraction(str(value))


def _window(start, end):
    return {
        "start_s": float(start),
        "end_s": float(end),
        "start_fraction": [str(start.numerator), str(start.denominator)],
        "end_fraction": [str(end.numerator), str(end.denominator)],
    }


def _point_windows(point, tick, native):
    origin = getattr(point, "origin_mu", 0) * tick
    low = Fraction(0)
    high = getattr(point, "duration_mu", 0) * tick
    awg = []
    for event in getattr(point, "logical_events", ()):
        start = event.start_mu * tick
        end = start + event.duration_mu * tick
        low, high = min(low, start), max(high, end)
        if event.kind == "spectrum_region":
            awg.extend((start, end))
    for event in point.physical_events:
        if event.kind == "spectrum_program":
            group = event.params
            trigger = (
                group["trigger_physical_mu"] * _fraction(group["trigger_time_unit_s"])
                - origin
            )
            low, high = min(low, trigger), max(high, trigger)
            for card in group["cards"].values():
                contract, program = card["contract"], card["program"]
                if program.get("kind") == "dds":
                    start = (
                        trigger + _fraction(contract["dds_trigger_latency_ns"]) / 10**9
                    )
                    end = start + program["frames"][-1]["at_mu"] * tick
                    awg.extend((start, end))
                    low, high = min(low, start), max(high, end)
                    continue
                rate = _fraction(program["card_spec"]["sample_rate_hz"])
                start = trigger + contract["trigger_latency_samples"] / rate
                end = start + program["physical_duration_samples"] / rate
                awg.extend((start, end))
                low, high = min(low, start), max(high, end)
        else:
            start = event.start_mu * tick - origin
            end = start + event.duration_mu * tick
            low, high = min(low, start), max(high, end)
    if native:
        for call in [*native.get("timed", ()), *native.get("suffix", ())]:
            low = min(low, _fraction(call["start_s_from_control_T0"]))
            high = max(high, _fraction(call["end_s_from_control_T0"]))
    return _window(low, high), _window(min(awg), max(awg)) if awg else None


def execution_axis(compiled):
    """Return O(points + chunks) metadata, never O(points * shots) copies.

    ``control`` includes physical pre-roll, logical delay, DAC tail and declared
    fixed native stages. The legacy key ``awg`` spans all finite Spectrum
    AWG/DDS command regions/cards of a point,
    preserving gaps between regions, compensating prefixes and physical tails.
    If a point has no AWG region, the AWG projection retains its known control
    window as an empty interval instead of silently dropping that execution.

    The JS companion enumerates bounded pages in the actual host-plan order.
    The minimal legacy simulator adapter is supported with one cyclic shot.
    """
    adapter = SimpleNamespace(
        points=compiled.points,
        shots=getattr(compiled, "shots", 1),
        averaging_mode=getattr(compiled, "averaging_mode", "cyclic"),
        host_execution_plan=getattr(compiled, "host_execution_plan", None),
    )
    plan = execution_plan_for(adapter)
    layouts = point_execution_layout(plan)
    lifecycle = native_lifecycle(compiled)
    native_points = (
        {point["index"]: point for point in lifecycle["points"]} if lifecycle else {}
    )
    tick = _fraction(compiled.setup.tick_seconds)
    points = []
    for point in compiled.points:
        native = native_points.get(point.index)
        control, awg = _point_windows(point, tick, native)
        points.append(
            {
                "index": point.index,
                "control": control,
                "awg": awg or control,
                "has_awg": awg is not None,
                "layout": dict(layouts[point.index]),
                "native_prefix_duration_unknown": bool(native and native["prefix"]),
            }
        )
    return {
        "schema_version": 1,
        "axis_kind": "concatenated_known_windows",
        "wall_clock_time": False,
        "host_intervals_included": False,
        "native_waveforms_simulated": False,
        "shots": plan["shots"],
        "mode": plan["averaging_mode"],
        "scope": plan["averaging_scope"],
        "total_executions": len(points) * plan["shots"],
        "chunks": [
            {"index": chunk["index"], "points": list(chunk["points"])}
            for chunk in plan["chunks"]
        ],
        "points": points,
    }
