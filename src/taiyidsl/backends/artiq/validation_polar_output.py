"""Replay packed POWASF words independently of compiler effect annotations.

This is a quantized, zero-order-hold output model, not a hardware measurement.
The declared trigger-to-output delays are inputs to the check, not its findings.
"""

import hashlib
import json
import math
from bisect import bisect_right
from collections import defaultdict

from taiyidsl.backends.artiq.waveforms import ad9910_amplitude_ram
from taiyidsl.config.hardware import DeviceCalibration
from taiyidsl.ir import CompileError

_PHASE_COMMANDS = {"rz", "shift_phase", "reset_frame", "reset_phase"}
_LAUNCHES = {"dds_polar_select", "dds_polar_retrigger"}


def _pow(phase):
    return round((phase % (2 * math.pi)) / (2 * math.pi) * 65536) & 0xFFFF


def _stream(changes, initial):
    """Sorted transitions with last assignment winning at an identical time."""
    values = {-(10**30): initial}
    values.update(changes)
    return sorted(values), values


def _value(stream, when):
    times, values = stream
    return values[times[bisect_right(times, when) - 1]]


def _binding_targets(compiled, frame, signal_frames):
    for signal, owner in signal_frames.items():
        if owner == frame and signal in compiled.signal_map:
            yield from compiled.signal_map.bindings(signal)


def _signal_frame_associations(compiled):
    return {
        q.signal(route): q.drive_frame(route)
        for q in compiled.qubits.values()
        for route in q.drive_names
    }


