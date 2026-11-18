"""Indexed, conservative placement of blocking ARTIQ DDS configuration calls."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import accumulate

from taiyidsl.backends.artiq.submission import is_buffered
from taiyidsl.ir import CompileError, ScheduledEvent


@dataclass
class ConfigurationRequest:
    # Until placement succeeds, event.start_mu is the output's deadline.
    event: ScheduledEvent
    earliest_mu: int | None


class _IntervalIndex:
    """Find the earliest-starting overlap, retaining nested interval boundaries."""

    def __init__(self, spans):
        spans = sorted(spans)
        self.starts = [start for start, _ in spans]
        self.prefix_ends = list(accumulate((end for _, end in spans), max))

    def blocker(self, start, end):
        index = bisect_right(self.prefix_ends, start)
        if index < len(self.starts) and self.starts[index] < end:
            return self.starts[index]
        return None


class _SubmissionIndex:
    """Mutable index of mutually exclusive RAM/configuration submission spans.

    Searches are logarithmic. Insertion uses Python lists, so arbitrary insertions
    still have linear movement cost; this is not an interval-tree scheduler.
    """

    def __init__(self, spans):
        spans = sorted(spans)
        self.starts = [start for start, _ in spans]
        self.ends = [end for _, end in spans]
        self.prefix_ends = list(accumulate(self.ends, max))

    def blocker(self, start, end):
        index = bisect_right(self.prefix_ends, start)
        if index < len(self.starts) and self.starts[index] < end:
            return self.starts[index]
        return None

    def add(self, start, end):
        index = bisect_left(self.starts, start)
        self.starts.insert(index, start)
        self.ends.insert(index, end)
        self.prefix_ends = list(accumulate(self.ends, max))


def place_configurations(requests, pulses, setup, edges, ram_jobs, reservations):
    """Preserve successful legacy placements, retry tied deadlines on failure.

    The bounded retry gives the latest slot to a channel whose previous pulse
    ends later. It is a heuristic, not a complete feasibility search. Neither
    attempt changes output timing or relaxes shared-resource constraints.
    """
    if not requests:
        return [], False
    output_spans = {}
    for pulse in pulses:
        device = setup.devices[pulse.signal]
        span = (pulse.start_mu, pulse.end_mu)
        output_spans.setdefault(("device", device.name), []).append(span)
        if device.update_group:
            output_spans.setdefault(("group", device.update_group), []).append(span)
    output_indices = {key: _IntervalIndex(spans) for key, spans in output_spans.items()}
    reserved_spans = {}
    for start, end, resources, _ in reservations:
        for resource in resources:
            reserved_spans.setdefault(resource, []).append((start, end))
    reserved_indices = {
        key: _IntervalIndex(spans) for key, spans in reserved_spans.items()
    }
    sorted_edges = sorted(edges)
    fixed_submissions = list(ram_jobs)
    # Build only the relevant index list once per device, rather than per retry.
    indices_by_device = {}
    for request in requests:
        event = request.event
        key = (event.signal, is_buffered(event))
        if key in indices_by_device:
            continue
        device = setup.devices[event.signal]
        indices_by_device[key] = [
            output_indices[key]
            for key in (("device", device.name), ("group", device.update_group))
            if key in output_indices and not is_buffered(event)
        ] + [
            reserved_indices[key] for key in event.resources if key in reserved_indices
        ]

    def attempt(ordered):
        all_submissions, opaque, by_bus = _SubmissionIndex([]), _SubmissionIndex([]), {}
        for job in fixed_submissions:
            start, end, _, _ = job
            all_submissions.add(start, end)
            bus = getattr(job, "bus", None)
            if getattr(job, "buffered_spi", False) and bus is not None:
                by_bus.setdefault(bus, _SubmissionIndex([])).add(start, end)
            else:
                opaque.add(start, end)
        starts = {}
        for index in ordered:
            request = requests[index]
            event = request.event
            device = setup.devices[event.signal]
            buffered = is_buffered(event)
            submissions = (
                (opaque, by_bus.setdefault(device.bus, _SubmissionIndex([])))
                if buffered and device.bus is not None
                else (all_submissions,)
            )
            finish = event.start_mu
            while True:
                start = (
                    (finish - event.duration_mu) // device.config_grid_mu
                ) * device.config_grid_mu
                finish = start + event.duration_mu
                if request.earliest_mu is not None and start < request.earliest_mu:
                    raise CompileError(
                        f"No legal DDS preparation window on {device.name}; "
                        "strict adjacency/short delay cannot accommodate "
                        "parameter update"
                    )
                blockers = [
                    boundary
                    for intervals in (
                        *submissions,
                        *indices_by_device[(event.signal, buffered)],
                    )
                    if (boundary := intervals.blocker(start, finish)) is not None
                ]
                edge_index = bisect_right(sorted_edges, start)
                if (
                    not buffered
                    and edge_index < len(sorted_edges)
                    and sorted_edges[edge_index] < finish
                ):
                    blockers.append(sorted_edges[edge_index])
                if not blockers:
                    break
                finish = min(blockers)
            all_submissions.add(start, finish)
            if buffered and device.bus is not None:
                by_bus[device.bus].add(start, finish)
            else:
                opaque.add(start, finish)
            starts[index] = start
        return starts

    original_order = list(range(len(requests)))
    reordered = False
    try:
        starts = attempt(original_order)
    except CompileError as original_error:
        retry_order = sorted(
            original_order,
            key=lambda index: (
                requests[index].event.start_mu,
                -requests[index].earliest_mu
                if requests[index].earliest_mu is not None
                else float("inf"),
                requests[index].event.signal,
            ),
        )
        if retry_order == original_order:
            raise
        try:
            starts = attempt(retry_order)
        except CompileError:
            raise original_error from None
        reordered = True
    for index, request in enumerate(requests):
        request.event.start_mu = starts[index]
    return [request.event for request in requests], reordered
