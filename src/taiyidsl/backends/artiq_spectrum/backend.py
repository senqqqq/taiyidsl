"""Explicit ARTIQ master / Spectrum static-sequence compilation.

Only this backend uses the 200 ps scheduling grid. The ARTIQ lowerer receives
its original device contracts and native machine units, and never sees owned
Spectrum gate, wait, or frame events.
"""

from __future__ import annotations

import copy
import math
from fractions import Fraction

from taiyidsl.backends.artiq.backend import ArtiqBackend
from taiyidsl.backends.base import Backend, BackendCapabilities
from taiyidsl.compiler.timebase import _exact_integer as _exact_integer
from taiyidsl.compiler.timebase import native_factor as native_factor
from taiyidsl.compiler.trigger_routes import resolve_trigger_signal
from taiyidsl.config.hardware import SignalMap
from taiyidsl.config.spectrum import HYBRID_TICK_SECONDS
from taiyidsl.ir import CompileError, PhysicalPlan, ScheduledEvent


def native_setup(setup):
    result = copy.deepcopy(setup)
    result.tick_seconds = setup.spectrum.artiq_tick_seconds
    result.spectrum = None
    result.devices = {
        name: device
        for name, device in result.devices.items()
        if device.kind != "spectrum"
    }
    result.calibration = {
        name: cal for name, cal in result.calibration.items() if name in result.devices
    }
    return result


def native_mapping(mapping, setup):
    result = SignalMap()
    for signal in mapping:
        bindings = mapping.bindings(signal)
        if all(setup.devices[b.device].kind != "spectrum" for b in bindings):
            result[signal] = copy.deepcopy(mapping[signal])
    return result


def native_event(event, factor):
    result = copy.deepcopy(event)
    result.start_mu = _exact_integer(Fraction(event.start_mu, factor), event.source)
    result.duration_mu = _exact_integer(
        Fraction(event.duration_mu, factor), event.source
    )
    # Logical references use the hybrid scheduler tick. Physical DDS payloads
    # already retain native mu, including during artiq_subplan reconstruction.
    if event.kind in {"play", "rz", "shift_phase", "reset_frame", "reset_phase"}:
        if "carrier_reference_mu" in event.params:
            result.params["carrier_reference_mu"] = _exact_integer(
                Fraction(event.params["carrier_reference_mu"], factor), event.source
            )
    return result


