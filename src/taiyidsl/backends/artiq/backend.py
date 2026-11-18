"""ARTIQ 9 / AD9910 / TTL physical compilation policy.

Blocking submission, shared update groups, register formats and shot-tracking
phase live here rather than in the common experiment compiler.
"""

import copy
import math
from bisect import bisect_right

from taiyidsl.backends.artiq._frames import hardware_frame_events
from taiyidsl.backends.artiq._nco import finalize_nco_epochs
from taiyidsl.backends.artiq._preparation import (
    ConfigurationRequest,
    place_configurations,
)
from taiyidsl.backends.artiq._ram_boundary import lower_ram_boundary
from taiyidsl.backends.artiq._ram_polar import lower_ram_polar
from taiyidsl.backends.artiq._square import lower_square
from taiyidsl.backends.artiq.submission import validate_submission_plan
from taiyidsl.backends.artiq.waveforms import ad9910_amplitude_ram
from taiyidsl.backends.base import Backend, BackendCapabilities
from taiyidsl.config.hardware import ControlBinding, DeviceCalibration
from taiyidsl.ir import CompileError, PhysicalPlan, ScheduledEvent


class ArtiqBackend(Backend):
    name = "artiq"
    version = "10"
    target = "ARTIQ 9 / AD9910 / TTL"
    phase_policy = "explicit_continuous_or_legacy_tracking"
    capabilities = BackendCapabilities(
        device_kinds=frozenset({"dds", "ttl"}),
        pulse_shapes=frozenset({"square", "flat_top_gaussian"}),
        operations=BackendCapabilities(device_kinds=frozenset()).operations | {"trig"},
    )

    def validate_setup(self, setup, mapping):
        for device in setup.devices.values():
            if device.config_duration_mu % device.grid_mu:
                raise CompileError(
                    "Configuration duration must be a multiple of device grid"
                )
            if (
                device.kind == "dds"
                and device.config_duration_mu % device.config_grid_mu
            ):
                raise CompileError(
                    "DDS configuration duration must respect configuration grid"
                )
            if not math.isfinite(device.sysclk_hz) or device.sysclk_hz <= 0:
                raise CompileError("sysclk_hz must be positive and finite")
            if not (
                0 <= device.min_frequency < device.max_frequency <= device.sysclk_hz / 2
            ):
                raise CompileError("Frequency limits must lie within Nyquist range")

    def lower(self, logical_events, setup, mapping, diagnostics):
        return _lower(logical_events, setup, mapping, diagnostics)

    def export(self, compiled, output_dir, **options):
        from taiyidsl.backends.artiq.export import export_artiq

        return export_artiq(compiled, output_dir, **options)

    def provenance(self):
        return {**super().provenance(), "artiq_compile": "not_verified"}


def quantize_ad9910(frequency, phase, amplitude, sysclk_hz):
    """Return unsigned FTW, POW and ASF; caller validates physical ranges."""
    return (
        round(frequency / sysclk_hz * 2**32),
        round(phase / (2 * math.pi) * 2**16) % 2**16,
        round(amplitude * 0x3FFF),
    )


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def _check_intervals(intervals):
    by_resource = {}
    for start, end, resources, source in intervals:
        if start == end:
            continue
        for resource in resources:
            by_resource.setdefault(resource, []).append((start, end, source))
    for resource, spans in by_resource.items():
        spans.sort(key=lambda x: (x[0], x[1]))
        for a, b in zip(spans, spans[1:]):
            if _overlap(a, b):
                raise CompileError(
                    f"Resource conflict on {resource}: {a[2]} overlaps {b[2]}"
                )


