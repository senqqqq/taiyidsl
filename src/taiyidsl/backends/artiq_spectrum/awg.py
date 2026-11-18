"""Finite, resident Spectrum AWG programs, without instrument access.

The JSON representation contains recipes, never sample arrays. ``phase_rad`` is
the resolved carrier phase at the pulse's first sample; calibration prefixes
therefore do not enter the carrier calculation here. Samples cover [0, N), and
local modulation uses u = n/N. Polynomial coefficients are in ascending order.
The real RF carrier convention is amplitude * envelope * cos(phase).
All arrays have shape (samples, active_channels), little-endian int16, C order.
"""

import copy
import hashlib
import math
from contextlib import contextmanager
from contextvars import ContextVar

import numpy as np

from taiyidsl.ir import CompileError

_ALIGNMENT = 32
_MAX_LOOPS = 2**20 - 1
_MAX_SEGMENTS = 65536
_MAX_STEPS = 4096
_FULL_SCALE = 32767
_RENDER_WORK_BYTES = ContextVar("spectrum_render_work_bytes", default=None)
# Conservative allowance for float64 carrier/modulation/envelope temporaries,
# channel accumulation, quantization and a simultaneous packed comparison tile.
# Array lengths are bounded by one tile, including polynomial evaluation.
RENDER_BYTES_PER_SAMPLE_CHANNEL = 128


@contextmanager
def render_work_budget(byte_limit):
    """Bound managed renderer arrays without changing waveform arithmetic."""
    token = _RENDER_WORK_BYTES.set(byte_limit)
    try:
        yield
    finally:
        _RENDER_WORK_BYTES.reset(token)


def _check_sample_workspace(samples, channels):
    limit = _RENDER_WORK_BYTES.get()
    workspace = samples * channels * RENDER_BYTES_PER_SAMPLE_CHANNEL
    if limit is not None and workspace > limit:
        raise CompileError("Spectrum renderer exceeds managed sample work budget")


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise CompileError(f"Spectrum {name} must be an integer")
    value = int(value)
    if not minimum <= value <= 2**53 - 1:
        raise CompileError(f"Spectrum {name} is out of range")
    return value


def _finite(value, name):
    if isinstance(value, bool):
        raise CompileError(f"Spectrum {name} must be finite")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise CompileError(f"Spectrum {name} must be finite") from exc
    if not math.isfinite(value):
        raise CompileError(f"Spectrum {name} must be finite")
    return value


def _card(spec):
    channels = [_integer(c, "channel") for c in spec["active_channels"]]
    if channels not in ([0], [1], [0, 1]):
        raise CompileError("M4i.6631 active_channels must be [0], [1], or [0, 1]")
    rate = _finite(spec.get("sample_rate_hz", 1.25e9), "sample rate")
    if rate != 1.25e9:
        raise CompileError("Spectrum waveform profile requires 1.25 GS/s")
    result = copy.deepcopy(spec)
    result.update(
        active_channels=channels,
        sample_rate_hz=rate,
        memory_bytes=_integer(spec.get("memory_bytes", 4 * 2**30), "memory", 1),
        min_segment_samples=384 // len(channels),
    )
    if result["memory_bytes"] > 4 * 2**30:
        raise CompileError("M4i.6631 memory cannot exceed 4 GiB")
    for key, maximum in (
        ("max_segments", _MAX_SEGMENTS),
        ("max_steps", _MAX_STEPS),
        ("max_loops", _MAX_LOOPS),
    ):
        result[key] = _integer(spec.get(key, maximum), key, 1)
        if result[key] > maximum:
            raise CompileError(f"Spectrum {key} exceeds the hardware limit")
    return result