class ArtiqSpectrumBackend(Backend):
    name = "artiq_spectrum"
    version = "6"
    target = "ARTIQ 9 master / M4i.6631-x8 AWG sequence and DDS20 transport"
    phase_policy = (
        "unified_frame_with_sequence_or_shot_carrier_reference_and_explicit_reset"
    )
    capabilities = BackendCapabilities(
        device_kinds=frozenset({"dds", "ttl", "spectrum"}),
        pulse_shapes=frozenset({"square", "flat_top_gaussian", "sampled"}),
        operations=ArtiqBackend.capabilities.operations
        | {
            "trigplay",
            "triggered_sequence",
            "scope_delay",
            "native_call",
            "native_delay",
            "move",
        },
    )

    def prepare_setup(self, setup):
        if setup.spectrum is None:
            raise CompileError("artiq_spectrum requires an explicit SpectrumConfig")
        if setup.tick_seconds not in {
            setup.spectrum.artiq_tick_seconds,
            HYBRID_TICK_SECONDS,
        }:
            raise CompileError("Setup tick differs from the declared native ARTIQ tick")
        setup.tick_seconds = HYBRID_TICK_SECONDS
        return setup

    def validate_setup(self, setup, mapping):
        if setup.spectrum is None:
            raise CompileError("Missing SpectrumConfig")
        ArtiqBackend().validate_setup(
            native_setup(setup), native_mapping(mapping, setup)
        )
        known_outputs = set()
        if any(setup.devices[name].kind == "spectrum" for name in setup.calibration):
            raise CompileError(
                "Spectrum calibration belongs in group.calibration.channels; "
                "native ARTIQ DeviceCalibration cannot be silently reused"
            )
        for name, group in setup.spectrum.groups.items():
            for trigger_name in group.trigger_device_names:
                trigger = setup.devices.get(trigger_name)
                if trigger is None or trigger.kind != "ttl":
                    raise CompileError(
                        f"Group {name} requires an ARTIQ TTL trigger device"
                    )
                if getattr(trigger, "ttl_idle_level", 0) != 0:
                    raise CompileError(
                        "Spectrum rising-edge triggers require idle-low TTL"
                    )
                if trigger.latency_mu != 0:
                    raise CompileError(
                        "Declare trigger path latency in the Spectrum contract"
                    )
                trigger_calibration = setup.calibration.get(trigger_name)
                if trigger_calibration is not None and any(
                    getattr(trigger_calibration, field) != expected
                    for field, expected in (
                        ("latency_mu", 0),
                        ("phase_offset", 0),
                        ("frequency_offset", 0),
                        ("amplitude_scale", 1),
                    )
                ):
                    raise CompileError(
                        "Declare trigger path calibration in the Spectrum contract"
                    )
                if group.trigger_width_mu % trigger.grid_mu:
                    raise CompileError("Spectrum trigger width violates the TTL grid")
            resolve_trigger_signal(group, mapping, setup, source=f"group {name}")
            if group.epoch_grid_mu % setup.rtio_coarse_mu:
                raise CompileError(
                    "Spectrum epoch grid must include the RTIO coarse grid"
                )
            for device, output in group.outputs.items():
                if (
                    device not in setup.devices
                    or setup.devices[device].kind != "spectrum"
                ):
                    raise CompileError(f"Unknown Spectrum output device: {device}")
                if device in known_outputs:
                    raise CompileError("Spectrum output belongs to multiple groups")
                if setup.devices[device].latency_mu != 0:
                    raise CompileError(
                        "Spectrum Device.latency_mu must be zero; declare timing "
                        "in the Spectrum card and channel calibration contracts"
                    )
                known_outputs.add(device)
                card = group.cards[output.card]
                if output.initial_frequency_hz is not None and not (
                    setup.devices[device].min_frequency
                    <= output.initial_frequency_hz
                    <= min(setup.devices[device].max_frequency, card.sample_rate_hz / 2)
                ):
                    raise CompileError(
                        "Spectrum initial carrier frequency exceeds the output contract"
                    )
                if card.mode == "dds":
                    _dds_prefix_mu(group, card, setup)
                else:
                    _prefix_samples(group, card, setup)
        if known_outputs != {
            name for name, device in setup.devices.items() if device.kind == "spectrum"
        }:
            raise CompileError(
                "Every Spectrum device must belong to a synchronization group"
            )
        for signal in mapping:
            try:
                devices = [setup.devices[b.device] for b in mapping.bindings(signal)]
            except KeyError as exc:
                raise CompileError(f"Unknown mapped device: {exc}") from exc
            if any(d.kind == "spectrum" for d in devices) and not all(
                d.kind == "spectrum" for d in devices
            ):
                raise CompileError("A signal cannot mix ARTIQ and Spectrum outputs")

    def signal_grid(self, signal, setup, mapping):
        if signal not in mapping:
            return 1
        grids = [
            4
            if setup.devices[b.device].kind == "spectrum"
            else setup.devices[b.device].grid_mu * native_factor(setup)
            for b in mapping.bindings(signal)
        ]
        return math.lcm(*grids)

    def schedule(
        self,
        experiment,
        body,
        setup,
        mapping,
        coordinates,
        allow_quantization,
        diagnostics,
        max_events,
    ):
        from taiyidsl.backends.artiq_spectrum.scheduling import schedule_spectrum

        if allow_quantization:
            raise CompileError(
                "The hybrid backend requires strict timing, without rounding"
            )
        return schedule_spectrum(
            experiment,
            body,
            setup,
            mapping,
            coordinates,
            allow_quantization,
            diagnostics,
            max_events,
            self,
        )

    def lower(self, logical_events, setup, mapping, diagnostics):
        return _lower(logical_events, setup, mapping, diagnostics)

    def export(self, compiled, output_dir, **options):
        from taiyidsl.backends.artiq_spectrum.export import export_spectrum

        return export_spectrum(compiled, output_dir, **options)

    def provenance(self):
        return {
            **super().provenance(),
            "artiq_compile": "not_verified",
            "spectrum_driver": "not_verified",
            "hardware": "not_verified",
        }


