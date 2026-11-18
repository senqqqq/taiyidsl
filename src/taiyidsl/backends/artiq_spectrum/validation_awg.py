"""Validate finite playback against an independent mathematical pulse oracle.

Replay/materialization is shared; expected samples are evaluated independently.
"""

import hashlib
import math

import numpy as np

from taiyidsl.ir import CompileError

from .awg import (
    _ALIGNMENT,
    _FULL_SCALE,
    _RENDER_WORK_BYTES,
    RENDER_BYTES_PER_SAMPLE_CHANNEL,
    _card,
    _check_sample_workspace,
    _integer,
    _memory,
    _pulses,
    _walk,
    materialize_segment,
    reconstruct_window,
)


def _expected_window(pulses, channels, rate, start, stop):
    """Independent output oracle: no segment recipe or replay annotation input."""
    _check_sample_workspace(stop - start, len(channels))
    expected = np.zeros((stop - start, len(channels)), dtype=np.float64)
    for pulse in pulses:
        a = max(start, pulse["start_sample"])
        b = min(stop, pulse["start_sample"] + pulse["duration_samples"])
        if a >= b:
            continue
        n = np.arange(a - pulse["start_sample"], b - pulse["start_sample"], dtype=float)
        duration = pulse["duration_samples"]
        angle = pulse["phase_rad"] + 2 * math.pi * (
            (n * pulse["frequency_hz"] / rate) % 1
        )
        modulation = pulse.get("phase_modulation")
        if modulation:
            x = n / duration
            if modulation["kind"] == "half_sine":
                pm = modulation["amplitude"] * np.sin(math.pi * x)
            else:
                pm = sum(
                    c * x**power for power, c in enumerate(modulation["coefficients"])
                )
            angle += pulse["phase_scale"] * pm
        for step in pulse.get("phase_steps", []):
            angle[n >= step["sample"]] += step["delta_rad"]
        env = pulse["envelope"]
        amp = np.full(len(n), pulse["amplitude"])
        if env["kind"] == "flat_top_gaussian":
            edge = env["cutoff_sigma"] * env["sigma_samples"]
            distance = np.where(
                n < edge,
                edge - n,
                np.where(n > duration - edge, n - (duration - edge), 0),
            )
            normalizer = -math.expm1(-(env["cutoff_sigma"] ** 2) / 2)
            amp *= np.clip(
                1
                + np.expm1(-0.5 * (distance / env["sigma_samples"]) ** 2) / normalizer,
                0,
                1,
            )
        expected[a - start : b - start, channels.index(pulse["channel"])] = (
            amp * np.cos(angle)
        )
    return np.rint(expected * _FULL_SCALE).astype("<i2")


def validate_program(program, *, expected_pulses=None, chunk_samples=500000):
    """Check actual finite playback against pulse targets with <=1 LSB tolerance.

    Separate formulas permit one LSB of floating point rounding at half-LSB
    boundaries. SHA256 still checks exact packed bytes. Zero loops are verified
    once plus every intersecting requested pulse, without expanding long idle.
    External expected_pulses can anchor verification to the compiler's input IR.
    """
    card = _card(program["card_spec"])
    chunk = _integer(chunk_samples, "validation chunk", 1)
    limit = _RENDER_WORK_BYTES.get()
    if limit is not None:
        chunk = min(
            chunk,
            limit // (len(card["active_channels"]) * RENDER_BYTES_PER_SAMPLE_CHANNEL),
        )
        if chunk < 1:
            raise CompileError("Sample work budget cannot accommodate validation")
    logical = _integer(program["logical_duration_samples"], "logical duration")
    targets = _pulses(
        program["pulses"] if expected_pulses is None else expected_pulses, card, logical
    )
    segments, steps = program["segments"], program["steps"]
    if len({s["id"] for s in segments}) != len(segments):
        raise CompileError("Spectrum duplicate segment ids")
    if len({s["index"] for s in steps}) != len(steps):
        raise CompileError("Spectrum duplicate sequence step indices")
    if {s["id"] for s in segments} != set(range(len(segments))):
        raise CompileError("Spectrum segment ids must occupy consecutive native slots")
    if {s["index"] for s in steps} != set(range(len(steps))):
        raise CompileError("Spectrum step indices must occupy consecutive native slots")
    if not 1 <= len(steps) <= card["max_steps"]:
        raise CompileError("Spectrum sequence step limit exceeded")
    maximum = _integer(program.get("max_tile_samples", 500000), "tile bound", 1)
    for segment in segments:
        _integer(segment["id"], "segment id")
        size = _integer(
            segment["sample_count"], "segment samples", card["min_segment_samples"]
        )
        if size % _ALIGNMENT:
            raise CompileError("Spectrum segment is not aligned to 32 samples")
        if size > maximum:
            raise CompileError("Spectrum segment exceeds its materialization bound")
    memory = _memory(card, segments)
    if memory != program["memory"]:
        raise CompileError("Spectrum memory accounting does not match segments")
    for segment in segments:
        packed = materialize_segment(program, segment["id"])
        if hashlib.sha256(packed.tobytes()).hexdigest() != segment["sha256"]:
            raise CompileError("Spectrum packed segment hash mismatch")
    for step in steps:
        for field in ("index", "next_step", "segment_id"):
            _integer(step[field], field)
        loops = _integer(step["loops"], "loop count", 1)
        if loops > card["max_loops"]:
            raise CompileError("Spectrum loop count exceeds the finite hardware limit")
        if not isinstance(step["end"], bool):
            raise CompileError("Spectrum end flag must be boolean")
    checked, physical = 0, 0
    for left, right, segment, loops in _walk(program):
        physical = right
        packed = materialize_segment(program, segment["id"])
        if not np.any(packed):
            windows = [
                (
                    max(left, p["start_sample"]),
                    min(right, p["start_sample"] + p["duration_samples"]),
                )
                for p in targets
                if p["amplitude"] != 0
            ]
            for begin, end in windows:
                for offset in range(begin, end, chunk):
                    expected = _expected_window(
                        targets,
                        card["active_channels"],
                        card["sample_rate_hz"],
                        offset,
                        min(end, offset + chunk),
                    )
                    checked += len(expected)
                    if np.any(np.abs(expected.astype(np.int32)) > 1):
                        raise CompileError("Spectrum replay differs from pulse targets")
        else:
            for repeat in range(loops):
                begin = left + repeat * len(packed)
                for offset in range(0, len(packed), chunk):
                    actual = packed[offset : offset + chunk]
                    expected = _expected_window(
                        targets,
                        card["active_channels"],
                        card["sample_rate_hz"],
                        begin + offset,
                        begin + offset + len(actual),
                    )
                    checked += len(actual)
                    if np.any(
                        np.abs(actual.astype(np.int32) - expected.astype(np.int32)) > 1
                    ):
                        raise CompileError("Spectrum replay differs from pulse targets")
    if physical != program["physical_duration_samples"] or physical <= logical:
        raise CompileError("Spectrum physical duration or zero tail is invalid")
    if program["tail_samples"] != physical - logical:
        raise CompileError("Spectrum zero tail length is inconsistent")
    if np.any(reconstruct_window(program, physical - 1, physical)):
        raise CompileError("Spectrum final output sample is not zero")
    return dict(
        status="passed",
        physical_duration_samples=physical,
        checked_samples=checked,
        segment_count=len(segments),
        step_count=len(steps),
        max_quantization_difference_lsb=1,
    )
