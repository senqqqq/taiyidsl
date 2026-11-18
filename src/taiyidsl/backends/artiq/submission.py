"""Offline checks for planned RTIO submission, separate from output timing.

These checks model the known driver writes and a non-spreading SED. They do not
prove CPU/FIFO throughput or replace real target channel and clock validation.
"""

from bisect import bisect_right
from dataclasses import dataclass

from taiyidsl.ir import CompileError


def is_buffered(event):
    """Only these writes can overlap unrelated events without changing output."""
    return event.kind == "dds_prepare" or (
        event.kind == "dds_ram_phase" and event.params.get("ram_boundary", False)
    )


def is_scheduled_spi(event):
    """Known SPI calls permit unrelated RTIO submissions during transmission.

    Profile selection itself triggers RAM, so it is not a buffered DDS write
    waiting for a later IO_UPDATE.
    """
    return is_buffered(event) or event.kind == "dds_polar_select"


@dataclass(frozen=True)
class PreparationWindow:
    start_mu: int
    end_mu: int
    resources: tuple
    label: str
    buffered_spi: bool
    bus: str | None

    def __iter__(self):
        # Keep existing reservation/report consumers compatible.
        return iter((self.start_mu, self.end_mu, self.resources, self.label))


def preparation_window(event, setup):
    device = setup.devices[event.signal]
    return PreparationWindow(
        event.start_mu,
        event.end_mu,
        event.resources,
        f"{event.kind} preparation",
        is_scheduled_spi(event),
        device.bus,
    )


def validate_sed(timestamps, *, coarse_mu=8, lane_count=8):
    """Simulate timestamp-based lane selection in actual submission order.

    Initial lane timestamps are before the shot. Event spreading and arbitrary
    gateware channel latency compensation require separate target validation.
    """
    if type(coarse_mu) is not int or coarse_mu <= 0:
        raise ValueError("coarse_mu must be a positive integer")
    if type(lane_count) is not int or lane_count <= 0:
        raise ValueError("lane_count must be a positive integer")
    ends, lane, previous = [None] * lane_count, 0, None
    for timestamp in timestamps:
        coarse = int(timestamp) // coarse_mu
        if previous is not None and coarse <= previous:
            lane = (lane + 1) % lane_count
        if ends[lane] is not None and coarse <= ends[lane]:
            raise CompileError(
                "RTIO submission order exceeds declared SED lane capacity; "
                "no output was staggered"
            )
        ends[lane] = previous = coarse


def validate_channel_order(records, *, coarse_mu=8):
    """Reject reversed or colliding submissions to one physical RTIO channel."""
    previous = {}
    for timestamp, channel in records:
        coarse = int(timestamp) // coarse_mu
        if channel in previous and coarse <= previous[channel]:
            raise CompileError(
                f"RTIO channel {channel} has reversed or colliding submissions"
            )
        previous[channel] = coarse


def ordered_rtio_events(events, setup):
    """Expand known timed driver calls, without sorting their internal writes.

    Opaque legacy configuration/rearm calls retain global scheduling barriers;
    their internal writes are checked by the installed real-driver audit.
    """
    records = []
    for event in events:
        device = setup.devices[event.signal]
        start, kind = event.start_mu, event.kind
        bus = ("spi", device.bus or f"unknown:{device.name}")
        update = ("update", device.update_group or device.name)
        if kind == "dds_prepare":
            records.extend(
                (start + offset * device.spi_ref_period_mu, bus)
                for offset in (0, 1, 20, 21, 88, 89)
            )
        elif kind == "dds_ram_phase" and is_buffered(event):
            records.extend(((start, bus), (start + device.spi_ref_period_mu, bus)))
        elif kind == "dds_polar_select":
            offsets = (
                (0, 1)
                if device.ram_profile_control == "urukul_rev8"
                else (0, 1, 52, 53)
            )
            records.extend(
                (start + offset * device.spi_ref_period_mu, bus) for offset in offsets
            )
        elif kind in {
            "dds_commit",
            "dds_commit_off",
            "dds_ram_commit_off",
            "dds_nco_epoch",
            "dds_nco_epoch_off",
            "dds_polar_retrigger",
            "dds_polar_commit_off",
        }:
            records.append((start, update))
        elif kind == "dds_ram_start":
            records.append((start, update))
            if not event.params.get("ram_boundary"):
                records.append((start + 8, update))
        elif kind in {"dds_on", "dds_off", "ttl_on", "ttl_off"}:
            records.append((start, ("switch", device.name)))
    return records