def _prefix_samples(group, card, setup):
    lead = (
        Fraction(group.trigger_lead_mu)
        * Fraction(str(setup.spectrum.artiq_tick_seconds))
        * card.sample_rate_hz
    )
    prefix = _exact_integer(lead, "Trigger lead") - card.trigger_latency_samples
    if prefix < 0:
        raise CompileError("Trigger lead must cover the declared card output latency")
    return prefix


def _dds_prefix_mu(group, card, setup):
    if card.dds_trigger_latency_ns is None:
        raise CompileError("DDS needs an explicit dds_trigger_latency_ns contract")
    lead = Fraction(group.trigger_lead_mu) * Fraction(
        str(setup.spectrum.artiq_tick_seconds)
    )
    latency = Fraction(str(card.dds_trigger_latency_ns)) / 1_000_000_000
    prefix = _exact_integer((lead - latency) / Fraction("0.0000000002"), "DDS lead")
    if prefix < 0 or prefix % 32:
        raise CompileError(
            "DDS trigger lead minus latency must be nonnegative on the 6.4 ns grid"
        )
    return prefix


def _carrier_phase(frequency, start_mu):
    cycles = (
        Fraction(str(frequency))
        * Fraction(start_mu)
        * Fraction(str(HYBRID_TICK_SECONDS))
    ) % 1
    return 2 * math.pi * float(cycles)


def card_sample_assets(pulses, setup):
    """Select each source used by this point and card exactly once."""
    assets = getattr(setup, "_sample_assets", {})
    return {
        digest: assets[digest]
        for digest in {
            p["envelope"]["sample_digest"]
            for p in pulses
            if p["envelope"]["kind"] == "sampled"
        }
    }


def _validate_raw_controls(event, binding, phase_correction, state, members):
    if (
        event.params["frequency"] != 0
        or binding.frequency_offset != 0
        or binding.frequency_scale != 1
        or event.params["phase"] != 0
        or binding.phase_offset != 0
        or binding.phase_scale != 1
        or phase_correction != 0
        or state.modulation_phase_rad != 0
    ):
        raise CompileError(
            f"Raw waveform cannot apply frequency or phase transforms, calibration "
            f"phase offsets or an active frame offset at {event.source}"
        )
    for update in members:
        if (
            update.kind in {"rz", "shift_phase", "reset_frame", "reset_phase"}
            and event.start_mu < update.start_mu < event.end_mu
            and binding.device in update.params.get("target_devices", ())
        ):
            raise CompileError(
                f"Raw waveform cannot apply an in-pulse frame update at {update.source}"
            )


def _pulse_phase(
    event,
    binding,
    state,
    frequency,
    correction,
    device,
    card,
    diagnostics,
    modulation_duration_mu,
):
    params = event.params
    phase_evolution = state.pulse(
        start_ticks=event.start_mu,
        duration_ticks=event.duration_mu,
        frequency_hz=frequency,
        phase_scale=binding.phase_scale,
        modulation=params.get("phase_modulation"),
        modulation_mode=params.get("phase_modulation_mode", "local"),
        modulation_duration_ticks=modulation_duration_mu,
    )
    if "sampled" in params:
        phase_evolution["frequency_check_scope"] = (
            "carrier_and_explicit_phase_modulation_excludes_sampled_envelope"
        )
    low = phase_evolution["instantaneous_frequency_min_hz"]
    high = phase_evolution["instantaneous_frequency_max_hz"]
    phase_evolution["frequency_range_exceeds_contract"] = (
        low < device.min_frequency
        or high > min(device.max_frequency, card.sample_rate_hz / 2, 400e6)
    )
    if phase_evolution["frequency_range_exceeds_contract"]:
        diagnostics.append(
            {
                "kind": "spectrum_modulation_frequency_range",
                "device": binding.device,
                "source": event.source,
                "minimum_hz": low,
                "maximum_hz": high,
                "status": "outside_nominal_range_requires_waveform_acceptance",
            }
        )
    phase = (
        phase_evolution["carrier_phase_rad"]
        + binding.phase_scale * params["phase"]
        + binding.phase_offset
        + correction
        + phase_evolution["inherited_modulation_phase_rad"]
    ) % (2 * math.pi)
    return phase, phase_evolution