def _pulses(pulses, card, duration):
    result = []
    for source in pulses:
        p = copy.deepcopy(source)
        for field, minimum in (
            ("channel", 0),
            ("start_sample", 0),
            ("duration_samples", 1),
        ):
            p[field] = _integer(p[field], field, minimum)
        if p["channel"] not in card["active_channels"]:
            raise CompileError("Spectrum pulse uses an inactive channel")
        if p["start_sample"] + p["duration_samples"] > duration:
            raise CompileError("Spectrum pulse exceeds logical duration")
        for field in ("frequency_hz", "amplitude", "phase_rad"):
            p[field] = _finite(p[field], field)
        if not 0 <= p["frequency_hz"] <= 400e6:
            raise CompileError("Spectrum frequency exceeds the 400 MHz profile")
        if abs(p["amplitude"]) > 1:
            raise CompileError("Spectrum amplitude would clip")
        p["phase_scale"] = _finite(p.get("phase_scale", 1), "phase_scale")
        env = p.get("envelope", {"kind": "square"})
        if env["kind"] == "flat_top_gaussian":
            sigma = _finite(env["sigma_samples"], "sigma_samples")
            cutoff = _finite(env["cutoff_sigma"], "cutoff_sigma")
            flat = _integer(env["flat_samples"], "flat_samples")
            if sigma <= 0 or cutoff <= 0:
                raise CompileError(
                    "Spectrum Gaussian sigma and cutoff must be positive"
                )
            if -math.expm1(-0.5 * cutoff**2) == 0:
                raise CompileError("Spectrum Gaussian normalization is not finite")
            edge = sigma * cutoff
            if not math.isfinite(edge):
                raise CompileError("Spectrum Gaussian edge must be finite")
            if not math.isclose(edge, round(edge), rel_tol=0, abs_tol=1e-8):
                raise CompileError("Spectrum Gaussian edge must lie on the sample grid")
            if 2 * round(edge) + flat != p["duration_samples"]:
                raise CompileError(
                    "Spectrum Gaussian duration does not match its edges"
                )
            env = dict(
                kind=env["kind"],
                sigma_samples=sigma,
                cutoff_sigma=cutoff,
                flat_samples=flat,
            )
        elif env["kind"] != "square":
            raise CompileError("Unsupported Spectrum envelope")
        p["envelope"] = env
        mod = p.get("phase_modulation")
        if mod is not None:
            if mod["kind"] == "half_sine":
                mod["amplitude"] = _finite(mod["amplitude"], "phase amplitude")
            elif mod["kind"] == "polynomial":
                if not mod["coefficients"]:
                    raise CompileError("Spectrum polynomial requires coefficients")
                mod["coefficients"] = [
                    _finite(c, "phase coefficient") for c in mod["coefficients"]
                ]
            else:
                raise CompileError("Unsupported Spectrum phase modulation")
        steps = {}
        for step in p.get("phase_steps", []):
            sample = _integer(step["sample"], "phase step sample")
            if sample >= p["duration_samples"]:
                raise CompileError("Spectrum phase step must occur inside the pulse")
            delta = _finite(step["delta_rad"], "phase step delta")
            steps[sample] = steps.get(sample, 0.0) + delta
        p["phase_steps"] = [
            {"sample": sample, "delta_rad": delta}
            for sample, delta in sorted(steps.items())
        ]
        result.append(p)
    result.sort(key=lambda p: (p["start_sample"], p["channel"]))
    ends = {}
    for p in result:
        if p["start_sample"] < ends.get(p["channel"], 0):
            raise CompileError("Spectrum pulses overlap on the same channel")
        ends[p["channel"]] = p["start_sample"] + p["duration_samples"]
    return result


def _pulse_values(p, n, rate):
    u = n / p["duration_samples"]
    phase = p["phase_rad"] + 2 * np.pi * np.remainder(
        n * (p["frequency_hz"] / rate), 1.0
    )
    mod = p.get("phase_modulation")
    if mod:
        if mod["kind"] == "half_sine":
            local = mod["amplitude"] * np.sin(np.pi * u)
        else:
            local = np.polynomial.polynomial.polyval(u, mod["coefficients"])
        phase = phase + p["phase_scale"] * local
    for step in p.get("phase_steps", []):
        phase = phase + (n >= step["sample"]) * step["delta_rad"]
    envelope = np.ones(len(n))
    env = p["envelope"]
    if env["kind"] == "flat_top_gaussian":
        sigma, cutoff = env["sigma_samples"], env["cutoff_sigma"]
        distance = np.maximum(
            sigma * cutoff - np.minimum(n, p["duration_samples"] - n), 0
        )
        baseline = math.expm1(-0.5 * cutoff**2)
        envelope = (np.expm1(-0.5 * (distance / sigma) ** 2) - baseline) / (
            -math.expm1(-0.5 * cutoff**2)
        )
        envelope = np.maximum(0, np.minimum(1, envelope))
    return p["amplitude"] * envelope * np.cos(phase)


