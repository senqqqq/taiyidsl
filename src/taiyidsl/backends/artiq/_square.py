"""Buffered single-tone scheduling: preparation never moves an output boundary."""

import copy
import math
from collections import defaultdict

from taiyidsl.backends.artiq._frames import carrier_phase_word, copy_carrier_reference
from taiyidsl.backends.artiq._nco import continuous_contract
from taiyidsl.backends.artiq._preparation import (
    ConfigurationRequest,
    place_configurations,
)
from taiyidsl.backends.artiq.submission import preparation_window
from taiyidsl.config.hardware import DeviceCalibration
from taiyidsl.ir import CompileError, ScheduledEvent


def lower_square(pulses, frames, setup, mapping, edges, ram_jobs, reservations):
    """Return timed Square events and resource-constrained preparation spans.

    Every shared update refreshes every participating channel: persistent phase
    autoclear would otherwise reset an unchanged channel to its old POW.
    """
    by_device = defaultdict(list)
    for pulse in pulses:
        by_device[pulse.signal].append(pulse)
    by_frame = defaultdict(list)
    for event in frames:
        by_frame[event.signal].append(event)
    for events in by_frame.values():
        events.sort(key=lambda e: e.start_mu)

    states, groups = {}, defaultdict(list)
    gates = []
    for name, sequence in sorted(by_device.items()):
        device = setup.devices[name]
        continuous = continuous_contract(device, setup)
        device_frames = {
            frame: [
                command
                for command in commands
                if name in command.params.get("target_devices", (name,))
            ]
            for frame, commands in by_frame.items()
        }
        latency = device.square_latency_mu
        if latency < math.ceil(79 / device.sysclk_hz / setup.tick_seconds):
            raise CompileError("square_latency_mu must cover 79 DDS clock cycles")
        if device.square_prepare_duration_mu < 156 * device.spi_ref_period_mu:
            raise CompileError("Square preparation budget is below real driver minimum")
        if device.square_prepare_duration_mu % device.config_grid_mu:
            raise CompileError(
                "Square preparation budget must respect configuration grid"
            )
        if 8 * setup.tick_seconds <= 4 / device.sysclk_hz:
            raise CompileError("8-mu Square commit must exceed the DDS SYNC_CLK period")
        cycles = device.sysclk_hz * setup.tick_seconds
        if not math.isclose(cycles, round(cycles), abs_tol=1e-8):
            raise CompileError("Square tracking requires integer DDS clocks per tick")
        sequence.sort(key=lambda e: e.start_mu)
        owners = {p.params.get("frame", p.params.get("qubit")) for p in sequence}
        if len(owners) > 1 and any(device_frames.get(owner) for owner in owners):
            raise CompileError("Hardware frames require unambiguous DDS ownership")
        group = device.update_group or f"device:{name}"
        groups[group].append(name)
        changes = {}
        for pulse in sequence:
            params = copy.deepcopy(pulse.params)
            # Transform a frame delta using exactly the binding used by play.
            frame = params.get("frame", params.get("qubit"))
            frame_scale = params.pop("_frame_scale", 1.0)
            base = params["phase"] - params.get("frame_phase", 0) * frame_scale
            cal = setup.calibration.get(name, DeviceCalibration())
            offset = device.latency_mu + cal.latency_mu
            current = 0.0
            for command in device_frames.get(frame, ()):
                if command.start_mu - offset <= pulse.start_mu:
                    current = command.params["phase"]
                    copy_carrier_reference(params, command)
            params["phase"] = (base + current * frame_scale) % (2 * math.pi)
            params["_base_phase"] = base
            params["_frame_scale"] = frame_scale
            changes[pulse.start_mu] = (params, pulse)
        # Commands update a held DDS state even when no later play follows.
        for frame, commands in device_frames.items():
            relevant = [
                p
                for p in sequence
                if p.params.get("frame", p.params.get("qubit")) == frame
            ]
            if not relevant:
                continue
            cal = setup.calibration.get(name, DeviceCalibration())
            offset = device.latency_mu + cal.latency_mu
            for command in commands:
                when = command.start_mu - offset
                if when in changes and changes[when][1].kind not in {
                    "shift_phase",
                    "reset_frame",
                }:
                    changes[when][0]["_explicit_frame"] = True
                    changes[when][0]["command_kind"] = command.params.get(
                        "command_kind", command.kind
                    )
                    continue  # The play already includes the final frame value.
                preceding = [t for t in changes if t <= when]
                base_params, _ = changes[max(preceding) if preceding else min(changes)]
                params = copy.deepcopy(base_params)
                copy_carrier_reference(params, command)
                if not preceding and continuous:
                    params["_base_phase"] = params.get("nco_initial_phase", 0.0)
                elif (
                    not preceding
                    and "virtual_z_drive_offset" in params
                    and not params.get("hardware_frame_resolved")
                ):
                    # A future virtual Z changes that future play's axis. It
                    # must not leak backwards into an earlier hardware command.
                    params["_base_phase"] -= (
                        params.get("virtual_z_drive_offset", 0) * params["_frame_scale"]
                    )
                    params["virtual_z_drive_offset"] = 0
                    params["virtual_z_phase"] = 0
                params["phase"] = (
                    params["_base_phase"]
                    + command.params["phase"] * params["_frame_scale"]
                ) % (2 * math.pi)
                params["command_kind"] = command.params.get(
                    "command_kind", command.kind
                )
                changes[when] = (params, command)
        # Identical plays need no new update; explicit phase commands always do.
        filtered, previous = {}, None
        for when, (params, source) in sorted(changes.items()):
            signature = tuple(
                params[k] for k in ("frequency", "phase", "amplitude")
            ) + (params.get("carrier_reference_mu"),)
            explicit = params.get("_explicit_frame", False) or source.kind in {
                "shift_phase",
                "reset_frame",
            }
            if signature != previous or explicit:
                commit = when - latency
                if commit % device.config_grid_mu:
                    earlier = commit // device.config_grid_mu * device.config_grid_mu
                    # A gated-off channel may settle before its RF-on edge.
                    # Live changes and explicit phase commands must be exact.
                    if explicit or any(
                        p.start_mu <= earlier + latency < p.end_mu for p in sequence
                    ):
                        raise CompileError(
                            "Square commit violates the configuration grid"
                        )
                    commit = earlier
                filtered[commit] = (params, source)
            previous = signature
        states[name] = filtered
        # RF continuity is independent of parameter continuity.
        start, end, ids, source = None, None, (), None
        for pulse in sequence:
            if pulse.duration_mu == 0:
                continue
            if start is not None and pulse.start_mu == end:
                end = pulse.end_mu
                ids += pulse.logical_ids
                continue
            if start is not None:
                gates.extend(_gate(name, start, end, source, ids))
            start, end, ids, source = (
                pulse.start_mu,
                pulse.end_mu,
                pulse.logical_ids,
                pulse.source,
            )
        if start is not None:
            gates.extend(_gate(name, start, end, source, ids))

    events, requests = list(gates), []
    all_edges = set(edges) | {e.start_mu for e in gates}
    for group, members in sorted(groups.items()):
        modes = {setup.devices[name].nco_mode for name in members}
        if len(modes) != 1:
            raise CompileError("A shared IO_UPDATE group cannot mix NCO phase policies")
        continuous = modes == {"continuous"}
        times = sorted({t for name in members for t in states[name]})
        previous_commit = None
        for when in times:
            if previous_commit is not None and when < previous_commit + 8:
                raise CompileError(
                    "Square IO_UPDATE pulses overlap; no gap was inserted"
                )
            all_edges.update((when, when + 8))
            for name in members:
                device = setup.devices[name]
                if continuous and when not in states[name]:
                    continue
                past = [t for t in states[name] if t <= when]
                params, source = states[name][max(past) if past else min(states[name])]
                params = copy.deepcopy(params)
                effect = when + device.square_latency_mu
                for rs, re, resources, _ in reservations:
                    if {name, group}.intersection(resources) and rs <= effect < re:
                        raise CompileError(
                            "Square effect overlaps an explicit resource reservation"
                        )
                ftw = round(params["frequency"] / device.sysclk_hz * 2**32)
                pow_ = round(params["phase"] / (2 * math.pi) * 2**16) % 2**16
                asf = round(params["amplitude"] * 0x3FFF)
                cycles = round(device.sysclk_hz * setup.tick_seconds)
                phase_word = carrier_phase_word(params, device, setup)
                params.update(
                    ftw=ftw,
                    pow=pow_,
                    asf=asf,
                    square_pow=(
                        phase_word >> 16
                        if continuous
                        else ((phase_word + effect * ftw * cycles) >> 16) & 0xFFFF
                    ),
                    carrier_phase_word_u32=phase_word,
                    nco_mode=device.nco_mode,
                    effect_mu=effect,
                    square_latency_mu=device.square_latency_mu,
                    ref_time_mu=0,
                )
                params.pop("_base_phase", None)
                params.pop("_frame_scale", None)
                params.pop("_explicit_frame", None)
                resources = tuple(
                    sorted({name, device.bus, group, "kernel_submit"} - {None})
                )
                prepare = ScheduledEvent(
                    "dds_prepare",
                    name,
                    when,
                    device.square_prepare_duration_mu,
                    params,
                    source.source,
                    resources,
                    source.logical_ids,
                )
                requests.append(
                    ConfigurationRequest(
                        prepare,
                        previous_commit + 8 if previous_commit is not None else None,
                    )
                )
                events.append(
                    ScheduledEvent(
                        "dds_effect",
                        name,
                        effect,
                        0,
                        copy.deepcopy(params),
                        source.source,
                        (name,),
                        source.logical_ids,
                    )
                )
            leader = members[0]
            commit_resources = set(members) | {group, "kernel_submit"}
            for rs, re, resources, _ in reservations:
                if (
                    commit_resources.intersection(resources)
                    and when < re
                    and rs < when + 8
                ):
                    raise CompileError(
                        "Square commit overlaps an explicit resource reservation"
                    )
            for kind, timestamp in (("dds_commit", when), ("dds_commit_off", when + 8)):
                events.append(
                    ScheduledEvent(
                        kind,
                        leader,
                        timestamp,
                        0,
                        {"commit_members": list(members)},
                        "Square group commit",
                        (group,),
                        (),
                    )
                )
            previous_commit = when
    # Metadata effect events are not submission edges. Physical TTL/IO_UPDATE
    # edges remain barriers for opaque blocking SPI calls.
    configs, _ = place_configurations(
        requests, [], setup, all_edges, ram_jobs, reservations
    )
    events.extend(configs)
    jobs = [preparation_window(e, setup) for e in configs]
    return events, jobs, all_edges


def _gate(name, start, end, source, ids):
    return [
        ScheduledEvent(f"dds_{kind}", name, timestamp, 0, {}, source, (name,), ids)
        for kind, timestamp in (("on", start), ("off", end))
    ]