def resolve_card_pulses(
    members,
    card_name,
    card,
    outputs,
    setup,
    mapping,
    diagnostics,
    *,
    start_mu,
    reference_kind,
    prefix=0,
    calibration=None,
    preceding_resets=(),
):
    """Resolve final pulse samples and phase from an already placed card timeline."""
    from taiyidsl.backends.artiq_spectrum.phase import OutputPhaseState
    from taiyidsl.spectrum_calibration import resolve_channel_calibration

    pulses = []
    reference_ticks = start_mu if reference_kind == "sequence" else 0
    phase_states = {
        name: OutputPhaseState(
            output,
            reference_ticks=reference_ticks,
            reference_kind=reference_kind,
        )
        for name, output in outputs.items()
        if output.card == card_name
    }
    if reference_kind == "shot":
        for event in sorted(preceding_resets, key=lambda item: item.start_mu):
            for device in event.params.get("target_devices", ()):
                if device in phase_states:
                    phase_states[device].reset(event.start_mu)
    ordered_members = sorted(members, key=lambda item: item.start_mu)
    for event in ordered_members:
        if event.kind == "reset_phase":
            for device in event.params.get("target_devices", ()):
                if device in phase_states:
                    phase_states[device].reset(event.start_mu)
            continue
        if event.kind != "play" or event.duration_mu == 0:
            continue
        for binding in mapping.bindings(event.signal):
            output = outputs[binding.device]
            if output.card != card_name:
                continue
            params = event.params
            frequency = (
                params["frequency"] * binding.frequency_scale + binding.frequency_offset
            )
            try:
                channel_cal = resolve_channel_calibration(
                    calibration or {}, card_name, output.channel, frequency
                )
            except ValueError as exc:
                raise CompileError(str(exc)) from exc
            if (
                channel_cal.get("sample_rate_hz", card.sample_rate_hz)
                != card.sample_rate_hz
            ):
                raise CompileError("Spectrum calibration sample rate mismatch")
            gain = channel_cal.get("gain", 1.0)
            phase_correction = channel_cal.get("phase_offset_rad", 0.0)
            delay_ns = channel_cal.get("delay_ns", 0.0)
            if (
                not all(math.isfinite(v) for v in (gain, phase_correction, delay_ns))
                or gain <= 0
            ):
                raise CompileError("Invalid Spectrum channel calibration")
            shift = channel_cal.get(
                "sample_shift_samples",
                round(-Fraction(str(delay_ns)) * card.sample_rate_hz / 1_000_000_000),
            )
            relative_start = _exact_integer(
                Fraction(event.start_mu - start_mu, 4), event.source
            )
            start_sample = prefix + relative_start + shift
            if start_sample < 0:
                raise CompileError("Channel delay correction exceeds trigger pre-roll")
            duration = _exact_integer(Fraction(event.duration_mu, 4), event.source)
            envelope = dict(params.get("sampled", {"kind": "square"}))
            active_samples = duration
            for name in ("pad_before", "pad_after"):
                pad = envelope.pop(f"{name}_s", None)
                if pad is not None:
                    count = _exact_integer(
                        Fraction(**pad) * Fraction(str(card.sample_rate_hz)), name
                    )
                    envelope[f"{name}_samples"] = count
                    active_samples -= count
            device = setup.devices[binding.device]
            raw = params.get("sampled", {}).get("interpretation") == "waveform"
            if not raw and (
                not device.min_frequency
                <= frequency
                <= min(device.max_frequency, card.sample_rate_hz / 2)
            ):
                raise CompileError(
                    "Spectrum carrier frequency exceeds the output contract"
                )
            amplitude = params["amplitude"] * binding.amplitude_scale / gain
            state = phase_states[binding.device]
            if raw:
                _validate_raw_controls(event, binding, phase_correction, state, members)
                # A direct DAC voltage leaves the output oscillator's state alone.
                phase, phase_evolution = 0.0, {"carrier_mode": "none"}
            else:
                phase, phase_evolution = _pulse_phase(
                    event,
                    binding,
                    state,
                    frequency,
                    phase_correction,
                    device,
                    card,
                    diagnostics,
                    active_samples * 4,
                )
            if params.get("shape") == "flat_top_gaussian":
                if Fraction(str(params["sample_interval_s"])) != Fraction(
                    1, card.sample_rate_hz
                ):
                    raise CompileError(
                        "Spectrum Gaussian requires explicit 0.8 ns sample_interval"
                    )
                envelope = {
                    "kind": "flat_top_gaussian",
                    "sigma_samples": float(
                        Fraction(str(params["sigma_s"])) * card.sample_rate_hz
                    ),
                    "cutoff_sigma": params["cutoff_sigma"],
                    "flat_samples": _exact_integer(
                        Fraction(str(params["flat_duration_s"])) * card.sample_rate_hz,
                        "Gaussian plateau",
                    ),
                }
            phase_steps = []
            previous = params.get("frame_phase", 0.0)
            for update in ordered_members:
                if (
                    update.kind not in {"shift_phase", "reset_frame"}
                    or not event.start_mu < update.start_mu < event.end_mu
                    or event.signal not in update.params.get("target_signals", ())
                ):
                    continue
                target = update.params["effective_frame_phase"]
                phase_steps.append(
                    {
                        "sample": _exact_integer(
                            Fraction(update.start_mu - event.start_mu, 4), update.source
                        ),
                        "delta_rad": binding.phase_scale * (target - previous),
                    }
                )
                previous = target
            pulses.append(
                {
                    "channel": output.channel,
                    "start_sample": start_sample,
                    "duration_samples": duration,
                    "frequency_hz": frequency,
                    "amplitude": amplitude,
                    "phase_rad": phase,
                    "frame_phase": params.get("frame_phase", 0.0),
                    "virtual_z_phase": params.get("virtual_z_phase", 0.0),
                    "phase_modulation": copy.deepcopy(params.get("phase_modulation")),
                    "phase_scale": binding.phase_scale,
                    "phase_evolution": phase_evolution,
                    "operation_path": list(params.get("operation_path", ())),
                    "section_schedule": copy.deepcopy(
                        params.get("section_schedule", [])
                    ),
                    "phase_steps": phase_steps,
                    "envelope": envelope,
                    "logical_id": event.logical_ids,
                    "source": event.source,
                }
            )
            residual = delay_ns + shift * 0.8
            if residual:
                diagnostics.append(
                    {
                        "kind": "spectrum_fractional_delay_residual",
                        "device": binding.device,
                        "residual_ns": residual,
                        "status": "requires_hardware_acceptance",
                    }
                )
    return pulses


