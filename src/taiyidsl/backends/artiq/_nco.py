"""One RF-off phase epoch followed by continuous fixed-FTW accumulation."""

import copy
import math

from taiyidsl.ir import CompileError, ScheduledEvent


def continuous_contract(device, setup):
    """Validate the independently declared epoch, never infer it from autoclear."""
    if device.nco_mode != "continuous":
        return False
    if device.nco_epoch_latency_mu is None:
        raise CompileError(
            "Continuous NCO requires explicit nco_epoch_latency_mu; "
            "legacy phase/autoclear latency is not a release calibration"
        )
    cycles = device.sysclk_hz * setup.tick_seconds
    if not math.isclose(cycles, round(cycles), abs_tol=1e-8):
        raise CompileError("Continuous NCO requires integer DDS clocks per target tick")
    return True


def finalize_nco_epochs(physical, setup, diagnostics, reservations=()):
    """Add timed releases and a fixed reference correction before origin shifting.

    Preload holds each accumulator at zero in its final DDS mode. All releases
    precede timed preparation, so no later register write can be latched early.
    The integer correction leaves less than one POW LSB per carrier reference;
    the running accumulator retains all 32 bits, including across reset_phase.
    """
    states = {}
    for event in physical:
        device = setup.devices[event.signal]
        if device.kind == "dds" and device.nco_mode == "continuous":
            continuous_contract(device, setup)
            if "ftw" in event.params:
                states.setdefault(event.signal, []).append(event)
    if not states:
        return
    grid = math.lcm(
        setup.rtio_coarse_mu,
        *(setup.devices[name].config_grid_mu for name in states),
    )
    settling = max(
        max(
            setup.devices[name].nco_epoch_latency_mu,
            setup.devices[name].ram_latency_mu or 0,
            setup.devices[name].square_latency_mu or 0,
        )
        for name in states
    )
    earliest = min(event.start_mu for event in physical)
    release = ((earliest - settling - 8 - setup.rtio_coarse_mu) // grid) * grid
    # An explicitly reserved resource may also extend into the preroll. Move
    # initialization earlier, never move requested experiment events.
    while True:
        blockers = [
            start
            for start, end, resources, _ in reservations
            if release < end
            and start < release + settling + 8
            and (
                "kernel_submit" in resources
                or any(
                    {name, setup.devices[name].update_group}.intersection(resources)
                    for name in states
                )
            )
        ]
        if not blockers:
            break
        release = ((min(blockers) - settling - 8) // grid) * grid

    groups = {}
    for name, events in sorted(states.items()):
        device = setup.devices[name]
        ftws = {event.params["ftw"] for event in events}
        if len(ftws) != 1:
            raise CompileError(
                f"Continuous NCO on {name} requires fixed FTW per shot; "
                "no automatic reset or gap was inserted"
            )
        if any(event.kind == "dds_config" for event in events):
            raise CompileError("Continuous NCO requires timed Square or resident RAM")
        ftw = next(iter(ftws))
        epoch = release + device.nco_epoch_latency_mu
        epoch_word = epoch * ftw * round(device.sysclk_hz * setup.tick_seconds)
        correction = epoch_word >> 16
        first = min(events, key=lambda event: event.start_mu).params
        initial_phase = first.get(
            "nco_preload_phase", first.get("nco_initial_phase", 0.0)
        )
        initial_pow = (
            round(initial_phase / (2 * math.pi) * 2**16) + correction
        ) & 0xFFFF
        polar_bank = next(
            (
                copy.deepcopy(event.params["polar_bank"])
                for event in events
                if "polar_bank" in event.params
            ),
            None,
        )
        if polar_bank is not None:
            # All events share one resident bank. Apply the constant epoch
            # correction once, never once per invocation of a reused entry.
            for entry in polar_bank:
                entry["pow_samples"] = [
                    ((phase + epoch_word) >> 16) & 0xFFFF
                    for phase in entry["phase_targets_u32"]
                ]
                entry["words"] = [
                    phase << 16 | amplitude << 2
                    for phase, amplitude in zip(entry["pow_samples"], entry["asf"])
                ]
        for event in events:
            p = event.params
            p.update(
                nco_mode="continuous",
                nco_epoch_release_mu=release,
                nco_epoch_effect_mu=epoch,
                nco_epoch_pow=correction & 0xFFFF,
                nco_initial_pow=initial_pow,
            )
            for key in ("square_pow", "ram_pow"):
                if key in p:
                    p[key] = ((p["carrier_phase_word_u32"] + epoch_word) >> 16) & 0xFFFF
            if "polar_bank" in p:
                p["polar_bank"] = polar_bank
                p["incoming_pow"] = (
                    (p["incoming_phase_word_u32"] + epoch_word) >> 16
                ) & 0xFFFF
        group = device.update_group or f"device:{name}"
        groups.setdefault(group, []).append(name)
        diagnostics.append(
            {
                "kind": "continuous_nco",
                "device": name,
                "ftw": ftw,
                "phase_policy": "fixed_ftw_continuous",
                "epoch_relative_mu": epoch,
                "reference_quantization_bound_pow_lsb": 1,
                "carrier_reference_resets": sorted(
                    {
                        event.params["carrier_reference_mu"]
                        for event in events
                        if "carrier_reference_mu" in event.params
                    }
                ),
                "status": "assumed_not_measured",
            }
        )
    for group, members in sorted(groups.items()):
        for kind, when in (
            ("dds_nco_epoch", release),
            ("dds_nco_epoch_off", release + 8),
        ):
            physical.append(
                ScheduledEvent(
                    kind,
                    members[0],
                    when,
                    0,
                    {"commit_members": members, "nco_mode": "continuous"},
                    "RF-off NCO epoch release",
                    (group,),
                    (),
                )
            )
