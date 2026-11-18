"""Resident ASF replay with separate carrier and envelope output contracts."""

import copy
import math

from taiyidsl.backends.artiq._frames import carrier_phase_word, copy_carrier_reference
from taiyidsl.backends.artiq._nco import continuous_contract
from taiyidsl.backends.artiq._preparation import (
    ConfigurationRequest,
    place_configurations,
)
from taiyidsl.backends.artiq.submission import preparation_window
from taiyidsl.config.hardware import DeviceCalibration
from taiyidsl.ir import CompileError, ScheduledEvent


def _zero_output(sequence, start, end, tick):
    """Prove zero over half-open held, quantized ASF sample intervals."""
    for pulse in sequence:
        left, right = max(start, pulse.start_mu), min(end, pulse.end_mu)
        if left >= right:
            continue
        dt = pulse.params["ram_sample_interval_s"] / tick
        first = max(0, math.floor((left - pulse.start_mu) / dt + 1e-9))
        stop = min(
            len(pulse.params["ram_asf"]),
            math.ceil((right - pulse.start_mu) / dt - 1e-9),
        )
        if any(pulse.params["ram_asf"][first:stop]):
            return False
    return True


def _with_frames(sequence, frames, device, setup):
    """Merge boundary commands; synthesize real RF-off updates elsewhere."""
    result = copy.deepcopy(sorted(sequence, key=lambda p: p.start_mu))
    owners = {p.params.get("frame", p.params.get("qubit")) for p in result}
    commands = sorted(
        (
            f
            for f in frames
            if f.signal in owners
            and device.name in f.params.get("target_devices", (device.name,))
        ),
        key=lambda f: f.start_mu,
    )
    if len(owners) > 1 and commands:
        raise CompileError("Hardware frames require unambiguous DDS ownership")
    offset = (
        device.latency_mu
        + setup.calibration.get(device.name, DeviceCalibration()).latency_mu
    )
    for pulse in result:
        scale = pulse.params.pop("_frame_scale", 1.0)
        base = pulse.params["phase"] - pulse.params.get("frame_phase", 0) * scale
        pulse.params["_base_phase"], pulse.params["_frame_scale"] = base, scale
        before = [f for f in commands if f.start_mu - offset <= pulse.start_mu]
        if before:
            pulse.params["phase"] = (base + before[-1].params["phase"] * scale) % (
                2 * math.pi
            )
            copy_carrier_reference(pulse.params, before[-1])
    by_time = {f.start_mu - offset: f for f in commands}
    for when, command in sorted(by_time.items()):
        if any(p.start_mu < when < p.end_mu for p in sequence):
            raise CompileError(
                "RAM hardware frame updates require a gate boundary or RF-off interval"
            )
        matching = [
            p
            for p in result
            if p.start_mu == when and not p.params.get("ram_frame_only")
        ]
        if matching:
            for pulse in matching:
                pulse.params["_explicit_frame"] = True
                pulse.params["command_kind"] = command.params.get(
                    "command_kind", command.kind
                )
            continue
        lp = (
            device.ram_pow_latency_mu
            if device.nco_mode == "continuous"
            else device.ram_phase_latency_mu
        )
        restart = when + device.ram_latency_mu - lp
        if restart < when and any(
            p.start_mu < when and restart < p.end_mu for p in sequence
        ):
            raise CompileError("RAM frame restart would alter an active gate envelope")
        if any(
            when < p.start_mu < restart + 8 + setup.rtio_coarse_mu for p in sequence
        ):
            raise CompileError(
                "RAM frame restart would overlap the next gate; "
                "the two IO_UPDATE commits cannot preserve both requested boundaries"
            )
        preceding = [p for p in result if p.start_mu <= when]
        state = max(preceding, key=lambda p: p.start_mu) if preceding else result[0]
        params = copy.deepcopy(state.params)
        copy_carrier_reference(params, command)
        if not preceding and device.nco_mode == "continuous":
            params["_base_phase"] = params.get("nco_initial_phase", 0.0)
        elif (
            not preceding
            and "virtual_z_drive_offset" in params
            and not params.get("hardware_frame_resolved")
        ):
            params["_base_phase"] -= (
                params.get("virtual_z_drive_offset", 0) * params["_frame_scale"]
            )
            params["virtual_z_drive_offset"] = 0
            params["virtual_z_phase"] = 0
        params["phase"] = (
            params["_base_phase"] + command.params["phase"] * params["_frame_scale"]
        ) % (2 * math.pi)
        params["ram_frame_only"] = True
        params["_explicit_frame"] = True
        params["command_kind"] = command.params.get("command_kind", command.kind)
        # A carrier-only command uses its actual carrier effect as the deadline.
        result.append(
            ScheduledEvent(
                "dds",
                device.name,
                restart,
                0,
                params,
                command.source,
                (device.name,),
                command.logical_ids,
            )
        )
    return sorted(result, key=lambda p: p.start_mu)


