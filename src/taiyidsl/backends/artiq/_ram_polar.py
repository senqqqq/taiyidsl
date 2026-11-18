"""Optional resident POWASF RAM with strict, compensated phase boundaries.

Only the phase samples change: every played ASF word is retained verbatim.
Profile selection is a real CPLD SPI operation, not an instantaneous command.
"""

import copy
import math

from taiyidsl.backends.artiq._frames import carrier_phase_word, copy_carrier_reference
from taiyidsl.backends.artiq._nco import continuous_contract
from taiyidsl.backends.artiq.submission import preparation_window
from taiyidsl.config.hardware import DeviceCalibration
from taiyidsl.ir import CompileError, ScheduledEvent


def _pow(phase):
    return round(phase / (2 * math.pi) * 65536) & 0xFFFF


def _states(original, frames, device, setup):
    """Apply resolved frames, retaining independent and terminal commands."""
    sequence = copy.deepcopy(sorted(original, key=lambda p: p.start_mu))
    owners = {p.params.get("frame", p.params.get("qubit")) for p in sequence}
    commands = [
        f
        for f in frames
        if f.signal in owners and device.name in f.params["target_devices"]
    ]
    if len(owners) > 1 and commands:
        raise CompileError("Hardware frames require unambiguous DDS ownership")
    offset = (
        device.latency_mu
        + setup.calibration.get(device.name, DeviceCalibration()).latency_mu
    )
    by_time = {f.start_mu - offset: f for f in commands}
    ids_by_time = {}
    for command in commands:
        when = command.start_mu - offset
        ids_by_time[when] = ids_by_time.get(when, ()) + command.logical_ids
    for pulse in sequence:
        p = pulse.params
        scale = p.get("_frame_scale", 1.0)
        base = p["phase"] - p.get("frame_phase", 0) * scale
        p["_base_phase"] = base
        preceding = [when for when in by_time if when <= pulse.start_mu]
        if preceding:
            command = by_time[max(preceding)]
            p["phase"] = (base + command.params["phase"] * scale) % (2 * math.pi)
            copy_carrier_reference(p, command)
    gates = [p for p in sequence if p.duration_mu]
    result = list(gates)
    for when, command in sorted(by_time.items()):
        if any(p.start_mu < when < p.end_mu for p in gates):
            raise CompileError(
                "POWASF hardware frame updates require a gate boundary "
                "or an RF-off interval"
            )
        matching = [p for p in gates if p.start_mu == when]
        if matching:
            for pulse in matching:
                pulse.params["command_kind"] = command.params["command_kind"]
                pulse.logical_ids += ids_by_time[when]
            continue
        preceding = [p for p in gates if p.start_mu <= when]
        state = max(preceding, key=lambda p: p.start_mu) if preceding else sequence[0]
        params = copy.deepcopy(state.params)
        copy_carrier_reference(params, command)
        base = (
            params["_base_phase"] if preceding else params.get("nco_initial_phase", 0.0)
        )
        params["phase"] = (
            base + command.params["phase"] * params.get("_frame_scale", 1.0)
        ) % (2 * math.pi)
        params.update(
            polar_frame_only=True,
            command_kind=command.params["command_kind"],
        )
        result.append(
            ScheduledEvent(
                "dds",
                device.name,
                when,
                0,
                params,
                command.source,
                (device.name,),
                ids_by_time[when],
            )
        )
    return sorted(result, key=lambda p: p.start_mu)


def _contract(device, setup):
    if not continuous_contract(device, setup):
        raise CompileError("POWASF RAM requires nco_mode='continuous'")
    fields = (
        "ram_latency_mu",
        "ram_polar_phase_latency_mu",
        "ram_profile_latency_mu",
        "ram_profile_phase_latency_mu",
    )
    if any(getattr(device, field) is None for field in fields):
        raise CompileError("POWASF RAM requires explicit " + ", ".join(fields))
    la, lp, sa, sp = (getattr(device, field) for field in fields)
    if la < math.ceil(106 / device.sysclk_hz / setup.tick_seconds) or lp < math.ceil(
        94 / device.sysclk_hz / setup.tick_seconds
    ):
        raise CompileError(
            "POWASF matched RAM IO_UPDATE latencies must cover at least "
            "106 envelope and 94 phase SYSCLK cycles"
        )
    if la < lp or sa < sp or la - lp != sa - sp:
        raise CompileError(
            "POWASF requires equal nonnegative envelope-minus-phase latency "
            "for IO_UPDATE and profile selection"
        )
    if 8 * setup.tick_seconds <= 4 / device.sysclk_hz:
        raise CompileError("8-mu RAM commit must exceed the DDS SYNC_CLK period")
    duration = (52 if device.ram_profile_control == "urukul_rev8" else 112) * (
        device.spi_ref_period_mu
    )
    return la, lp, sa, sp, duration