def _logical_outputs(compiled, point, names):
    """Derive requested envelopes and carrier offsets from the recorded DSL IR.

    Do not use physical effect events, their POW targets, prefix lengths, or bank
    ASF metadata as the oracle. Mapping/calibration and the public envelope
    sampler are shared with compilation; packed playback words are not.
    """
    outputs = {
        name: {
            "phase": {},
            "asf": {},
            "rf_changes": defaultdict(int),
            "ftws": set(),
            "initial_ftws": set(),
        }
        for name in names
    }
    frames, references, base = {}, {}, {}
    # Derive frame ownership from public qubit snapshots, independently of
    # lowering's packed words and frame-effect annotations. None stays unbound.
    signal_frames = _signal_frame_associations(compiled)
    for name in names:
        cal = compiled.setup.calibration.get(name, DeviceCalibration())
        outputs[name]["latency"] = (
            compiled.setup.devices[name].latency_mu + cal.latency_mu
        )
    for signal in compiled.signal_map:
        for binding in compiled.signal_map.bindings(signal):
            if binding.device not in outputs:
                continue
            cal = compiled.setup.calibration.get(binding.device, DeviceCalibration())
            initial = binding.phase_offset + cal.phase_offset
            old = outputs[binding.device].setdefault("initial_phase", initial)
            if _pow(old) != _pow(initial):
                raise CompileError(
                    "POWASF verification found conflicting initial phases"
                )
            base.setdefault(binding.device, initial)

    for event in sorted(point.logical_events, key=lambda item: item.start_mu):
        if event.kind in _PHASE_COMMANDS:
            uid = event.signal
            if event.kind == "rz":
                frames[uid] = frames.get(uid, 0.0) - event.params["angle"]
            elif event.kind in {"reset_frame", "reset_phase"}:
                frames[uid] = 0.0
                if event.kind == "reset_phase":
                    references[uid] = event.start_mu
            else:
                frames[uid] = frames.get(uid, 0.0) + event.params["angle"]
            frame = frames.get(uid, 0.0)
            for signal, initial in event.params.get("initial_states", {}).items():
                if signal not in compiled.signal_map:
                    continue
                for binding in compiled.signal_map.bindings(signal):
                    if binding.device not in outputs:
                        continue
                    cal = compiled.setup.calibration.get(
                        binding.device, DeviceCalibration()
                    )
                    frequency = initial["frequency"] * binding.frequency_scale
                    frequency += binding.frequency_offset + cal.frequency_offset
                    outputs[binding.device]["initial_ftws"].add(
                        round(
                            frequency
                            / compiled.setup.devices[binding.device].sysclk_hz
                            * 2**32
                        )
                    )
            for binding in _binding_targets(compiled, uid, signal_frames):
                name = binding.device
                if name not in outputs:
                    continue
                target = outputs[name]
                when = point.origin_mu + event.start_mu - target["latency"]
                reference = (
                    references[uid] - target["latency"] if uid in references else 0
                )
                target["phase"][when] = (
                    base[name] + frame * binding.phase_scale,
                    reference,
                )
        elif event.kind == "play" and event.duration_mu:
            for binding in compiled.signal_map.bindings(event.signal):
                name = binding.device
                if name not in outputs:
                    continue
                target = outputs[name]
                cal = compiled.setup.calibration.get(name, DeviceCalibration())
                uid = signal_frames.get(event.signal)
                drive = uid is not None
                p = event.params
                frequency = p["frequency"] * binding.frequency_scale
                frequency += binding.frequency_offset + cal.frequency_offset
                target["ftws"].add(
                    round(frequency / compiled.setup.devices[name].sysclk_hz * 2**32)
                )
                if drive:
                    phase = p.get(
                        "requested_phase", p["phase"] - p.get("frame_phase", 0.0)
                    )
                    frame = frames.get(uid, 0.0)
                else:
                    phase, frame = p["phase"], 0.0
                base[name] = phase * binding.phase_scale + binding.phase_offset
                base[name] += cal.phase_offset
                start = point.origin_mu + event.start_mu - target["latency"]
                reference = (
                    references[uid] - target["latency"] if uid in references else 0
                )
                target["phase"][start] = (
                    base[name] + frame * binding.phase_scale,
                    reference,
                )
                parameters = dict(p)
                parameters["amplitude"] *= binding.amplitude_scale * cal.amplitude_scale
                samples = ad9910_amplitude_ram(
                    parameters,
                    compiled.setup.devices[name].sysclk_hz,
                    event.duration_mu * compiled.setup.tick_seconds,
                )
                step = round(
                    samples["ram_sample_interval_s"] / compiled.setup.tick_seconds
                )
                for index, asf in enumerate(samples["ram_asf"]):
                    target["asf"][start + index * step] = asf
                target["rf_changes"][start] += 1
                target["rf_changes"][start + event.duration_mu] -= 1
    for name, target in outputs.items():
        target["ftws"] = target["ftws"] or target.pop("initial_ftws")
        state, edges = 0, {}
        for when, delta in sorted(target.pop("rf_changes").items()):
            state += delta
            edges[when] = bool(state)
        target["rf"] = _stream(edges, False)
        device = compiled.setup.devices[name]
        releases = [
            event
            for event in point.physical_events
            if event.kind == "dds_nco_epoch" and name in event.params["commit_members"]
        ]
        if len(target["ftws"]) != 1 or len(releases) > 1:
            raise CompileError(
                "POWASF logical replay requires a fixed FTW and one epoch"
            )
        ftw = next(iter(target["ftws"]))
        cycles = round(device.sysclk_hz * compiled.setup.tick_seconds)
        epoch_word = (
            (
                (releases[0].start_mu + device.nco_epoch_latency_mu - point.origin_mu)
                * ftw
                * cycles
            )
            if releases
            else 0
        )
        target["phase"] = _stream(
            {
                when: (
                    _pow(phase)
                    + ((epoch_word - reference * ftw * cycles) >> 16)
                    - (epoch_word >> 16)
                )
                & 0xFFFF
                for when, (phase, reference) in target["phase"].items()
            },
            _pow(target["initial_phase"]),
        )
        target["asf"] = _stream(target["asf"], 0)
    return outputs


def _decode_bank(bank):
    decoded, addresses = {}, set()
    for entry in bank:
        profile = entry["profile"]
        if profile in decoded or not 0 <= profile < 8:
            raise CompileError("Invalid POWASF profile library index")
        words = entry["words"]
        if not words or entry["end"] - entry["start"] + 1 != len(words):
            raise CompileError("POWASF RAM address span does not match packed words")
        span = set(range(entry["start"], entry["end"] + 1))
        if addresses.intersection(span) or min(span) < 0 or max(span) >= 1024:
            raise CompileError(
                "POWASF RAM library addresses overlap or exceed capacity"
            )
        addresses.update(span)
        if entry["mode"] not in {0, 1} or not 1 <= entry["step"] <= 65535:
            raise CompileError("POWASF output model supports DIRECTSWITCH/RAMPUP only")
        if any(
            type(word) is not int or not 0 <= word < 2**32 or word & 3 for word in words
        ):
            raise CompileError("Invalid packed POWASF word")
        decoded[profile] = {
            "phase": [(word >> 16) & 0xFFFF for word in words],
            "asf": [(word >> 2) & 0x3FFF for word in words],
            "step": entry["step"],
            "mode": entry["mode"],
        }
    return decoded, len(addresses)