def _render(recipe, samples, card):
    _check_sample_workspace(samples, len(card["active_channels"]))
    values = np.zeros((samples, len(card["active_channels"])), dtype=np.float64)
    if recipe["kind"] == "fragments":
        for fragment in recipe["fragments"]:
            p = fragment["pulse"]
            count = _integer(fragment["count"], "fragment count", 1)
            dest = _integer(fragment["dest_start"], "fragment destination")
            offset = _integer(fragment["source_start"], "fragment source")
            if dest + count > samples or offset + count > p["duration_samples"]:
                raise CompileError("Spectrum fragment exceeds its segment or pulse")
            channel = card["active_channels"].index(p["channel"])
            values[dest : dest + count, channel] += _pulse_values(
                p,
                np.arange(offset, offset + count, dtype=np.float64),
                card["sample_rate_hz"],
            )
    elif recipe["kind"] != "zero":
        raise CompileError("Unknown Spectrum segment recipe")
    if not np.all(np.isfinite(values)) or np.any(np.abs(values) > 1 + 1e-14):
        raise CompileError("Spectrum samples would clip or are not finite")
    return np.rint(values * _FULL_SCALE).astype("<i2")


def materialize_segment(program, segment_id):
    """Return packed, interleaved DAC samples for one bounded resident segment."""
    for segment in program["segments"]:
        if segment["id"] == segment_id:
            samples = _integer(segment["sample_count"], "segment samples", 1)
            if samples > program.get("max_tile_samples", 500000):
                raise CompileError("Spectrum segment exceeds its materialization bound")
            return _render(segment["recipe"], samples, program["card_spec"])
    raise CompileError(f"Unknown Spectrum segment {segment_id!r}")


