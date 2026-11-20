"""Compile shot-local Square pulses to timed DDS20 phase-jump commands.

Times are samples of the 1.25 GHz DAC. The command timer advances every eight
samples. DDS cores output sine, while the DSL defines real RF using cosine.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from bisect import bisect_right
from collections import defaultdict
from fractions import Fraction

from taiyidsl.backends.artiq_spectrum.awg import _pulses
from taiyidsl.backends.artiq_spectrum.dds import (
    AMPLITUDE_STEP,
    COMMAND_GUARD_CYCLES,
    FREQUENCY_STEP_HZ,
    MAX_TIMER_CYCLES,
    QUEUE_CAPACITY,
    dds_channel_cores,
)
from taiyidsl.config.spectrum import SAMPLE_RATE_HZ
from taiyidsl.ir import CompileError

PHASE_STEP_DEG = 360 / 4096
SAMPLES_PER_CYCLE = 8


def _digest(program):
    fields = {key: value for key, value in program.items() if key != "sha256"}
    data = json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def _cores(contract):
    if (
        contract["mode"] != "dds"
        or contract["model"] != "M4i.6631-x8"
        or contract["sample_rate_hz"] != SAMPLE_RATE_HZ
    ):
        raise CompileError("Square DDS requires a 1.25 GS/s M4i.6631-x8 DDS20")
    channels = tuple(contract["active_channels"])
    if channels not in {(0,), (1,), (0, 1)} or any(
        type(c) is not int for c in channels
    ):
        raise CompileError("Invalid active DDS channels")
    routes = dds_channel_cores(contract.get("dds_routing", "16/5"))
    return {channel: routes[channel][0] for channel in channels}


def _check_sample(sample, source):
    if type(sample) is not int or sample < 0 or sample % SAMPLES_PER_CYCLE:
        raise CompileError(f"DDS timing must lie on the 6.4 ns grid at {source}")


def _square_pulses(contract, pulses, duration):
    pulses = list(pulses)
    for pulse in pulses:
        source = pulse.get("source", "unnamed pulse")
        if pulse.get("envelope", {}).get("kind", "square") != "square":
            raise CompileError(f"DDS play requires a Square envelope at {source}")
        if pulse.get("phase_modulation") is not None:
            raise CompileError(f"DDS play cannot realize phase modulation at {source}")
        _check_sample(pulse["start_sample"], source)
        _check_sample(pulse["duration_samples"], source)
        for step in pulse.get("phase_steps", []):
            _check_sample(step["sample"], source)
    _check_sample(duration, "shot end")
    return _pulses(pulses, contract, duration)


def _command_count(core_count, uses_timer):
    # Frequency, amplitude and phase per core; trigger source, EXEC, optional timer.
    return 3 * core_count + 2 + int(uses_timer)


def _check_timer(cycles, next_writes, at_sample):
    required = COMMAND_GUARD_CYCLES + next_writes
    if type(cycles) is not int or not required <= cycles <= MAX_TIMER_CYCLES:
        raise CompileError(
            f"DDS timer at sample {at_sample} requires {required}..{MAX_TIMER_CYCLES} "
            f"command cycles for the next frame, got {cycles}"
        )


def _tone(channel, core, active, sample):
    tone = dict(
        channel=channel, core=core, frequency_hz=0.0, amplitude=0.0, phase_deg=0.0
    )
    if active is not None:
        pulse, local_phase = active
        freq_word = round(pulse["frequency_hz"] / FREQUENCY_STEP_HZ)
        # The accumulator runs at the realized DDS frequency. Multiplication in
        # integer words preserves the phase through long pulses on the other channel.
        elapsed = sample - pulse["start_sample"]
        cycles = ((elapsed * freq_word) % 2**32) / 2**32
        sine_phase = 90 + math.degrees(local_phase) + 360 * cycles
        phase_word = round(sine_phase / PHASE_STEP_DEG) % 4096
        tone.update(
            frequency_hz=freq_word * FREQUENCY_STEP_HZ,
            amplitude=round(pulse["amplitude"] / AMPLITUDE_STEP) * AMPLITUDE_STEP,
            phase_deg=phase_word * PHASE_STEP_DEG,
        )
    return tone


def build_square_program(contract, pulses, duration_samples):
    """Produce one full, bounded command packet, including the terminal mute."""
    cores = _cores(contract)
    pulses = _square_pulses(contract, pulses, duration_samples)
    changes = defaultdict(dict)
    for pulse in pulses:
        channel = pulse["channel"]
        start = pulse["start_sample"]
        phase = pulse["phase_rad"]
        changes[start][channel] = (pulse, phase)
        for step in pulse["phase_steps"]:
            phase += step["delta_rad"]
            changes[start + step["sample"]][channel] = (pulse, phase)
        changes[start + pulse["duration_samples"]][channel] = None
    boundaries = sorted({0, duration_samples, *changes})
    normal_writes = _command_count(len(cores), True)
    total = len(boundaries) * normal_writes - 1
    if total > QUEUE_CAPACITY:
        raise CompileError(
            f"DDS shot exceeds the {QUEUE_CAPACITY}-command SINGLE queue"
        )
    frames, active = [], {}
    for index, sample in enumerate(boundaries):
        active.update(changes[sample])
        cycles = None
        if index < len(boundaries) - 1:
            cycles = (boundaries[index + 1] - sample) // SAMPLES_PER_CYCLE
            next_writes = _command_count(len(cores), index + 2 < len(boundaries))
            _check_timer(cycles, next_writes, sample)
        tones = [
            _tone(channel, core, active.get(channel), sample)
            for channel, core in cores.items()
        ]
        frames.append(dict(at_sample=sample, timer_cycles=cycles, tones=tones))
    program = dict(
        kind="dds_square",
        schema_version=1,
        card_spec=copy.deepcopy(contract),
        phase_behaviour="jump",
        pulses=pulses,
        duration_samples=duration_samples,
        frames=frames,
        execution_count=len(frames),
        command_count=total,
    )
    program["sha256"] = _digest(program)
    return program


def _verify_tone(tone, pulse, sample):
    """Test hardware values against the requested RF on one half-open interval."""
    if pulse is None:
        if any(tone[key] != 0 for key in ("frequency_hz", "amplitude", "phase_deg")):
            raise CompileError("DDS idle/terminal command must be muted")
        return
    frequency, amplitude, phase = (
        tone[k] for k in ("frequency_hz", "amplitude", "phase_deg")
    )
    for actual, target, step in (
        (frequency, pulse["frequency_hz"], FREQUENCY_STEP_HZ),
        (amplitude, pulse["amplitude"], AMPLITUDE_STEP),
    ):
        rounded = round(actual / step) * step
        if rounded != actual or abs(actual - target) > step / 2 + math.ulp(target):
            raise CompileError("DDS frequency/amplitude differs from pulse target")
    if not 0 <= phase < 360 or (phase / PHASE_STEP_DEG) % 1:
        raise CompileError("DDS phase exceeds the register range or resolution")
    elapsed = sample - pulse["start_sample"]
    # Independently compare the requested cosine to the commanded sine. Use
    # exact rational cycles to avoid losing angle precision on a long plateau.
    cycles = Fraction(elapsed * round(frequency / FREQUENCY_STEP_HZ), 2**32) % 1
    delta = sum(
        step["delta_rad"] for step in pulse["phase_steps"] if step["sample"] <= elapsed
    )
    expected = math.degrees(pulse["phase_rad"] + delta) + float(360 * cycles)
    if abs(math.remainder(phase - 90 - expected, 360)) > PHASE_STEP_DEG / 2 + 1e-9:
        raise CompileError("DDS jump phase differs from the shot-referenced carrier")


def validate_square_program(program):
    """Independently check timers, pulse coverage, oscillator settings and identity."""
    try:
        if (program["kind"], program["schema_version"], program["phase_behaviour"]) != (
            "dds_square",
            1,
            "jump",
        ):
            raise CompileError("Unsupported square DDS program")
        cores = _cores(program["card_spec"])
        duration = program["duration_samples"]
        pulses = _square_pulses(program["card_spec"], program["pulses"], duration)
        frames = program["frames"]
        samples = [frame["at_sample"] for frame in frames]
        boundaries = {0, duration}
        for pulse in pulses:
            start = pulse["start_sample"]
            boundaries.update((start, start + pulse["duration_samples"]))
            boundaries.update(start + step["sample"] for step in pulse["phase_steps"])
        if samples != sorted(boundaries) or len(samples) != program["execution_count"]:
            raise CompileError("DDS frame times differ from the shot edges")
        count = 0
        for i, frame in enumerate(frames):
            last = i == len(frames) - 1
            count += _command_count(len(cores), not last)
            timer = frame["timer_cycles"]
            if last:
                if timer is not None:
                    raise CompileError("DDS terminal frame must return to card trigger")
            else:
                _check_timer(
                    timer, _command_count(len(cores), i + 2 < len(frames)), samples[i]
                )
                if samples[i] + timer * SAMPLES_PER_CYCLE != samples[i + 1]:
                    raise CompileError("DDS timer differs from the next shot edge")
            if [(t["channel"], t["core"]) for t in frame["tones"]] != list(
                cores.items()
            ):
                raise CompileError("DDS frame has an invalid channel/core assignment")
        if count != program["command_count"] or count > QUEUE_CAPACITY:
            raise CompileError("DDS command count/capacity mismatch")
        for column, channel in enumerate(cores):
            targets = [p for p in pulses if p["channel"] == channel]
            starts = [p["start_sample"] for p in targets]
            for frame in frames:
                sample = frame["at_sample"]
                i = bisect_right(starts, sample) - 1
                pulse = None
                if i >= 0 and sample - starts[i] < targets[i]["duration_samples"]:
                    pulse = targets[i]
                _verify_tone(frame["tones"][column], pulse, sample)
        if program["sha256"] != _digest(program):
            raise CompileError("DDS square program digest mismatch")
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as exc:
        if isinstance(exc, CompileError):
            raise
        raise CompileError(f"Invalid DDS square program: {exc}") from exc
    return True