def _epoch(compiled, point, name, launches):
    device = compiled.setup.devices[name]
    releases = [
        event
        for event in point.physical_events
        if event.kind == "dds_nco_epoch" and name in event.params["commit_members"]
    ]
    if len(releases) != 1 or device.nco_mode != "continuous":
        raise CompileError("POWASF requires exactly one continuous NCO epoch per shot")
    ftws = {event.params["ftw"] for event in launches}
    if len(ftws) != 1:
        raise CompileError("POWASF output replay found a changing FTW")
    ftw = ftws.pop()
    epoch = releases[0].start_mu + device.nco_epoch_latency_mu
    cycles = round(device.sysclk_hz * compiled.setup.tick_seconds)
    accumulator = (epoch - point.origin_mu) * ftw * cycles
    correction = (accumulator >> 16) & 0xFFFF
    for event in launches:
        if (
            event.params["nco_epoch_effect_mu"] != epoch
            or event.params["nco_epoch_pow"] != correction
            or event.params["nco_mode"] != "continuous"
        ):
            raise CompileError("POWASF NCO epoch/correction differs between launches")
    forbidden = {"dds_config", "dds_ram_rearm", "dds_ram_config", "dds_square_prepare"}
    if any(e.signal == name and e.kind in forbidden for e in point.physical_events):
        raise CompileError(
            "POWASF continuous output contains an incompatible mode/reset"
        )
    # This is the residual of the initial reference. Explicit carrier resets
    # choose a new POW against the same running 32-bit accumulator.
    return correction, -(accumulator & 0xFFFF)


