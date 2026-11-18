"""Deterministic dependency scheduling of explicitly selected Sections.

Each direct child is indivisible. This places existing relative timelines; it is
not a hardware preparation solver and does not stretch pulses or insert gaps.
"""

import math
from dataclasses import replace

from ..ir import CompileError
from .resources import _frame as _frame
from .resources import _resources as _resources
from .resources import _walk, node_resources
from .resources import qubit_resources as qubit_resources


def _combine_grid(residue, period, next_residue, next_period):
    """Intersect start-time congruences without rounding internal boundaries."""
    divisor = math.gcd(period, next_period)
    difference = next_residue - residue
    if difference % divisor:
        raise CompileError("Section internal timing grids cannot share one start time")
    reduced = next_period // divisor
    multiplier = (
        (difference // divisor) * pow(period // divisor, -1, reduced)
    ) % reduced
    combined = math.lcm(period, next_period)
    return (residue + period * multiplier) % combined, combined


def _start_grid(scheduler, events):
    default_grid = scheduler.section_grid()
    origin = scheduler.time_origin
    # Containers have no hardware edge. Constrain their events, not the nominal
    # container start: a nested Section can already contain an alignment wait.
    residue, period = 0, 1
    for event in events:
        grid = default_grid
        if event.signal in scheduler.mapping:
            grid = math.lcm(grid, scheduler.signal_grid(event.signal))
        residue, period = _combine_grid(
            residue, period, -(origin + event.start_mu) % grid, grid
        )
    return residue, period


def schedule_asap(scheduler, node, frames, realtime):
    """Place independent children early and join them at the section exit."""
    if not realtime:
        raise CompileError("ASAP Section requires a realtime control region")
    forbidden = {"native_call", "native_timed", "native_delay", "triggered_sequence"}
    if any(child.kind in forbidden for child in _walk(node)):
        raise CompileError("ASAP Section cannot cross native or triggered boundaries")
    available, floor, end, result = {}, 0, 0, []
    for ordinal, child in enumerate(node.children):
        local = dict(frames)
        events, duration = scheduler.node(child, local, realtime)
        resources = node_resources(scheduler, child, events)
        synchronization = (
            child.kind in {"section_delay", "scope_delay"}
            or (child.kind == "barrier" and not child.params.get("qubits"))
            or child.params.get("circuit_layer", False)
        )
        if synchronization or (not resources and duration):
            ready = end
        else:
            ready = max([floor, *(available.get(r, 0) for r in resources)])
        residue, grid = _start_grid(scheduler, events)
        start = ready + (residue - ready) % grid
        finish = start + duration
        for event in events:
            shifted = replace(event, start_mu=event.start_mu + start)
            shifted.params.setdefault("section_schedule", []).insert(
                0,
                {
                    "name": node.params.get("operation_name", "section"),
                    "child_index": ordinal,
                    "dependency_ready_mu": ready,
                    "scheduled_start_mu": start,
                    "alignment_wait_mu": start - ready,
                    "start_grid_mu": grid,
                    "blocking_resources": [
                        f"{kind}:{name}"
                        for kind, name in sorted(resources)
                        if available.get((kind, name), 0) == ready and ready > 0
                    ],
                    "boundary": (
                        "layer"
                        if child.params.get("circuit_layer")
                        else "section_join"
                        if synchronization
                        else "prior_layer_or_join"
                        if floor > 0 and ready == floor
                        else None
                    ),
                    "parent_floor_mu": floor,
                    "resources": [f"{kind}:{name}" for kind, name in sorted(resources)],
                },
            )
            result.append(shifted)
        for resource in resources:
            available[resource] = finish
        if synchronization or (not resources and duration):
            floor = finish
        end = max(end, finish)
        frames.update(local)
        if len(result) > scheduler.max_events:
            raise CompileError("ASAP Section exceeds per-point event budget")
    return result, end