def _prepare_ram(pulses, setup, edges, reservations, diagnostics):
    """Allocate resident waveforms and explicit RF-off rearm windows per shot."""
    by_device = {}
    for pulse in pulses:
        if (
            "ram_asf" in pulse.params
            and setup.devices[pulse.signal].nco_mode != "continuous"
            and setup.devices[pulse.signal].ram_phase_latency_mu is None
        ):
            by_device.setdefault(pulse.signal, []).append(pulse)
    jobs = []
    sorted_edges = sorted(edges)
    active = {p.signal for p in pulses}
    for name, sequence in by_device.items():
        device = setup.devices[name]
        latency = device.ram_latency_mu
        if latency is None:
            raise CompileError(
                "FlatTopGaussian requires explicit device.ram_latency_mu "
                "calibration/assumption"
            )
        if latency < 8 or latency % device.config_grid_mu:
            raise CompileError(
                "ram_latency_mu must be >=8 and align to the DDS configuration grid"
            )
        if 8 * setup.tick_seconds <= 4 / device.sysclk_hz:
            raise CompileError("8-mu RAM launch must exceed the DDS SYNC_CLK period")
        if any(p.signal == name and "ram_asf" not in p.params for p in pulses) or any(
            other != name
            and device.update_group
            and setup.devices[other].update_group == device.update_group
            for other in active
        ):
            raise CompileError(
                "RAM playback requires an exclusive active IO_UPDATE group per shot; "
                "mixing Square and RAM on the same DDS is unsupported"
            )
        budget = device.ram_rearm_duration_mu
        phase_budget = device.ram_phase_update_duration_mu
        repeated = len(sequence) > 1
        bank_mode = budget is not None
        if phase_budget is not None:
            if not bank_mode:
                raise CompileError(
                    "Fast RAM replay requires explicit ram_rearm_duration_mu "
                    "and matched RAM bank mode"
                )
            if phase_budget < 8 or phase_budget % device.config_grid_mu:
                raise CompileError(
                    "ram_phase_update_duration_mu must be >=8 and align "
                    "to DDS configuration grid"
                )
        if bank_mode and latency < math.ceil(
            106 / device.sysclk_hz / setup.tick_seconds
        ):
            raise CompileError(
                "RAM bank mode requires matched-RAM ram_latency_mu of at least "
                "106 DDS clock cycles, aligned to the DDS configuration grid"
            )
        if repeated and budget is None:
            raise CompileError(
                "Repeated RAM playback requires explicit device.ram_rearm_duration_mu"
            )
        if bank_mode and (budget < 8 or budget % device.config_grid_mu):
            raise CompileError(
                "ram_rearm_duration_mu must be >=8 and align to DDS configuration grid"
            )
        resources = tuple(
            sorted({name, device.bus, device.update_group, "kernel_submit"} - {None})
        )
        bank, words = {}, 0
        previous = None
        for pulse in sorted(sequence, key=lambda p: p.start_mu):
            key = (tuple(pulse.params["ram_asf"]), pulse.params["ram_step"])
            if key not in bank:
                if len(bank) >= 8:
                    raise CompileError(f"RAM bank on {name} exceeds 8 profiles")
                count = len(key[0])
                if words + count > 1024:
                    raise CompileError(
                        f"Resident RAM bank on {name} exceeds 1024 words"
                    )
                bank[key] = (len(bank), words, words + count - 1)
                words += count
            profile, start, end = bank[key]
            pulse.params.update(
                ram_profile=profile,
                ram_start=start,
                ram_end=end,
                ram_matched_latency=int(bank_mode),
            )
            launch = pulse.start_mu - latency
            pulse.params["ram_launch_mu"] = launch
            pulse.params["ftw"], pulse.params["pow"], pulse.params["asf"] = (
                quantize_ad9910(
                    pulse.params["frequency"],
                    pulse.params["phase"],
                    pulse.params["amplitude"],
                    device.sysclk_hz,
                )
            )
            cycles = device.sysclk_hz * setup.tick_seconds
            if not math.isclose(cycles, round(cycles), abs_tol=1e-8):
                raise CompileError(
                    "RAM phase tracking requires integer DDS cycles per target tick"
                )
            pulse.params["ram_pow"] = (
                pulse.params["pow"]
                + ((launch * pulse.params["ftw"] * round(cycles)) >> 16)
            ) & 0xFFFF
            pulse.params["ram_replay_action"] = "first"
            pulse.params["ram_prepare_duration_mu"] = 0
            if bank_mode:
                pulse.params["ram_min_gap_mu"] = budget + latency
            if previous is not None:
                action, preparation = "rearm", budget
                if (
                    phase_budget is not None
                    and previous.params["ram_profile"] == profile
                    and previous.params["ftw"] == pulse.params["ftw"]
                ):
                    if previous.params["ram_pow"] == pulse.params["ram_pow"]:
                        action, preparation = "retrigger", 0
                    else:
                        action, preparation = "phase", phase_budget
                prepare_start = launch - preparation
                pulse.params.update(
                    ram_replay_action=action,
                    ram_prepare_duration_mu=preparation,
                    ram_min_gap_mu=preparation + latency,
                )
                if prepare_start < previous.end_mu:
                    raise CompileError(
                        f"RAM gate gap on {name} must be at least "
                        f"{preparation + latency} mu "
                        f"({action} preparation + ram_latency_mu); "
                        f"got {pulse.start_mu - previous.end_mu} mu"
                    )
                if preparation:
                    pulse.params["ram_prepare_mu"] = prepare_start
                    jobs.append((prepare_start, launch, resources, f"RAM {action}"))
            jobs.append((launch, launch + 8, resources, "RAM launch"))
            previous = pulse
        diagnostics.append(
            {
                "kind": "ram_bank",
                "device": name,
                "profiles": len(bank),
                "words": words,
                "plays": len(sequence),
                "min_gap_mu": budget + latency if bank_mode else None,
                "phase_update_budget_mu": phase_budget,
                "replay_action_counts": {
                    action: sum(
                        p.params["ram_replay_action"] == action for p in sequence
                    )
                    for action in ("first", "rearm", "phase", "retrigger")
                },
                "status": "assumed_not_measured",
            }
        )
    for start, end, resources, label in jobs:
        edge_index = bisect_right(sorted_edges, start)
        if edge_index < len(sorted_edges) and sorted_edges[edge_index] < end:
            raise CompileError(f"An output edge overlaps the {label} submission window")
        if any(
            set(resources).intersection(rr) and _overlap((start, end), (rs, re))
            for rs, re, rr, _ in reservations
        ):
            raise CompileError(f"{label} overlaps an explicit resource reservation")
    _check_intervals(jobs)
    return jobs