def _replay(compiled, point, name, launches):
    device = compiled.setup.devices[name]
    bank = launches[0].params["polar_bank"]
    if any(event.params["polar_bank"] != bank for event in launches):
        raise CompileError("POWASF profile library changes during a shot")
    decoded, word_count = _decode_bank(bank)
    correction, residual = _epoch(compiled, point, name, launches)
    initial = launches[0].params["polar_initial_profile"]
    if initial not in decoded or decoded[initial]["mode"] != 0:
        raise CompileError("POWASF initial profile must be a resident static hold")
    phase, asf = {}, {}
    triggers = []
    for event in launches:
        if event.kind == "dds_polar_select":
            factor = 52 if device.ram_profile_control == "urukul_rev8" else 112
            if event.duration_mu != factor * device.spi_ref_period_mu:
                raise CompileError("POWASF selector duration differs from real driver")
            trigger = event.end_mu
            l_amp = device.ram_profile_latency_mu
            l_phase = device.ram_profile_phase_latency_mu
        else:
            if event.duration_mu:
                raise CompileError("POWASF IO_UPDATE trigger cannot advance the cursor")
            trigger = event.start_mu
            l_amp = device.ram_latency_mu
            l_phase = device.ram_polar_phase_latency_mu
        if l_amp is None or l_phase is None:
            raise CompileError("POWASF output replay lacks declared pipeline delays")
        triggers.append((trigger, l_amp, l_phase, event.params["polar_profile"]))
    triggers.sort()
    for index, (trigger, l_amp, l_phase, profile) in enumerate(triggers):
        if profile not in decoded:
            raise CompileError("POWASF trigger selects an absent resident profile")
        entry = decoded[profile]
        raw_step = 4 * entry["step"] / device.sysclk_hz / compiled.setup.tick_seconds
        step = round(raw_step)
        if not math.isclose(raw_step, step, abs_tol=1e-8):
            raise CompileError("POWASF model needs integral sample ticks")
        # Selection and IO_UPDATE use different physical reference points.
        # Compare at the common envelope-output plane, not their raw trigger
        # timestamps. Their equal envelope-minus-phase differences establish
        # the corresponding phase cutoff without guessing pin propagation.
        stop = sum(triggers[index + 1][:2]) if index + 1 < len(triggers) else math.inf
        count = len(entry["phase"]) if entry["mode"] == 1 else 1
        for sample in range(count):
            emitted = trigger + sample * step
            if emitted + l_amp >= stop:
                break
            phase[emitted + l_phase] = (entry["phase"][sample] - correction) & 0xFFFF
            asf[emitted + l_amp] = entry["asf"][sample]
    rf = {
        event.start_mu: event.kind == "dds_on"
        for event in point.physical_events
        if event.signal == name and event.kind in {"dds_on", "dds_off"}
    }
    replay = {
        "phase": _stream(phase, (decoded[initial]["phase"][0] - correction) & 0xFFFF),
        "asf": _stream(asf, decoded[initial]["asf"][0]),
        "rf": _stream(rf, False),
    }
    references = sorted(
        {
            event.params["carrier_reference_mu"]
            for event in launches
            if "carrier_reference_mu" in event.params
        }
    )
    summary = {
        "device": name,
        "profiles": len(bank),
        "ram_words": word_count,
        "library_sha256": hashlib.sha256(
            json.dumps(bank, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "launches": len(launches),
        "selector": device.ram_profile_control,
        "selector_duration_mu": (
            52 if device.ram_profile_control == "urukul_rev8" else 112
        )
        * device.spi_ref_period_mu,
        "carrier_reference_resets": references,
        "reference_quantization_bound_pow_lsb": 1,
        "first_phase_output_mu": min(t + lp for t, _, lp, _ in triggers),
    }
    summary[
        "initial_nco_quantization_residual_u32"
        if references
        else "constant_nco_quantization_residual_u32"
    ] = residual
    return replay, summary


def verify_polar_output(compiled):
    """Check packed RAM output against logical pulses and hardware frame commands.

    Claimed ``dds_polar_effect`` timestamps and POWs are deliberately ignored.
    Comparison covers phase while RF is off as well as the entire envelope and
    RF windows. No hardware, ARTIQ compiler, or real driver is invoked here.
    """
    compiled.assert_integrity()
    signal_frames = _signal_frame_associations(compiled)
    points, checked = [], 0
    for point in compiled.points:
        launches = defaultdict(list)
        for event in point.physical_events:
            if event.kind in _LAUNCHES:
                launches[event.signal].append(event)
        names = set(launches)
        for event in point.logical_events:
            bindings = ()
            if event.kind == "play" and event.duration_mu:
                bindings = compiled.signal_map.bindings(event.signal)
            elif event.kind in _PHASE_COMMANDS:
                bindings = _binding_targets(compiled, event.signal, signal_frames)
            names.update(
                binding.device
                for binding in bindings
                if compiled.setup.devices[binding.device].ram_destination == "powasf"
            )
        expected = _logical_outputs(compiled, point, names)
        channels = []
        for name in sorted(names):
            events, desired = launches[name], expected[name]
            if not events:
                if (
                    any(desired["rf"][1].values())
                    or len(set(desired["phase"][1].values())) > 1
                ):
                    raise CompileError(
                        f"Missing POWASF hardware launch for logical output on {name}"
                    )
                continue
            actual_ftws = {event.params["ftw"] for event in events}
            if len(actual_ftws) > 1:
                raise CompileError("POWASF output replay found a changing FTW")
            if actual_ftws != desired["ftws"]:
                raise CompileError(
                    "POWASF FTW differs from the logical carrier frequency"
                )
            actual, summary = _replay(compiled, point, name, events)
            begin = min(
                point.origin_mu - desired["latency"],
                summary["first_phase_output_mu"] - 1,
            )
            end = max(
                point.origin_mu + point.duration_mu - desired["latency"],
                max(desired["phase"][0]),
                max(actual["phase"][0]),
                max(actual["asf"][0]),
            )
            for field in ("phase", "asf", "rf"):
                boundaries = {
                    begin,
                    end,
                    end + 1,
                    *(time for time in actual[field][0] if begin <= time <= end),
                    *(time for time in desired[field][0] if begin <= time <= end),
                }
                for when in sorted(boundaries):
                    observed = _value(actual[field], when)
                    wanted = _value(desired[field], when)
                    checked += 1
                    if observed != wanted:
                        raise CompileError(
                            f"POWASF {field} output mismatch on {name} at "
                            f"{when - point.origin_mu} mu: packed RAM gives "
                            f"{observed}, logical sequence requires {wanted}"
                        )
            channels.append(summary)
        points.append({"index": point.index, "channels": channels})
    return {
        "status": "passed" if any(p["channels"] for p in points) else "not_applicable",
        "model": "packed_words_separate_pipeline_zero_order_hold",
        "checked_boundaries": checked,
        "points": points,
        "effect_annotations_used_as_oracle": False,
        "delays": "declared_unmeasured_inputs",
        "real_driver_audit": "not_run_by_this_check",
        "real_artiq_compile": "not_run_by_this_check",
        "hardware_verified": False,
    }
