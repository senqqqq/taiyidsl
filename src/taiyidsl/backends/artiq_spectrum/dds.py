"""Offline, bounded native DDS20 transport programs (200 ps logical ticks).

DDS frames are atomic shadow-register commits. Frequency and amplitude ramps use
native linear slope generators. Amplitude ramps need an explicit precision profile.
The terminal frame disables the timer and keeps the initial, closed trap layout.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping

from taiyidsl.ir import CompileError

DDS_TICK_MU = 32
LOGICAL_TICK_SECONDS = 0.2e-9
DDS_TICK_SECONDS = 6.4e-9
FREQUENCY_STEP_HZ = 1_250_000_000 / 2**32
FREQUENCY_SLOPE_STEP = 1_250_000_000**2 / 2**51
AMPLITUDE_STEP = 2 / 2**16
QUEUE_CAPACITY = 4096
# Conservative software budget for two routing and two ramp-divider writes.
# Their driver buffering is checked at upload; this does not assert that all
# four settings are hardware FIFO commands on every supported driver version.
INITIAL_CONFIGURATION_COMMAND_RESERVE = 4
COMMAND_GUARD_CYCLES = 13
MAX_TIMER_CYCLES = 2**32 - 1


def _integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise CompileError(f"DDS {name} must be an integer >= {minimum}")
    return value


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompileError(f"DDS {name} must be a finite number")
    if not math.isfinite(value):
        raise CompileError(f"DDS {name} must be a finite number")
    return float(value)


def _quantize(value, step):
    return round(value / step) * step


def dds_channel_cores(routing="16/5"):
    """The two physical mux arrangements of the M4i.6631 DDS20 option."""
    if routing == "16/5":
        return {0: (*range(8), *range(12, 20)), 1: (*range(8, 12), 20)}
    if routing == "20/1":
        return {0: tuple(range(20)), 1: (20,)}
    raise CompileError("DDS routing must be '16/5' or '20/1'")


def _card(spec):
    if not isinstance(spec, Mapping):
        raise CompileError("DDS card_spec must be a mapping")
    spec = copy.deepcopy(dict(spec))
    if spec.get("model") != "M4i.6631-x8" or spec.get("mode") != "dds":
        raise CompileError("DDS requires an M4i.6631-x8 card in mode='dds'")
    if spec.get("sample_rate_hz") != 1_250_000_000:
        raise CompileError("DDS20 requires a 1.25 GHz clock")
    if spec.get("reference_clock_hz", 125_000_000) != 125_000_000:
        raise CompileError("DDS requires the 125 MHz reference clock contract")
    channels = list(spec.get("active_channels", ()))
    if channels not in ([0], [1], [0, 1]) or any(
        type(channel) is not int for channel in channels
    ):
        raise CompileError("Invalid DDS active channels")
    spec["active_channels"] = channels
    spec["dds_routing"] = spec.get("dds_routing", "16/5")
    dds_channel_cores(spec["dds_routing"])
    amp_step = spec.get("dds_amplitude_slope_step_per_s")
    if amp_step is not None and _finite(amp_step, "amplitude slope step") <= 0:
        raise CompileError("DDS amplitude slope precision must be positive")
    return spec


def _tones(tones, spec):
    if not isinstance(tones, (list, tuple)) or not tones:
        raise CompileError("DDS requires at least one active trapping tone")
    result, seen = [], set()
    routing = dds_channel_cores(spec["dds_routing"])
    for tone in tones:
        if not isinstance(tone, Mapping):
            raise CompileError("A DDS tone must be a mapping")
        core = _integer(tone.get("core"), "core")
        channel = _integer(tone.get("channel"), "channel")
        if (
            core in seen
            or channel not in spec["active_channels"]
            or core not in routing.get(channel, ())
        ):
            raise CompileError("Duplicate DDS core or incompatible core/channel route")
        seen.add(core)
        frequency = _finite(tone.get("frequency_hz"), "frequency")
        amplitude = _finite(tone.get("amplitude"), "amplitude")
        if not 0 < frequency <= 400e6 or not 0 < amplitude < 1:
            raise CompileError("DDS trap frequency/amplitude is outside its range")
        frequency = _quantize(frequency, FREQUENCY_STEP_HZ)
        amplitude = _quantize(amplitude, AMPLITUDE_STEP)
        if not 0 < frequency <= 400e6 or not 0 < amplitude < 1:
            raise CompileError("DDS tone is invalid after quantization")
        result.append(
            dict(
                core=core, channel=channel, frequency_hz=frequency, amplitude=amplitude
            )
        )
    for channel in spec["active_channels"]:
        if sum(t["amplitude"] for t in result if t["channel"] == channel) > 1:
            raise CompileError("DDS summed tone amplitudes exceed channel full scale")
    return sorted(result, key=lambda tone: tone["core"])


def _same_tones(left, right):
    return left == right


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


_PROGRAM_FIELDS = (
    "kind",
    "schema_version",
    "card_spec",
    "source_inputs",
    "initial_tones",
    "final_tones",
    "frames",
    "duration_mu",
    "prefix_mu",
    "command_count",
    "initialization_command_count",
    "resident_command_count",
    "execution_count",
    "quantization",
)


def _digest(program):
    data = {key: program[key] for key in _PROGRAM_FIELDS}
    return hashlib.sha256(_canonical(data).encode()).hexdigest()


def build_dds_program(card_spec, initial_tones, moves, duration_mu, *, prefix_mu=0):
    """Compile full-state tone knots into one preloaded, closed DDS shot.

    ``moves`` contain region-relative ``start_mu``, ``motion_duration_mu`` and
    ``knots`` (``offset_mu``, full ``tones``). Time inputs must already lie on
    the DDS grid. No duration is silently rounded or stretched.
    """
    spec = _card(card_spec)
    duration_mu = _integer(duration_mu, "duration_mu", 1)
    prefix_mu = _integer(prefix_mu, "prefix_mu")
    if duration_mu % DDS_TICK_MU or prefix_mu % DDS_TICK_MU:
        raise CompileError("DDS duration and prefix must align to the 6.4 ns grid")
    initial = _tones(initial_tones, spec)
    inputs = dict(
        initial_tones=copy.deepcopy(initial_tones),
        moves=copy.deepcopy(moves),
        duration_mu=duration_mu,
        prefix_mu=prefix_mu,
    )
    if not isinstance(moves, (list, tuple)):
        raise CompileError("DDS moves must be a sequence")
    # Each span stores an initial state and a requested state at its endpoint.
    spans, cursor, current, pins = [], 0, initial, set()
    for move in moves:
        if not isinstance(move, Mapping):
            raise CompileError("DDS moves must be mappings")
        tolerance = _finite(
            move.get("frequency_tolerance_hz", 1.0), "frequency tolerance"
        )
        if tolerance <= 0:
            raise CompileError("DDS frequency tolerance must be positive")
        start = _integer(move.get("start_mu"), "move start") + prefix_mu
        length = _integer(move.get("motion_duration_mu"), "move duration", 1)
        if start % DDS_TICK_MU or length % DDS_TICK_MU:
            raise CompileError("DDS moves must align to the 6.4 ns grid")
        if start < cursor or start + length > prefix_mu + duration_mu:
            raise CompileError("DDS moves overlap or exceed the program duration")
        knots = move.get("knots")
        if not isinstance(knots, (list, tuple)) or len(knots) < 2:
            raise CompileError("DDS move needs at least two full-state knots")
        resolved = []
        for knot in knots:
            if not isinstance(knot, Mapping):
                raise CompileError("DDS knots must be mappings")
            offset = _integer(knot.get("offset_mu"), "knot offset")
            if offset % DDS_TICK_MU:
                raise CompileError("DDS knots must align to the 6.4 ns grid")
            state = _tones(knot.get("tones"), spec)
            if [(t["core"], t["channel"]) for t in state] != [
                (t["core"], t["channel"]) for t in initial
            ]:
                raise CompileError("DDS knots must contain all and only active tones")
            resolved.append((offset, state))
        if resolved[0][0] != 0 or resolved[-1][0] != length:
            raise CompileError("DDS knot endpoints must match the complete move")
        if not _same_tones(current, resolved[0][1]):
            raise CompileError("DDS move starts at a different tone state")
        if start > cursor:
            spans.append((cursor, start, current, current, None))
        for (offset, state), (next_offset, next_state) in zip(resolved, resolved[1:]):
            if next_offset <= offset:
                raise CompileError("DDS knot times must increase strictly")
            spans.append(
                (start + offset, start + next_offset, state, next_state, tolerance)
            )
        cursor, current = start + length, resolved[-1][1]
        pins.add(cursor)
    end = prefix_mu + duration_mu
    if cursor < end:
        spans.append((cursor, end, current, current, None))
    if not _same_tones(current, initial):
        raise CompileError("DDS shot must return to its initial trapping tone state")
    frames, endpoint_errors, amplitude_errors = [], [], []
    actual = copy.deepcopy(initial)
    for start, stop, left, right, tolerance in spans:
        delta = stop - start
        pin = start in pins
        if pin:
            actual = copy.deepcopy(left)
        # When a move endpoint shares a frame with the next move's first slope,
        # the explicit pin suppresses that first update. Compensate its duration.
        ramp_seconds = (delta - (DDS_TICK_MU if pin else 0)) * LOGICAL_TICK_SECONDS
        if ramp_seconds <= 0:
            raise CompileError("DDS pinned frame has no time for a ramp update")
        tones = []
        next_actual = []
        for first, last in zip(actual, right):
            requested_slope = (
                (last["frequency_hz"] - first["frequency_hz"]) / ramp_seconds
                if tolerance is not None
                else 0.0
            )
            slope = _quantize(requested_slope, FREQUENCY_SLOPE_STEP)
            amp_delta = (
                last["amplitude"] - first["amplitude"] if tolerance is not None else 0.0
            )
            amp_slope = 0.0
            if amp_delta:
                amp_step = spec.get("dds_amplitude_slope_step_per_s")
                if amp_step is None:
                    raise CompileError(
                        "Varying DDS amplitudes require an explicit "
                        "dds_amplitude_slope_step_per_s hardware profile"
                    )
                amp_slope = _quantize(amp_delta / ramp_seconds, amp_step)
            # Slope-only commits preserve the existing accumulator. Rewriting
            # frequency at every knot would suppress the first ramp increment.
            before_latch = first["frequency_hz"] + slope * ramp_seconds
            if not 0 < before_latch <= 400e6:
                raise CompileError("DDS ramp crosses the permitted frequency range")
            # Account for both requested-coordinate rounding and NCO output
            # resolution; slope precision has its own accumulated residual.
            error = abs(last["frequency_hz"] - before_latch) + FREQUENCY_STEP_HZ
            if tolerance is not None and error > tolerance:
                raise CompileError(
                    "DDS quantized ramp exceeds the move frequency tolerance"
                )
            endpoint_errors.append(error)
            amp_before_latch = first["amplitude"] + amp_slope * ramp_seconds
            if not 0 < amp_before_latch < 1:
                raise CompileError("DDS ramp crosses the permitted amplitude range")
            amplitude_errors.append(abs(last["amplitude"] - amp_before_latch))
            tones.append(
                {
                    **first,
                    "frequency_slope_hz_per_s": slope,
                    "amplitude_slope_per_s": amp_slope,
                }
            )
            next_actual.append(
                {**first, "frequency_hz": before_latch, "amplitude": amp_before_latch}
            )
        for channel in spec["active_channels"]:
            peak = sum(
                tone["amplitude"] + max(0, tone["amplitude_slope_per_s"]) * ramp_seconds
                for tone in tones
                if tone["channel"] == channel
            )
            if peak > 1:
                raise CompileError("DDS amplitude ramps exceed channel full scale")
        frames.append(
            dict(
                at_mu=start,
                timer_mu=delta,
                trigger_source="timer",
                tones=tones,
                set_frequency=pin,
                set_amplitude=pin,
                ramp_update_delay_mu=DDS_TICK_MU if pin else 0,
            )
        )
        actual = next_actual
    terminal = [
        {**tone, "frequency_slope_hz_per_s": 0.0, "amplitude_slope_per_s": 0.0}
        for tone in initial
    ]
    frames.append(
        dict(
            at_mu=end,
            timer_mu=None,
            trigger_source="card",
            tones=terminal,
            set_frequency=True,
            set_amplitude=True,
            ramp_update_delay_mu=0,
        )
    )
    # Slope-only intermediate frames; final frequency/amplitude pins. Source,
    # timer and EXEC are additional commands. Guard is a conservative policy.
    for frame in frames:
        writes = 2 + int(frame["set_frequency"]) + int(frame["set_amplitude"])
        frame["command_count"] = (
            writes * len(frame["tones"]) + 2 + (frame["timer_mu"] is not None)
        )
    for previous, following in zip(frames, frames[1:]):
        cycles = previous["timer_mu"] // DDS_TICK_MU
        required = COMMAND_GUARD_CYCLES + following["command_count"]
        if cycles < required:
            raise CompileError(
                f"DDS frame needs >= {required} command cycles, got {cycles}"
            )
        if cycles > MAX_TIMER_CYCLES:
            raise CompileError("DDS timer exceeds its hardware range")
    count = sum(frame["command_count"] for frame in frames)
    init_count = 4 * len(initial) + 2 + INITIAL_CONFIGURATION_COMMAND_RESERVE
    if count + init_count > QUEUE_CAPACITY:
        raise CompileError(
            "DDS initial hold and shot exceed the 4096-command SINGLE queue"
        )
    program = dict(
        kind="dds",
        schema_version=1,
        card_spec=spec,
        source_inputs=inputs,
        initial_tones=initial,
        final_tones=copy.deepcopy(initial),
        frames=frames,
        duration_mu=duration_mu,
        prefix_mu=prefix_mu,
        command_count=count,
        initialization_command_count=init_count,
        resident_command_count=count + init_count,
        execution_count=len(frames),
        quantization=dict(
            tick_mu=DDS_TICK_MU,
            frequency_step_hz=FREQUENCY_STEP_HZ,
            frequency_slope_step_hz_per_s=FREQUENCY_SLOPE_STEP,
            amplitude_step=AMPLITUDE_STEP,
            ramp_stepsize=1,
            command_guard_cycles=COMMAND_GUARD_CYCLES,
            amplitude_slope_step_per_s=spec.get("dds_amplitude_slope_step_per_s"),
            amplitude_interpolation="native_linear",
            max_endpoint_correction_hz=max(endpoint_errors, default=0.0),
            max_endpoint_correction_amplitude=max(amplitude_errors, default=0.0),
        ),
    )
    program["sha256"] = _digest(program)
    return program


def validate_dds_program(program):
    """Recompile source intent, verifying frames, limits and the immutable digest."""
    try:
        if program.get("kind") != "dds" or program.get("schema_version") != 1:
            raise CompileError("Unsupported DDS program schema")
        source = program["source_inputs"]
        rebuilt = build_dds_program(program["card_spec"], **source)
        if any(program[key] != rebuilt[key] for key in _PROGRAM_FIELDS):
            raise CompileError("DDS program differs from its source intent")
        if program["sha256"] != rebuilt["sha256"]:
            raise CompileError("DDS program digest mismatch")
    except (KeyError, TypeError, AttributeError, OverflowError, ValueError) as exc:
        if isinstance(exc, CompileError):
            raise
        raise CompileError(f"Invalid DDS program: {exc}") from exc
    return True