def _lower(logical, setup, mapping, diagnostics):
    recorded_logical = logical
    for idx, event in enumerate(logical):
        event.logical_ids = (f"e{idx}",)
    logical = hardware_frame_events(logical, setup, mapping)
    intervals, pulses, reservations = [], [], []
    trigger_ids = set()
    strict_trigger_ids = set()
    frame_names = {
        e.signal for e in logical if e.kind in {"shift_phase", "reset_frame"}
    }
    for idx, event in enumerate(logical):
        event.logical_ids = (f"e{idx}",)
        # Historical protocol TTL windows may merge with an adjacent compatible
        # play. Explicit trig/acquisition/AWG launches require a distinct edge.
        if event.kind in {"trigger", "trig"}:
            trigger_ids.update(event.logical_ids)
            if "protocol" not in event.params:
                strict_trigger_ids.update(event.logical_ids)
        if event.kind not in {"play", "delay", "reserve", "trigger", "trig"}:
            continue
        if event.kind == "reserve" and event.signal not in mapping:
            known = {"kernel_submit"} | set(setup.devices)
            known |= {d.bus for d in setup.devices.values()} | {
                d.update_group for d in setup.devices.values()
            }
            if event.signal not in known:
                raise CompileError(f"Unknown reserved resource {event.signal}")
            resources = (event.signal,)
            intervals.append((event.start_mu, event.end_mu, resources, event.source))
            reservations.append((event.start_mu, event.end_mu, resources, event.source))
            continue
        try:
            bindings = mapping.bindings(event.signal)
        except (ValueError, TypeError) as exc:
            raise CompileError(str(exc)) from exc
        devices = []
        for binding in bindings:
            if binding.device not in setup.devices:
                raise CompileError(f"Unknown mapped device {binding.device}")
            device = setup.devices[binding.device]
            devices.append(device.name)
        intervals.append((event.start_mu, event.end_mu, tuple(devices), event.source))
        event.resources = tuple(devices)
        if event.kind == "reserve":
            reservations.append(
                (event.start_mu, event.end_mu, tuple(devices), event.source)
            )
        frame_state = (
            event.kind == "play"
            and event.params.get("frame", event.params.get("qubit")) in frame_names
            and all(
                setup.devices[b.device].square_latency_mu is not None for b in bindings
            )
        )
        if event.kind in {"delay", "reserve"} or (
            event.duration_mu == 0 and not frame_state
        ):
            continue
        for binding in bindings:
            device = setup.devices[binding.device]
            cal = setup.calibration.get(device.name, DeviceCalibration())
            latency = device.latency_mu + cal.latency_mu
            start = event.start_mu - latency
            if start % device.grid_mu or event.duration_mu % device.grid_mu:
                raise CompileError(
                    f"Pulse on {device.name} violates its "
                    f"{device.grid_mu} mu device grid"
                )
            if 0 < event.duration_mu < device.min_duration_mu:
                raise CompileError(f"Pulse shorter than minimum on {device.name}")
            params = dict(event.params)
            if device.kind == "dds" and (
                device.square_latency_mu is not None
                or device.ram_phase_latency_mu is not None
                or device.nco_mode == "continuous"
            ):
                params["_frame_scale"] = binding.phase_scale
                params["nco_initial_phase"] = binding.phase_offset + cal.phase_offset
            if device.kind == "dds":
                if event.kind in {"trigger", "trig"}:
                    raise CompileError("Protocol trigger steps require TTL resources")
                params["frequency"] = (
                    params["frequency"] * binding.frequency_scale
                    + binding.frequency_offset
                    + cal.frequency_offset
                )
                params["phase"] = (
                    params["phase"] * binding.phase_scale
                    + binding.phase_offset
                    + cal.phase_offset
                ) % (2 * math.pi)
                params["amplitude"] *= binding.amplitude_scale * cal.amplitude_scale
                if (
                    not device.min_frequency
                    <= params["frequency"]
                    <= device.max_frequency
                    or params["frequency"] >= device.sysclk_hz / 2
                ):
                    raise CompileError(f"Frequency outside limits on {device.name}")
                if not 0 <= params["amplitude"] <= 1:
                    raise CompileError(
                        f"Calibrated amplitude outside 0..1 on {device.name}"
                    )
            elif event.kind == "play":
                if device.ttl_idle_level != 0:
                    raise CompileError(
                        "Use trig for an idle-high TTL; play is active-high"
                    )
                if params.get("shape") == "flat_top_gaussian":
                    raise CompileError(
                        "FlatTopGaussian requires an AD9910 DDS, not TTL"
                    )
                if (
                    params["amplitude"] not in (0, 1)
                    or params["frequency"] != 0
                    or params["phase"] != 0
                ):
                    raise CompileError(
                        "TTL Square only supports amplitude 0/1, no frequency or phase"
                    )
                if params["amplitude"] == 0:
                    continue
            if device.kind == "ttl":
                if event.kind == "trig":
                    if binding != ControlBinding(binding.device) or any(
                        getattr(cal, field) != getattr(DeviceCalibration(), field)
                        for field in (
                            "amplitude_scale",
                            "phase_offset",
                            "frequency_offset",
                        )
                    ):
                        raise CompileError(
                            "Digital trig requires an identity TTL binding"
                        )
                if event.kind == "trig" or device.ttl_idle_level:
                    params.update(
                        digital_idle_level=device.ttl_idle_level,
                        digital_active_level=1 - device.ttl_idle_level,
                    )
                params.update(frequency=0, phase=0, amplitude=1)
            if params.get("shape") == "flat_top_gaussian":
                params.update(
                    ad9910_amplitude_ram(
                        params, device.sysclk_hz, event.duration_mu * setup.tick_seconds
                    )
                )
                if start % device.config_grid_mu:
                    raise CompileError(
                        "RAM launch must align to the DDS configuration grid"
                    )
            pulses.append(
                ScheduledEvent(
                    device.kind,
                    device.name,
                    start,
                    event.duration_mu,
                    params,
                    event.source,
                    (device.name,),
                    event.logical_ids,
                )
            )
    played_pulses = tuple(pulses)
    actual_state_names = {p.signal for p in played_pulses}
    signals_by_id = {e.logical_ids[0]: e.signal for e in logical}
    for frame in (e for e in logical if e.kind in {"shift_phase", "reset_frame"}):
        if frame.params.get("command_kind") in {
            "rz",
            "shift_phase",
            "reset_frame",
            "reset_phase",
        }:
            for pulse in played_pulses:
                if pulse.signal not in frame.params["target_devices"]:
                    continue
                if any(
                    signals_by_id[uid] not in frame.params["initial_states"]
                    for uid in pulse.logical_ids
                ):
                    raise CompileError(
                        "Hardware frame requires unambiguous Raman/RF DDS ownership; "
                        "a non-drive alias shares its physical phase state"
                    )
        transforms = {}
        for signal, defaults in frame.params.get("initial_states", {}).items():
            if signal not in mapping:
                continue
            for binding in mapping.bindings(signal):
                device = setup.devices[binding.device]
                if device.kind != "dds":
                    continue
                cal = setup.calibration.get(device.name, DeviceCalibration())
                transform = (
                    binding.phase_scale,
                    binding.phase_offset + cal.phase_offset,
                )
                if device.name not in actual_state_names:
                    # No played state identifies which logical alias owns the
                    # initial carrier. Defaults must agree after quantization.
                    ftw, _, asf = quantize_ad9910(
                        defaults["frequency"] * binding.frequency_scale
                        + binding.frequency_offset
                        + cal.frequency_offset,
                        0,
                        defaults["amplitude"]
                        * binding.amplitude_scale
                        * cal.amplitude_scale,
                        device.sysclk_hz,
                    )
                    transform += (ftw, asf)
                if device.name in transforms and transforms[device.name] != transform:
                    raise CompileError(
                        "Hardware frame has ambiguous initial state "
                        "or inconsistent DDS bindings"
                    )
                transforms[device.name] = transform
                if any(
                    p.signal == device.name
                    and p.params.get("frame", p.params.get("qubit")) == frame.signal
                    for p in pulses
                ):
                    continue
                params = {
                    "frequency": defaults["frequency"] * binding.frequency_scale
                    + binding.frequency_offset
                    + cal.frequency_offset,
                    "amplitude": defaults["amplitude"]
                    * binding.amplitude_scale
                    * cal.amplitude_scale,
                    "phase": (binding.phase_offset + cal.phase_offset) % (2 * math.pi),
                    "qubit": frame.signal,
                    "frame_phase": 0,
                    "_frame_scale": binding.phase_scale,
                    "nco_initial_phase": binding.phase_offset + cal.phase_offset,
                    "hardware_frame_resolved": True,
                }
                if (
                    not device.min_frequency
                    <= params["frequency"]
                    <= device.max_frequency
                    or not 0 <= params["amplitude"] <= 1
                ):
                    raise CompileError(
                        "Hardware frame initial state is outside DDS limits"
                    )
                pulses.append(
                    ScheduledEvent(
                        "dds",
                        device.name,
                        frame.start_mu - device.latency_mu - cal.latency_mu,
                        0,
                        params,
                        frame.source,
                        (device.name,),
                        frame.logical_ids,
                    )
                )
    _check_intervals(intervals)
    _check_intervals([(p.start_mu, p.end_mu, p.resources, p.source) for p in pulses])
    timed = [
        p
        for p in pulses
        if "ram_asf" not in p.params
        and setup.devices[p.signal].kind == "dds"
        and setup.devices[p.signal].square_latency_mu is not None
        and setup.devices[p.signal].ram_destination != "powasf"
    ]
    timed_names = {p.signal for p in timed}
    timed_groups = {setup.devices[n].update_group for n in timed_names} - {None}
    if any(
        "ram_asf" in p.params
        and (
            p.signal in timed_names
            or setup.devices[p.signal].update_group in timed_groups
        )
        for p in pulses
    ):
        raise CompileError(
            "RAM playback requires an exclusive active IO_UPDATE group; "
            "mixing Square/RAM is unsupported"
        )
    if any(
        p.signal not in timed_names
        and setup.devices[p.signal].kind == "dds"
        and setup.devices[p.signal].update_group in timed_groups
        for p in pulses
    ):
        raise CompileError(
            "Timed Square cannot share IO_UPDATE with active legacy/RAM DDS"
        )
    frames = [e for e in logical if e.kind in {"shift_phase", "reset_frame"}]
    polar_names = {
        p.signal for p in pulses if setup.devices[p.signal].ram_destination == "powasf"
    }
    boundary_names = {
        p.signal
        for p in pulses
        if "ram_asf" in p.params
        and p.signal not in polar_names
        and (
            setup.devices[p.signal].ram_phase_latency_mu is not None
            or setup.devices[p.signal].nco_mode == "continuous"
        )
    }
    for frame in frames:
        affected = [
            p
            for p in pulses
            if p.signal in frame.params["target_devices"]
            and p.params.get("frame", p.params.get("qubit")) == frame.signal
            and setup.devices[p.signal].kind == "dds"
        ]
        if not affected or any(
            p.signal not in timed_names | boundary_names | polar_names for p in affected
        ):
            raise CompileError(
                "Hardware frame updates require a known timed Square DDS state; "
                "configure square_latency_mu or RAM ram_phase_latency_mu "
                "(legacy RAM frame updates are unsupported)"
            )
    other_pulses = [p for p in pulses if p.signal not in timed_names]
    # Adjacent compatible output intervals form one physical pulse,
    # preserving logical identities.
    merged = []
    for name in sorted({p.signal for p in other_pulses}):
        seq = sorted(
            (p for p in other_pulses if p.signal == name), key=lambda p: p.start_mu
        )
        for pulse in seq:
            last = merged[-1] if merged and merged[-1].signal == name else None
            if (
                last
                and last.end_mu == pulse.start_mu
                and (
                    strict_trigger_ids.intersection(
                        pulse.logical_ids + last.logical_ids
                    )
                    or (
                        trigger_ids.intersection(pulse.logical_ids)
                        and trigger_ids.intersection(last.logical_ids)
                    )
                )
            ):
                raise CompileError("Trigger pulses require an explicit idle gap")

            def signature(p):
                return tuple(
                    p.params.get(k) for k in ("frequency", "phase", "amplitude")
                )

            if (
                last
                and pulse.signal not in polar_names
                and "ram_asf" not in last.params
                and "ram_asf" not in pulse.params
                and last.end_mu == pulse.start_mu
                and signature(last) == signature(pulse)
            ):
                last.duration_mu += pulse.duration_mu
                last.logical_ids += pulse.logical_ids
            else:
                merged.append(copy.deepcopy(pulse))
    edges = {t for p in merged for t in (p.start_mu, p.end_mu)}
    ram_jobs = _prepare_ram(merged, setup, edges, reservations, diagnostics)
    boundary_events, boundary_jobs, edges = lower_ram_boundary(
        [p for p in merged if p.signal not in polar_names],
        frames,
        setup,
        edges,
        ram_jobs,
        reservations,
        diagnostics,
    )
    ram_jobs += boundary_jobs
    polar_events, polar_jobs, edges = lower_ram_polar(
        pulses, frames, setup, edges, reservations, diagnostics
    )
    ram_jobs += polar_jobs
    square_events, square_jobs, edges = (
        lower_square(timed, frames, setup, mapping, edges, ram_jobs, reservations)
        if timed
        else ([], [], edges)
    )
    for job in ram_jobs:
        start, end, _, label = job
        if getattr(job, "buffered_spi", False):
            continue
        if any(start < edge < end for edge in edges):
            raise CompileError(f"An output edge overlaps the {label} submission window")
    requests, physical, previous = (
        [],
        list(square_events) + boundary_events + polar_events,
        {},
    )
    for pulse in sorted(merged, key=lambda p: (p.start_mu, p.signal)):
        device = setup.devices[pulse.signal]
        if pulse.signal in boundary_names | polar_names:
            continue
        if "ram_asf" in pulse.params:
            params = dict(pulse.params)
            launch = params.pop("ram_launch_mu")
            prepare = params.pop("ram_prepare_mu", None)
            params["ram_latency_mu"] = device.ram_latency_mu
            params["ref_time_mu"] = 0
            resources = tuple(
                sorted(
                    {device.name, device.bus, device.update_group, "kernel_submit"}
                    - {None}
                )
            )
            if prepare is not None:
                physical.append(
                    ScheduledEvent(
                        f"dds_ram_{params['ram_replay_action']}",
                        device.name,
                        prepare,
                        params["ram_prepare_duration_mu"],
                        dict(params),
                        pulse.source,
                        resources,
                        pulse.logical_ids,
                    )
                )
            physical.extend(
                [
                    ScheduledEvent(
                        "dds_on",
                        device.name,
                        pulse.start_mu,
                        0,
                        {},
                        pulse.source,
                        (device.name,),
                        pulse.logical_ids,
                    ),
                    ScheduledEvent(
                        "dds_ram_start",
                        device.name,
                        launch,
                        8,
                        params,
                        pulse.source,
                        resources,
                        pulse.logical_ids,
                    ),
                    ScheduledEvent(
                        "dds_off",
                        device.name,
                        pulse.end_mu,
                        0,
                        {},
                        pulse.source,
                        (device.name,),
                        pulse.logical_ids,
                    ),
                ]
            )
            continue
        signature = tuple(
            pulse.params.get(k) for k in ("frequency", "phase", "amplitude")
        )
        old = previous.get(device.name)
        if device.kind == "dds" and (old is None or old[0] != signature):
            if device.config_duration_mu < 332 * device.spi_ref_period_mu + 8:
                raise CompileError(
                    "DDS configuration budget is below real driver minimum"
                )
            earliest = old[1] if old else None
            resources = {
                device.name,
                device.bus,
                device.update_group,
                "kernel_submit",
            } - {None}
            params = {k: pulse.params[k] for k in ("frequency", "phase", "amplitude")}
            params["ftw"], params["pow"], params["asf"] = quantize_ad9910(
                params["frequency"],
                params["phase"],
                params["amplitude"],
                device.sysclk_hz,
            )
            params["actual_frequency"] = params["ftw"] * device.sysclk_hz / 2**32
            params["actual_phase"] = params["pow"] * (2 * math.pi) / 2**16
            params["actual_amplitude"] = params["asf"] / 0x3FFF
            params["ref_time_mu"] = 0
            requests.append(
                ConfigurationRequest(
                    ScheduledEvent(
                        "dds_config",
                        device.name,
                        pulse.start_mu,
                        device.config_duration_mu,
                        params,
                        pulse.source,
                        tuple(sorted(resources)),
                        pulse.logical_ids,
                    ),
                    earliest,
                )
            )
        previous[device.name] = (signature, pulse.end_mu)
        for suffix, when in (("on", pulse.start_mu), ("off", pulse.end_mu)):
            if device.kind == "ttl" and device.ttl_idle_level:
                suffix = "off" if suffix == "on" else "on"
            physical.append(
                ScheduledEvent(
                    f"{device.kind}_{suffix}",
                    device.name,
                    when,
                    0,
                    {
                        key: pulse.params[key]
                        for key in ("digital_idle_level", "digital_active_level")
                        if key in pulse.params
                    },
                    pulse.source,
                    (device.name,),
                    pulse.logical_ids,
                )
            )
    configs, reordered = place_configurations(
        requests, merged, setup, edges, ram_jobs + square_jobs, reservations
    )
    if reordered:
        diagnostics.append(
            {
                "kind": "preparation_reordered",
                "message": (
                    "Tied DDS preparation deadlines retried by latest release first; "
                    "output timing is unchanged. This is a bounded scheduling "
                    "heuristic, not a complete feasibility solver."
                ),
            }
        )
    physical.extend(configs)
    for ram in [e for e in physical if e.kind == "dds_ram_start"]:
        if any(
            _overlap((ram.start_mu, ram.end_mu), (c.start_mu, c.end_mu))
            for c in configs
        ):
            raise CompileError(
                "DDS configuration overlaps the RAM launch submission window"
            )
    finalize_nco_epochs(physical, setup, diagnostics, reservations)
    # Exact alignment of t0 and the pre-roll is part of the export contract.
    grids = [
        g
        for d in setup.devices.values()
        for g in (d.grid_mu, d.config_grid_mu if d.kind == "dds" else 1)
    ]
    grid = math.lcm(setup.rtio_coarse_mu, *grids)
    minimum = min((e.start_mu for e in physical), default=0)
    origin = ((max(0, -minimum) + grid - 1) // grid) * grid
    for event in physical:
        event.start_mu += origin
        if event.kind in {
            "dds_config",
            "dds_ram_start",
            "dds_ram_rearm",
            "dds_ram_phase",
            "dds_ram_effect",
            "dds_prepare",
            "dds_effect",
            "dds_polar_select",
            "dds_polar_retrigger",
            "dds_polar_effect",
        }:
            event.params["ref_time_mu"] = origin
        if "effect_mu" in event.params:
            event.params["effect_mu"] += origin
        if "carrier_effect_mu" in event.params:
            event.params["carrier_effect_mu"] += origin
        for key in (
            "nco_epoch_release_mu",
            "nco_epoch_effect_mu",
            "polar_commit_mu",
            "polar_amplitude_effect_mu",
        ):
            if key in event.params:
                event.params[key] += origin
    # Edges at configuration start must be submitted first; edge at finish follows it.
    physical.sort(
        key=lambda e: (
            e.start_mu,
            e.kind
            in {
                "dds_config",
                "dds_ram_rearm",
                "dds_ram_phase",
                "dds_prepare",
                "dds_polar_select",
            }
            or (e.kind == "dds_ram_start" and not e.params.get("ram_boundary")),
            e.signal,
            e.kind,
        )
    )
    if configs:
        diagnostics.append(
            {
                "kind": "driver_contract",
                "status": "assumed_not_measured",
                "message": (
                    "DDS configuration durations and output model "
                    "require real target validation"
                ),
            }
        )
    if timed:
        diagnostics.append(
            {
                "kind": "square_timing_contract",
                "status": "assumed_not_measured",
                "message": "Buffered prepare / timed commit / matched output effect; "
                "latency and analog phase require target calibration",
            }
        )
    validate_submission_plan(physical, setup)
    for recorded, lowered in zip(recorded_logical, logical):
        recorded.resources = lowered.resources
    return PhysicalPlan(physical, origin)