def _split_lengths(length, maximum, minimum):
    if maximum < 2 * minimum:
        count = (length + maximum - 1) // maximum
        units, extra = divmod(length // _ALIGNMENT, count)
        if units * _ALIGNMENT < minimum:
            raise CompileError("Spectrum tile limit cannot accommodate native segments")
        for i in range(count):
            yield (units + (i < extra)) * _ALIGNMENT
        return
    while length:
        count = min(length, maximum)
        if 0 < length - count < minimum:
            count = length - minimum
        if count < minimum:
            raise CompileError("Spectrum tile limit cannot accommodate native segments")
        yield count
        length -= count


def _memory(card, segments):
    count = len(segments)
    if not 1 <= count <= card["max_segments"]:
        raise CompileError("Spectrum program exceeds the segment limit")
    slots = 1 << max(1, (count - 1).bit_length())
    width = 2 * len(card["active_channels"])
    capacity = card["memory_bytes"] // slots // width // _ALIGNMENT * _ALIGNMENT
    largest = max(s["sample_count"] for s in segments)
    if largest > capacity:
        raise CompileError("Spectrum equal-partition memory slot capacity exceeded")
    return dict(
        segment_slots=slots,
        slot_capacity_samples=capacity,
        slot_bytes=capacity * width,
        resident_bytes=sum(s["sample_count"] * width for s in segments),
        allocated_bytes=slots * largest * width,
    )


def build_card_program(card_spec, pulses, duration_samples, *, max_tile_samples=500000):
    """Compile exact gate locations into finite native static sequence steps.

    Long all-channel idle spans use repeated zero segments; small remainders
    stay in neighboring tiles. The added zero tail changes physical duration
    only. Equal partition memory is checked after byte-level deduplication.
    """
    card = _card(card_spec)
    duration = _integer(duration_samples, "duration_samples")
    targets = _pulses(pulses, card, duration)
    minimum = card["min_segment_samples"]
    maximum = _integer(max_tile_samples, "max_tile_samples", minimum)
    maximum = maximum // _ALIGNMENT * _ALIGNMENT
    physical = max(minimum, (duration // _ALIGNMENT + 1) * _ALIGNMENT)
    program = dict(
        schema_version=1,
        card_spec=card,
        max_tile_samples=maximum,
        pulses=targets,
        logical_duration_samples=duration,
        physical_duration_samples=physical,
        tail_samples=physical - duration,
        segments=[],
        steps=[],
    )
    by_hash = {}

    def emit(start, count, loops=1, zero=False):
        recipe = {"kind": "zero"}
        if not zero:
            fragments = []
            for p in targets:
                left = max(start, p["start_sample"])
                right = min(start + count, p["start_sample"] + p["duration_samples"])
                if right > left:
                    fragments.append(
                        dict(
                            pulse=copy.deepcopy(p),
                            source_start=left - p["start_sample"],
                            dest_start=left - start,
                            count=right - left,
                        )
                    )
            recipe = dict(kind="fragments", fragments=fragments)
        packed = _render(recipe, count, card)
        if not np.any(packed):
            recipe = {"kind": "zero"}
        digest = hashlib.sha256(packed.tobytes()).hexdigest()
        key = (count, digest)
        segment_id = by_hash.get(key)
        if segment_id is None:
            segment_id = len(program["segments"])
            program["segments"].append(
                dict(id=segment_id, sample_count=count, sha256=digest, recipe=recipe)
            )
            by_hash[key] = segment_id
            _memory(card, program["segments"])
        elif not np.array_equal(packed, materialize_segment(program, segment_id)):
            raise CompileError("Spectrum segment hash collision")
        while loops:
            steps = program["steps"]
            if (
                steps
                and steps[-1]["segment_id"] == segment_id
                and steps[-1]["loops"] < card["max_loops"]
            ):
                take = min(loops, card["max_loops"] - steps[-1]["loops"])
                steps[-1]["loops"] += take
            else:
                take = min(loops, card["max_loops"])
                steps.append(
                    dict(
                        index=len(steps),
                        segment_id=segment_id,
                        loops=take,
                        next_step=len(steps) + 1,
                        end=False,
                    )
                )
                if len(steps) > card["max_steps"]:
                    raise CompileError(
                        "Spectrum program exceeds the sequence step limit"
                    )
            loops -= take

    def zeros(start, length):
        block = min(4000, maximum)
        repeats, rest = divmod(length, block)
        if 0 < rest < minimum and repeats:
            repeats -= 1
            rest += block
        if repeats:
            emit(start, block, repeats, zero=True)
            start += repeats * block
        if rest:
            for count in _split_lengths(rest, maximum, minimum):
                emit(start, count, zero=True)
                start += count

    ranges = []
    for p in targets:
        if p["amplitude"] == 0:
            continue
        start = p["start_sample"] // _ALIGNMENT * _ALIGNMENT
        end = p["start_sample"] + p["duration_samples"] + _ALIGNMENT - 1
        end = end // _ALIGNMENT * _ALIGNMENT
        end = min(physical, max(end, start + minimum))
        start = min(start, end - minimum)
        if start < minimum:
            start = 0
        if physical - end < minimum:
            end = physical
        if ranges and start - ranges[-1][1] < minimum:
            ranges[-1][1] = max(end, ranges[-1][1])
        else:
            ranges.append([start, end])
    cursor = 0
    for start, end in ranges:
        if cursor < start:
            zeros(cursor, start - cursor)
        cursor = start
        for count in _split_lengths(end - start, maximum, minimum):
            emit(cursor, count)
            cursor += count
    if cursor < physical:
        zeros(cursor, physical - cursor)
    program["steps"][-1].update(end=True, next_step=0)
    program["memory"] = _memory(card, program["segments"])
    return program


def _walk(program):
    """Interpret next_step/end, not list order or annotated duration."""
    steps = {step["index"]: step for step in program["steps"]}
    segments = {segment["id"]: segment for segment in program["segments"]}
    seen, index, position = set(), 0, 0
    while True:
        if index in seen or index not in steps:
            raise CompileError("Spectrum sequence has a cycle or missing next step")
        seen.add(index)
        step = steps[index]
        if step["segment_id"] not in segments:
            raise CompileError("Spectrum step references a missing segment")
        segment = segments[step["segment_id"]]
        length = segment["sample_count"] * step["loops"]
        yield position, position + length, segment, step["loops"]
        position += length
        if step["end"]:
            break
        index = step["next_step"]
    if len(seen) != len(steps):
        raise CompileError("Spectrum sequence has unreachable steps")


def iter_program_samples(
    program, start_sample=0, stop_sample=None, *, chunk_samples=500000
):
    """Yield (absolute sample index, array) for a bounded playback window."""
    start = _integer(start_sample, "window start")
    stop = program["physical_duration_samples"] if stop_sample is None else stop_sample
    stop = _integer(stop, "window stop")
    chunk = _integer(chunk_samples, "chunk_samples", 1)
    if not start <= stop <= program["physical_duration_samples"]:
        raise CompileError("Spectrum reconstruction window is out of range")
    for left, right, segment, _ in _walk(program):
        begin, end = max(left, start), min(right, stop)
        if begin >= end:
            continue
        samples = materialize_segment(program, segment["id"])
        while begin < end:
            offset = (begin - left) % len(samples)
            count = min(chunk, len(samples) - offset, end - begin)
            yield begin, samples[offset : offset + count].copy()
            begin += count


def reconstruct_window(program, start_sample, stop_sample, *, max_samples=1000000):
    """Decode a window, rejecting accidentally enormous allocations."""
    if stop_sample - start_sample > max_samples:
        raise CompileError("Spectrum reconstruction exceeds max_samples")
    arrays = [
        array
        for _, array in iter_program_samples(
            program, start_sample, stop_sample, chunk_samples=max_samples
        )
    ]
    if not arrays:
        return np.empty((0, len(program["card_spec"]["active_channels"])), dtype="<i2")
    return np.concatenate(arrays)