def validate_submission_plan(events, setup):
    """Validate certified rewinds, bus occupancy and buffered commit deadlines."""
    events = list(events)
    writes = []
    opaque = []
    commits = {}
    instructions = []
    previous_start, opaque_end = None, None
    for event in events:
        if event.kind in {"dds_effect", "dds_ram_effect", "dds_polar_effect"}:
            continue
        if type(event.start_mu) is not int or type(event.duration_mu) is not int:
            raise CompileError("Submission timestamps and durations must be integer mu")
        if event.duration_mu < 0:
            raise CompileError("Submission duration must be nonnegative")
        if previous_start is not None and event.start_mu < previous_start:
            raise CompileError("Instructions must be submitted in planned start order")
        if opaque_end is not None and event.start_mu < opaque_end:
            raise CompileError("An instruction overlaps an opaque driver submission")
        previous_start = event.start_mu
        instructions.append(event)
        if event.kind == "dds_polar_select":
            device = setup.devices[event.signal]
            cycles = 52 if device.ram_profile_control == "urukul_rev8" else 112
            if event.duration_mu != cycles * device.spi_ref_period_mu:
                raise CompileError(
                    "POWASF profile selector duration differs from driver"
                )
            if event.start_mu % device.config_grid_mu:
                raise CompileError("POWASF profile selector is off the SPI time grid")
            p = event.params
            if "effect_mu" in p and (
                event.end_mu + device.ram_profile_latency_mu != p["effect_mu"]
            ):
                raise CompileError("POWASF profile selection has the wrong effect time")
        if is_scheduled_spi(event):
            writes.append(event)
        elif event.duration_mu:
            opaque.append(event)
            opaque_end = event.end_mu
        if event.kind in {
            "dds_commit",
            "dds_ram_start",
            "dds_nco_epoch",
            "dds_polar_retrigger",
        }:
            for member in event.params.get("commit_members", [event.signal]):
                commits.setdefault(member, []).append(event.start_mu)

    # Opaque calls remain barriers even when another SPI bus is idle. Metadata
    # effects are excluded: they describe hardware evolution, not submissions.
    for call in opaque:
        if any(call.start_mu < event.start_mu < call.end_mu for event in instructions):
            raise CompileError("An instruction overlaps an opaque driver submission")
        if any(w.start_mu < call.end_mu and call.start_mu < w.end_mu for w in writes):
            raise CompileError(
                "Buffered preparation overlaps an opaque driver submission"
            )

    busy = {}
    unknown = []
    for call in writes:
        bus = setup.devices[call.signal].bus
        if bus is None:
            unknown.append(call)
        else:
            busy.setdefault(bus, []).append(call)
    for bus, calls in busy.items():
        calls.sort(key=lambda call: call.start_mu)
        for left, right in zip(calls, calls[1:]):
            if left.end_mu > right.start_mu:
                raise CompileError(f"SPI bus {bus} has overlapping preparation calls")
    for call in unknown:
        if any(
            other is not call
            and call.start_mu < other.end_mu
            and other.start_mu < call.end_mu
            for other in writes
        ):
            raise CompileError("Unknown SPI bus cannot overlap another preparation")

    per_device = {}
    for call in writes:
        if not is_buffered(call):
            # A selector changes active RAM state at the declared call-end
            # reference. An update during that SPI call has ambiguous ordering.
            if any(
                call.start_mu < t < call.end_mu for t in commits.get(call.signal, [])
            ):
                raise CompileError(
                    "IO_UPDATE overlaps an active POWASF profile selector"
                )
            continue
        times = commits.get(call.signal, [])
        index = bisect_right(times, call.start_mu)
        if index == len(times):
            raise CompileError("Buffered DDS preparation has no following commit")
        commit = times[index]
        if call.end_mu > commit:
            raise CompileError("IO_UPDATE occurs before buffered SPI data is ready")
        p = call.params
        if "effect_mu" in p:
            latency = (
                setup.devices[call.signal].square_latency_mu
                if call.kind == "dds_prepare"
                else p["ram_latency_mu"]
            )
            if p["effect_mu"] - latency != commit:
                raise CompileError(
                    "Buffered state would be consumed by the wrong commit"
                )
        previous = per_device.get(call.signal)
        if previous is not None and call.start_mu < previous:
            raise CompileError("Buffered DDS state is overwritten before its commit")
        per_device[call.signal] = commit

    records = ordered_rtio_events(events, setup)
    validate_channel_order(records, coarse_mu=setup.rtio_coarse_mu)
    validate_sed(
        (timestamp for timestamp, _ in records),
        coarse_mu=setup.rtio_coarse_mu,
        lane_count=setup.rtio_lane_count,
    )