def _lower(logical, setup, mapping, diagnostics):
    from taiyidsl.backends.artiq_spectrum.awg import build_card_program
    from taiyidsl.spectrum_identity import calibration_fingerprint

    factor = native_factor(setup)
    for index, event in enumerate(logical):
        event.logical_ids = (f"e{index}",)
    native = native_setup(setup)
    native_map = native_mapping(mapping, setup)
    regions = [e for e in logical if e.kind == "spectrum_region"]
    outside = [
        native_event(e, factor)
        for e in logical
        if e.kind != "spectrum_region"
        and not e.params.get("region_id")
        and e.params.get("route") not in {"native_artiq", "logical_frame"}
    ]
    programs = []
    for region in regions:
        group_name = region.params["group"]
        group = setup.spectrum.groups[group_name]
        members = [
            e
            for e in logical
            if e.params.get("region_id") == region.params["region_id"]
            and e.kind != "spectrum_region"
        ]
        preceding_resets = [
            event
            for event in logical
            if event.kind == "reset_phase"
            and event.params.get("route") == "logical_frame"
            and event.params.get("group") == group_name
            and event.start_mu <= region.start_mu
        ]
        if not any(
            e.kind in {"play", "move"} and e.duration_mu for e in members
        ) and not region.params.get("transport_card"):
            # A time-only block still consumes the requested logical time; it
            # requires no output because runtime leaves all unused outputs off.
            continue
        for event in outside:
            if event.kind not in {"play", "trig", "trigger"}:
                continue
            if any(
                binding.device in group.trigger_device_names
                for binding in native_map.bindings(event.signal)
            ):
                raise CompileError(
                    f"Spectrum group {group_name} owns trigger device "
                    f"{group.trigger_device_names} for this shot; "
                    f"manual {event.kind} on "
                    f"{event.signal} is not allowed at {event.source}"
                )
        trigger_start = (
            _exact_integer(Fraction(region.start_mu, factor), region.source)
            - group.trigger_lead_mu
        )
        trigger_routes = []
        for index, trigger_name in enumerate(group.trigger_device_names):
            trigger_device = native.devices[trigger_name]
            if trigger_start % trigger_device.grid_mu:
                raise CompileError(
                    "Region start cannot produce an exact ARTIQ trigger edge"
                )
            trigger_signal = f"__spectrum_trigger_{group_name}"
            if len(group.trigger_device_names) > 1:
                trigger_signal += f"_{index}"
            if trigger_signal in native_map:
                raise CompileError("Reserved Spectrum trigger signal is already mapped")
            native_map[trigger_signal] = trigger_name
            trigger_routes.append(
                {
                    "signal": trigger_signal,
                    "device": trigger_name,
                    "relative_mu": trigger_start,
                    "width_mu": group.trigger_width_mu,
                }
            )
            outside.append(
                ScheduledEvent(
                    "trigger",
                    trigger_signal,
                    trigger_start,
                    group.trigger_width_mu,
                    {"spectrum_group": group_name},
                    region.source,
                    logical_ids=region.logical_ids,
                )
            )
        cards = {}
        physical_end = region.end_mu
        for card_name, card in group.cards.items():
            contract = {
                **card.to_dict(),
                "trigger_device": group.card_trigger_devices[card_name],
                "epoch_grid_mu": group.epoch_grid_mu,
                "epoch_offset_mu": group.epoch_offset_mu,
                "trigger_width_mu": group.trigger_width_mu,
                "artiq_tick_seconds": setup.spectrum.artiq_tick_seconds,
            }
            if card.mode == "dds":
                from taiyidsl.backends.artiq_spectrum.dds import build_dds_program

                moves = [
                    e
                    for e in members
                    if e.kind == "move" and e.params["card"] == card_name
                ]
                initial_tones = region.params.get("transport_initial_tones")
                if initial_tones is None:
                    raise CompileError(
                        f"DDS card {card_name} needs a declared transport hold state"
                    )
                prefix = _dds_prefix_mu(group, card, setup)
                contract["calibration_sha256"] = calibration_fingerprint(
                    group.calibration
                )
                program = build_dds_program(
                    contract,
                    initial_tones,
                    [
                        {
                            **copy.deepcopy(e.params),
                            "start_mu": e.start_mu - region.start_mu,
                        }
                        for e in moves
                    ],
                    region.duration_mu,
                    prefix_mu=prefix,
                )
                cards[card_name] = {"contract": contract, "program": program}
                physical_end = max(
                    physical_end, region.start_mu + program["duration_mu"]
                )
                continue
            prefix = _prefix_samples(group, card, setup)
            pulses = resolve_card_pulses(
                members,
                card_name,
                card,
                group.outputs,
                setup,
                mapping,
                diagnostics,
                start_mu=region.start_mu,
                reference_kind=region.params.get("phase_reference", "sequence"),
                prefix=prefix,
                calibration=group.calibration,
                preceding_resets=preceding_resets,
            )
            duration = prefix + _exact_integer(
                Fraction(region.duration_mu, 4), region.source
            )
            duration = max(
                duration,
                max(
                    (p["start_sample"] + p["duration_samples"] for p in pulses),
                    default=0,
                ),
            )
            program = build_card_program(
                contract,
                pulses,
                duration,
                max_tile_samples=group.max_tile_samples,
                sample_assets=card_sample_assets(pulses, setup),
            )
            program["calibration_sha256"] = calibration_fingerprint(group.calibration)
            first_sample = (
                region.start_mu
                - group.trigger_lead_mu * factor
                + card.trigger_latency_samples * 4
            )
            physical_end = max(
                physical_end, first_sample + program["physical_duration_samples"] * 4
            )
            cards[card_name] = {"contract": contract, "program": program}
        programs.append(
            ScheduledEvent(
                "spectrum_program",
                group_name,
                region.start_mu,
                physical_end - region.start_mu,
                {
                    "name": group_name,
                    "region_id": region.params["region_id"],
                    "cards": cards,
                    "calibration": copy.deepcopy(group.calibration),
                    "trigger_device": (
                        group.trigger_device_names[0]
                        if len(group.trigger_device_names) == 1
                        else None
                    ),
                    "trigger_devices": group.card_trigger_devices,
                    "trigger_routes": trigger_routes,
                    "trigger_relative_mu": trigger_start,
                    "epoch_grid_mu": group.epoch_grid_mu,
                    "epoch_offset_mu": group.epoch_offset_mu,
                    "phase_reference": {
                        "mode": region.params.get("phase_reference", "sequence"),
                        "epoch": (
                            "triggered_sequence_start"
                            if region.params.get("phase_reference", "sequence")
                            == "sequence"
                            else "shot_logical_T0"
                        ),
                        "reference_start_ticks": (
                            region.start_mu
                            if region.params.get("phase_reference", "sequence")
                            == "sequence"
                            else 0
                        ),
                        "region_start_ticks": region.start_mu,
                        "tick_seconds": HYBRID_TICK_SECONDS,
                        "frame_convention": "base + F + local_modulation",
                        "carrier_during_idle": "analytically accumulated",
                        "physical_formula": (
                            "C + M + s*(base + F + modulation) + offset + calibration"
                        ),
                        "output_modes": {
                            name: {
                                "carrier_mode": output.carrier_mode,
                                "initial_frequency_hz": output.initial_frequency_hz,
                            }
                            for name, output in group.outputs.items()
                            if group.cards[output.card].mode == "awg"
                        },
                    },
                    "trigger_time_unit_s": setup.spectrum.artiq_tick_seconds,
                    "driver_parameter_time_unit_s": setup.spectrum.artiq_tick_seconds,
                    "terminal_frames": copy.deepcopy(
                        region.params.get("terminal_frames", {})
                    ),
                    "entry_frames": copy.deepcopy(
                        region.params.get("entry_frames", {})
                    ),
                },
                region.source,
                resources=tuple(group.outputs),
                logical_ids=region.logical_ids,
            )
        )
        if "trigger_signal" in region.params:
            programs[-1].params["trigger_signal"] = region.params["trigger_signal"]
    native_ids = {f"e{index}": event.logical_ids for index, event in enumerate(outside)}
    native_plan = ArtiqBackend().lower(outside, native, native_map, diagnostics)
    physical = []
    for event in native_plan.events:
        converted = copy.deepcopy(event)
        converted.start_mu *= factor
        converted.duration_mu *= factor
        converted.logical_ids = tuple(
            item for local in event.logical_ids for item in native_ids[local]
        )
        # The ARTIQ driver's parameter payload deliberately remains in native mu.
        physical.append(converted)
    for program in programs:
        program.start_mu += native_plan.origin_mu * factor
        program.params["trigger_physical_mu"] = (
            program.params["trigger_relative_mu"] + native_plan.origin_mu
        )
        for route in program.params["trigger_routes"]:
            route["physical_mu"] = route["relative_mu"] + native_plan.origin_mu
        physical.append(program)
    return PhysicalPlan(
        sorted(physical, key=lambda e: e.start_mu), native_plan.origin_mu * factor
    )