def lower_ram_polar(pulses, frames, setup, edges, reservations, diagnostics):
    """Allocate a bounded transition library without changing gate boundaries."""
    grouped = {}
    for pulse in pulses:
        if setup.devices[pulse.signal].ram_destination == "powasf":
            grouped.setdefault(pulse.signal, []).append(pulse)
    events, jobs, all_edges = [], [], set(edges)
    active_dds = {p.signal for p in pulses if setup.devices[p.signal].kind == "dds"}
    for name, original in sorted(grouped.items()):
        device = setup.devices[name]
        la, lp, sa, sp, select_duration = _contract(device, setup)
        if any(p.duration_mu and "ram_asf" not in p.params for p in original):
            raise CompileError(
                "POWASF supports shaped RAM pulses; Square/RAM mixing is unsupported"
            )
        if any(
            other != name
            and device.update_group
            and setup.devices[other].update_group == device.update_group
            for other in active_dds
        ):
            raise CompileError("POWASF requires an exclusive active IO_UPDATE group")
        if device.ram_profile_control == "urukul_rev8" and any(
            other != name
            and (device.bus is None or setup.devices[other].bus in {None, device.bus})
            for other in active_dds
        ):
            raise CompileError(
                "Rev8 shared profile pins require an exclusive active DDS "
                "on the SPI bus"
            )
        sequence = _states(original, frames, device, setup)
        if not sequence:
            continue
        ftws = {
            round(p.params["frequency"] / device.sysclk_hz * 2**32) for p in sequence
        }
        if len(ftws) != 1:
            raise CompileError("POWASF continuous NCO requires fixed FTW per shot")
        ftw = next(iter(ftws))
        initial = _pow(sequence[0].params.get("nco_initial_phase", 0.0))
        bank = [
            dict(
                profile=0,
                start=0,
                end=0,
                step=1,
                mode=0,
                asf=[0],
                pow_samples=[initial],
                phase_targets_u32=[initial << 16],
                words=[initial << 16],
            )
        ]
        keys, total_words, current_profile, incoming = {}, 1, 0, initial << 16
        previous, previous_commit, prior_select_end = None, None, None
        resources = tuple(
            sorted({name, device.bus, device.update_group, "kernel_submit"} - {None})
        )
        device_events = []
        for pulse in sequence:
            p = copy.deepcopy(pulse.params)
            frame_only = p.get("polar_frame_only", False)
            target = carrier_phase_word(p, device, setup)
            if frame_only and target == incoming:
                continue
            step = p.get("ram_step", 1)
            interval = 4 * step / device.sysclk_hz / setup.tick_seconds
            prefix = (la - lp) / interval
            if not math.isclose(prefix, round(prefix), abs_tol=1e-9):
                raise CompileError(
                    "POWASF phase compensation must be an integer original "
                    "sample interval; "
                    "the envelope was not resampled"
                )
            prefix = round(prefix)
            asf = [0] * (prefix + 1) if frame_only else list(p["ram_asf"])
            if prefix >= len(asf):
                raise CompileError("POWASF phase compensation exceeds the pulse length")
            phase_targets = [incoming] * prefix + [target] * (len(asf) - prefix)
            # Equality is based on actual samples: identical axis calls reuse
            # one entry, while an X->Y transition keeps its required old prefix.
            key = (tuple(asf), step, tuple(phase_targets))
            if key not in keys:
                if len(bank) >= 8 or total_words + len(asf) > 1024:
                    raise CompileError(
                        "POWASF resident bank exceeds 8 profiles or 1024 words"
                    )
                profile = len(bank)
                phases = [phase >> 16 for phase in phase_targets]
                bank.append(
                    dict(
                        profile=profile,
                        start=total_words,
                        end=total_words + len(asf) - 1,
                        step=step,
                        mode=1,
                        asf=asf,
                        pow_samples=phases,
                        phase_targets_u32=phase_targets,
                        words=[
                            phase << 16 | amplitude << 2
                            for phase, amplitude in zip(phases, asf)
                        ],
                    )
                )
                keys[key] = profile
                total_words += len(asf)
            profile = keys[key]
            select = profile != current_profile
            amplitude_latency, phase_latency = (sa, sp) if select else (la, lp)
            commit = pulse.start_mu - amplitude_latency
            start = commit - select_duration if select else commit
            grid = math.lcm(device.config_grid_mu, setup.rtio_coarse_mu)
            if start % grid or commit % grid:
                raise CompileError(
                    "POWASF selection/commit cannot meet the exact RTIO grid"
                )
            if previous is not None:
                # The new prefix is an actual phase output, including with RF
                # off. It must not expose a future Rz before its own boundary.
                if pulse.start_mu - (la - lp) < previous.start_mu:
                    raise CompileError(
                        "POWASF next transition would expose a future phase "
                        "or truncate the preceding phase effect"
                    )
                if commit <= previous_commit:
                    raise CompileError(
                        "POWASF trigger order cannot preserve requested "
                        "output boundaries"
                    )
            if select and prior_select_end is not None and start < prior_select_end:
                raise CompileError(
                    f"POWASF profile selection needs {select_duration} mu "
                    f"on {device.ram_profile_control}; "
                    "the requested gate spacing exceeds SPI throughput"
                )
            if (
                not select
                and previous_commit is not None
                and commit - previous_commit < 8 + setup.rtio_coarse_mu
            ):
                raise CompileError(
                    "POWASF retrigger requires an IO_UPDATE low coarse cycle"
                )
            p.update(
                ftw=ftw,
                pow=_pow(p["phase"]),
                ram_pow=target >> 16,
                carrier_phase_word_u32=target,
                incoming_pow=incoming >> 16,
                incoming_phase_word_u32=incoming,
                polar_profile=profile,
                polar_initial_profile=0,
                polar_frame_only=frame_only,
                polar_phase_prefix_samples=prefix,
                ram_destination="powasf",
                polar_commit_mu=commit,
                effect_mu=pulse.start_mu,
                carrier_effect_mu=pulse.start_mu,
                polar_amplitude_effect_mu=pulse.start_mu,
                polar_phase_latency_mu=phase_latency,
                polar_amplitude_latency_mu=amplitude_latency,
                ram_sample_interval_s=interval * setup.tick_seconds,
                ref_time_mu=0,
            )
            kind = "dds_polar_select" if select else "dds_polar_retrigger"
            event = ScheduledEvent(
                kind,
                name,
                start,
                select_duration if select else 0,
                dict(p),
                pulse.source,
                resources,
                pulse.logical_ids,
            )
            device_events.append(event)
            if select:
                jobs.append(preparation_window(event, setup))
                prior_select_end = commit
            else:
                device_events.append(
                    ScheduledEvent(
                        "dds_polar_commit_off",
                        name,
                        commit + 8,
                        0,
                        {},
                        pulse.source,
                        (device.update_group or name,),
                        pulse.logical_ids,
                    )
                )
            device_events.append(
                ScheduledEvent(
                    "dds_polar_effect",
                    name,
                    pulse.start_mu,
                    pulse.duration_mu,
                    dict(p),
                    pulse.source,
                    (name,),
                    pulse.logical_ids,
                )
            )
            for rs, re, rr, _ in reservations:
                if set(resources).intersection(rr) and (
                    start < re
                    and rs < commit + (0 if select else 8)
                    or rs <= commit < re
                    or rs <= pulse.start_mu < re
                ):
                    raise CompileError(
                        "POWASF transition overlaps an explicit resource reservation"
                    )
            current_profile, incoming = profile, target
            previous, previous_commit = pulse, commit
        for event in device_events:
            if event.kind != "dds_polar_commit_off":
                event.params["polar_bank"] = bank
        events.extend(device_events)
        spans = []
        for pulse in sorted(original, key=lambda p: p.start_mu):
            if not pulse.duration_mu:
                continue
            if spans and spans[-1][1] == pulse.start_mu:
                spans[-1][1] = pulse.end_mu
                spans[-1][2] += pulse.logical_ids
            else:
                spans.append([pulse.start_mu, pulse.end_mu, pulse.logical_ids])
        for start, end, ids in spans:
            for kind, when in (("dds_on", start), ("dds_off", end)):
                events.append(
                    ScheduledEvent(
                        kind, name, when, 0, {}, "POWASF RF gate", (name,), ids
                    )
                )
                all_edges.add(when)
        diagnostics.append(
            dict(
                kind="ram_polar_bank",
                device=name,
                profiles=len(bank),
                words=total_words,
                plays=sum(bool(p.duration_mu) for p in sequence),
                frame_updates=sum(
                    p.params.get("polar_frame_only", False) for p in sequence
                ),
                phase_compensation_mu=la - lp,
                profile_selection_duration_mu=select_duration,
                status="assumed_not_measured",
            )
        )
    return events, jobs, all_edges