def lower_ram_boundary(
    pulses, frames, setup, edges, fixed_jobs, reservations, diagnostics
):
    """Keep waveform memory bounded and never manufacture an inter-gate gap.

    Legacy tracking retains its recorded zero-tail contract. Continuous NCO
    never advances a phase change into an active gate, including its zero tail.
    """
    by_device = {}
    for pulse in pulses:
        if "ram_asf" in pulse.params and (
            setup.devices[pulse.signal].ram_phase_latency_mu is not None
            or setup.devices[pulse.signal].nco_mode == "continuous"
        ):
            by_device.setdefault(pulse.signal, []).append(pulse)
    events, requests, all_edges = [], [], set(edges)
    for name, original in sorted(by_device.items()):
        device = setup.devices[name]
        continuous = continuous_contract(device, setup)
        la = device.ram_latency_mu
        lp = device.ram_pow_latency_mu if continuous else device.ram_phase_latency_mu
        if continuous and lp is None:
            raise CompileError(
                "Continuous RAM requires explicit ram_pow_latency_mu; "
                "legacy POW/autoclear latency cannot be reused"
            )
        budget, rearm = (
            device.ram_phase_update_duration_mu,
            device.ram_rearm_duration_mu,
        )
        if la is None or rearm is None or budget is None:
            raise CompileError(
                "RAM boundary mode requires ram_latency_mu, "
                "ram_rearm_duration_mu and ram_phase_update_duration_mu"
            )
        if la < math.ceil(106 / device.sysclk_hz / setup.tick_seconds):
            raise CompileError(
                "RAM boundary mode requires matched-RAM envelope latency "
                "of at least 106 DDS cycles"
            )
        if not continuous and not 0 < lp <= la:
            raise CompileError(
                "ram_phase_latency_mu must be positive and <= ram_latency_mu"
            )
        if any(value % device.config_grid_mu for value in (la, budget, rearm)):
            raise CompileError(
                "RAM boundary timing must align to DDS configuration grid"
            )
        if budget < 52 * device.spi_ref_period_mu:
            raise CompileError(
                "RAM phase preparation budget is below real driver minimum"
            )
        if 8 * setup.tick_seconds <= 4 / device.sysclk_hz:
            raise CompileError("8-mu RAM commit must exceed the DDS SYNC_CLK period")
        if any(p.signal == name and "ram_asf" not in p.params for p in pulses) or any(
            p.signal != name
            and device.update_group
            and setup.devices[p.signal].update_group == device.update_group
            for p in pulses
        ):
            raise CompileError(
                "RAM playback requires an exclusive active IO_UPDATE group; "
                "mixing Square/RAM is unsupported"
            )
        cycles = device.sysclk_hz * setup.tick_seconds
        if not math.isclose(cycles, round(cycles), abs_tol=1e-8):
            raise CompileError(
                "RAM phase tracking requires integer DDS cycles per target tick"
            )
        cycles = round(cycles)
        sequence = _with_frames(original, frames, device, setup)
        preload_phase = (
            sequence[0].params.get("nco_initial_phase", 0.0)
            if sequence[0].params.get("_explicit_frame")
            else sequence[0].params["phase"]
        )
        bank, words, previous = {}, 0, None
        resources = tuple(
            sorted({name, device.bus, device.update_group, "kernel_submit"} - {None})
        )
        actions = dict.fromkeys(("first", "phase", "retrigger", "rearm"), 0)
        for pulse in sequence:
            p = copy.deepcopy(pulse.params)
            frame_only = p.get("ram_frame_only", False)
            explicit = p.pop("_explicit_frame", False)
            key = (tuple(p["ram_asf"]), p["ram_step"])
            if key not in bank:
                if len(bank) >= 8 or words + len(key[0]) > 1024:
                    raise CompileError(
                        f"Resident RAM bank on {name} exceeds 8 profiles or 1024 words"
                    )
                bank[key] = (len(bank), words, words + len(key[0]) - 1)
                words += len(key[0])
            profile, start, end = bank[key]
            launch = pulse.start_mu - la
            carrier = launch + lp
            if launch % device.config_grid_mu:
                raise CompileError("RAM commit violates the configuration grid")
            ftw = round(p["frequency"] / device.sysclk_hz * 2**32)
            pow_ = round(p["phase"] / (2 * math.pi) * 2**16) % 2**16
            phase_word = carrier_phase_word(p, device, setup)
            actual_pow = (
                phase_word >> 16
                if continuous
                else ((phase_word + carrier * ftw * cycles) >> 16) & 0xFFFF
            )
            p.update(
                ftw=ftw,
                pow=pow_,
                asf=round(p["amplitude"] * 0x3FFF),
                ram_pow=actual_pow,
                carrier_phase_word_u32=phase_word,
                ram_profile=profile,
                ram_start=start,
                ram_end=end,
                ram_matched_latency=1,
                ram_boundary=True,
                ram_frame_only=frame_only,
                ram_latency_mu=la,
                ram_phase_latency_mu=lp,
                nco_mode=device.nco_mode,
                carrier_effect_mu=carrier,
                effect_mu=pulse.start_mu,
                ref_time_mu=0,
                ram_replay_action="first",
                ram_prepare_duration_mu=0,
                ram_min_gap_mu=0,
            )
            if continuous:
                p["ram_pow_latency_mu"] = lp
                p["nco_preload_phase"] = preload_phase
                if explicit and not frame_only and lp != la:
                    raise CompileError(
                        "Strict RAM phase/envelope boundary requires equal "
                        "ram_pow_latency_mu and ram_latency_mu; "
                        "zero-tail early phase changes are not allowed"
                    )
            p.pop("_base_phase", None)
            p.pop("_frame_scale", None)
            if previous is not None:
                old, old_pulse, old_launch = previous
                same = old["ftw"] == ftw and old["ram_profile"] == profile
                if continuous and old["ftw"] != ftw:
                    raise CompileError("Continuous NCO requires fixed FTW per shot")
                if continuous and old["ram_profile"] != profile:
                    raise CompileError(
                        "Continuous RAM profile changes require a mode-preserving "
                        "rearm path; disabling RAM would interrupt the NCO FTW source"
                    )
                action = (
                    (
                        "retrigger"
                        if old["ram_pow"] == actual_pow
                        and not (continuous and explicit)
                        else "phase"
                    )
                    if same
                    else "rearm"
                )
                preparation = 0 if action == "retrigger" else budget if same else rearm
                release = old_launch + 8 if same else old_pulse.end_mu
                if launch < old_launch + 8 + setup.rtio_coarse_mu:
                    raise CompileError(
                        "RAM IO_UPDATE pulses need a low RTIO coarse period "
                        "between commits; no gap was inserted"
                    )
                if not same and launch - preparation < old_pulse.end_mu:
                    raise CompileError(
                        f"RAM gate gap on {name} must be at least {rearm + la} mu "
                        "for profile/frequency rearm"
                    )
                continuous_phase = continuous and old["ram_pow"] == actual_pow
                legacy_continuous_phase = (
                    same
                    and (
                        (carrier - old["carrier_effect_mu"]) * cycles * ftw
                        + ((old["ram_pow"] - actual_pow) << 16)
                    )
                    % 2**32
                    == 0
                )
                if continuous and not frame_only and not continuous_phase:
                    overlaps_active = any(
                        gate.start_mu < pulse.start_mu and carrier < gate.end_mu
                        for gate in original
                    )
                    if carrier > pulse.start_mu or (
                        carrier < pulse.start_mu and overlaps_active
                    ):
                        raise CompileError(
                            "Strict RAM carrier/envelope latency mismatch would "
                            "change phase inside a gate; no zero-tail allowance "
                            "or gap was inserted"
                        )
                if (
                    not continuous
                    and not frame_only
                    and not legacy_continuous_phase
                    and not _zero_output(
                        original, carrier, pulse.start_mu, setup.tick_seconds
                    )
                ):
                    raise CompileError(
                        f"RAM carrier/envelope latency mismatch on {name} would "
                        "change phase over nonzero envelope; the existing "
                        "quantized zero tail is insufficient, no gap was inserted"
                    )
                p.update(
                    ram_replay_action=action,
                    ram_prepare_duration_mu=preparation,
                    ram_min_gap_mu=0 if same else rearm + la,
                )
                if preparation:
                    request = ScheduledEvent(
                        f"dds_ram_{action}",
                        name,
                        launch,
                        preparation,
                        dict(p),
                        pulse.source,
                        resources,
                        pulse.logical_ids,
                    )
                    requests.append(ConfigurationRequest(request, release))
            elif continuous and explicit:
                # Do not preload a first/standalone Rz target: hold frame zero
                # until the requested hardware deadline, then really commit.
                p["ram_prepare_duration_mu"] = budget
                requests.append(
                    ConfigurationRequest(
                        ScheduledEvent(
                            "dds_ram_phase",
                            name,
                            launch,
                            budget,
                            dict(p),
                            pulse.source,
                            resources,
                            pulse.logical_ids,
                        ),
                        None,
                    )
                )
            for rs, re, rr, _ in reservations:
                if set(resources).intersection(rr) and launch < re and rs < launch + 8:
                    raise CompileError(
                        "RAM commit overlaps an explicit resource reservation"
                    )
                if {name, device.update_group}.intersection(rr) and any(
                    rs <= effect < re for effect in (carrier, pulse.start_mu)
                ):
                    raise CompileError(
                        "RAM effect overlaps an explicit resource reservation"
                    )
            events.extend(
                [
                    ScheduledEvent(
                        "dds_ram_start",
                        name,
                        launch,
                        0,
                        dict(p),
                        pulse.source,
                        resources,
                        pulse.logical_ids,
                    ),
                    ScheduledEvent(
                        "dds_ram_commit_off",
                        name,
                        launch + 8,
                        0,
                        {},
                        pulse.source,
                        (name,),
                        pulse.logical_ids,
                    ),
                    ScheduledEvent(
                        "dds_ram_effect",
                        name,
                        pulse.start_mu,
                        0,
                        dict(p),
                        pulse.source,
                        (name,),
                        pulse.logical_ids,
                    ),
                ]
            )
            all_edges.update((launch, launch + 8))
            actions[p["ram_replay_action"]] += 1
            previous = p, pulse, launch
        # Merge RF gating only; every envelope restart remains explicit.
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
                    ScheduledEvent(kind, name, when, 0, {}, "RAM RF gate", (name,), ids)
                )
                all_edges.add(when)
        diagnostics.append(
            dict(
                kind="ram_bank",
                device=name,
                profiles=len(bank),
                words=words,
                plays=len(original),
                min_gap_mu=0,
                replay_action_counts=actions,
                phase_latency_mu=lp,
                envelope_latency_mu=la,
                status="assumed_not_measured",
            )
        )
    configs, _ = place_configurations(
        requests, [], setup, all_edges, fixed_jobs, reservations
    )
    events.extend(configs)
    jobs = [preparation_window(e, setup) for e in configs]
    return events, jobs, all_edges
