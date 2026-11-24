"""Resolve Spectrum carrier and modulation state after final event scheduling.

Carrier cycles are integrated exactly from decimal frequencies and integer ticks.
The modulation accumulator is an output phase in radians, already transformed by
the binding's phase scale. The logical drive frame F is resolved separately.
"""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np

from taiyidsl.config.spectrum import HYBRID_TICK_SECONDS
from taiyidsl.ir import CompileError


def modulation_characteristics(modulation, duration_s, frequency_hz, phase_scale):
    """Describe analytic endpoints and smooth frequency extrema, not bandwidth.

    Endpoint phase jumps and finite envelope bandwidth are separate from the
    derivative on the open pulse interval. Polynomial extrema use real roots of
    the second derivative; these diagnostics are not an analog acceptance test.
    """
    start = end = 0.0
    derivatives = [0.0]
    if modulation is not None:
        if modulation["kind"] == "half_sine":
            amplitude = float(modulation["amplitude"])
            derivatives = [-abs(amplitude * math.pi), abs(amplitude * math.pi)]
        elif modulation["kind"] == "polynomial":
            coefficients = np.asarray(modulation["coefficients"], dtype=float)
            start = float(coefficients[0])
            try:
                end = math.fsum(float(value) for value in coefficients)
            except OverflowError as exc:
                raise CompileError(
                    "Spectrum modulation endpoint is not finite"
                ) from exc
            derivative = np.polynomial.polynomial.polyder(coefficients)
            second = np.polynomial.polynomial.polyder(derivative)
            locations = [0.0, 1.0]
            try:
                if np.any(second):
                    for root in np.polynomial.polynomial.polyroots(second):
                        if abs(root.imag) < 1e-9 and 0 < root.real < 1:
                            locations.append(float(root.real))
            except (np.linalg.LinAlgError, ValueError) as exc:
                raise CompileError(
                    "Cannot resolve finite Spectrum modulation derivatives"
                ) from exc
            derivatives = np.polynomial.polynomial.polyval(locations, derivative)
        else:
            raise CompileError("Unsupported Spectrum phase modulation")
    frequencies = [
        frequency_hz + phase_scale * float(value) / (2 * math.pi * duration_s)
        for value in derivatives
    ]
    if not all(
        math.isfinite(value)
        for value in [
            start,
            end,
            end - start,
            phase_scale * start,
            phase_scale * end,
            *frequencies,
        ]
    ):
        raise CompileError("Spectrum modulation phase/frequency must remain finite")
    return {
        "modulation_start_rad": start,
        "modulation_end_rad": end,
        "modulation_net_change_rad": end - start,
        "instantaneous_frequency_min_hz": min(frequencies),
        "instantaneous_frequency_max_hz": max(frequencies),
        "frequency_check_scope": "smooth_phase_derivative_not_analog_bandwidth",
    }


class OutputPhaseState:
    """One shot's independent phase state for one physical Spectrum output."""

    def __init__(
        self,
        output,
        tick_seconds=HYBRID_TICK_SECONDS,
        *,
        reference_ticks=0,
        reference_kind="shot",
    ):
        self.output = output
        self.tick = Fraction(str(tick_seconds))
        self.reference_ticks = reference_ticks
        self.reference_kind = reference_kind
        self.last_end_ticks = reference_ticks
        self.cycles = Fraction(0)
        self.frequency_hz = output.initial_frequency_hz
        self.modulation_phase_rad = 0.0

    def reset(self, at_ticks):
        """Clear carrier phase and carried modulation without changing frequency."""
        if at_ticks < self.last_end_ticks:
            raise CompileError(
                "reset_phase cannot occur inside an active Spectrum pulse"
            )
        self.reference_ticks = self.last_end_ticks = at_ticks
        self.reference_kind = "explicit_reset_phase"
        self.cycles = Fraction(0)
        self.modulation_phase_rad = 0.0

    def pulse(
        self,
        *,
        start_ticks,
        duration_ticks,
        frequency_hz,
        phase_scale,
        modulation,
        modulation_mode="local",
        modulation_duration_ticks=None,
    ):
        """Return start phase and provenance; update once at the logical end.

        Call only for a nonzero pulse, before waveform tiling or calibration
        shifts. A carry transfers m(1), including a nonzero entrance jump m(0),
        while pulse.phase remains local. Idle holds the last physical frequency.
        A padded pulse advances its carrier for its entire duration and its
        modulation only during the unpadded source interval.
        """
        if duration_ticks <= 0 or start_ticks < self.last_end_ticks:
            raise CompileError(
                "Spectrum phase state needs ordered nonoverlapping pulses"
            )
        if modulation_mode not in {"local", "carry"}:
            raise CompileError("Spectrum phase_modulation_mode must be local or carry")
        if self.output.carrier_mode == "continuous":
            idle = start_ticks - self.last_end_ticks
            self.cycles = (
                self.cycles + Fraction(str(self.frequency_hz)) * idle * self.tick
            ) % 1
            start_cycles = self.cycles
        else:
            start_cycles = (
                Fraction(str(frequency_hz))
                * (start_ticks - self.reference_ticks)
                * self.tick
            ) % 1
        end_cycles = (
            start_cycles + Fraction(str(frequency_hz)) * duration_ticks * self.tick
        ) % 1
        active_ticks = (
            duration_ticks
            if modulation_duration_ticks is None
            else modulation_duration_ticks
        )
        details = modulation_characteristics(
            modulation, float(active_ticks * self.tick), frequency_hz, phase_scale
        )
        inherited = self.modulation_phase_rad
        terminal = inherited
        if modulation_mode == "carry":
            try:
                terminal = math.fsum(
                    (inherited, phase_scale * details["modulation_end_rad"])
                )
            except OverflowError as exc:
                raise CompileError(
                    "Accumulated Spectrum modulation is not finite"
                ) from exc
        if not math.isfinite(terminal):
            raise CompileError("Accumulated Spectrum modulation must remain finite")
        details.update(
            carrier_mode=self.output.carrier_mode,
            carrier_reference_ticks=self.reference_ticks,
            carrier_reference_time_s=float(self.reference_ticks * self.tick),
            carrier_reference_kind=self.reference_kind,
            initial_frequency_hz=self.output.initial_frequency_hz,
            carrier_frequency_hz=frequency_hz,
            carrier_phase_rad=2 * math.pi * float(start_cycles),
            carrier_end_phase_rad=2 * math.pi * float(end_cycles),
            inherited_modulation_phase_rad=inherited,
            terminal_modulation_phase_rad=terminal,
            phase_modulation_mode=modulation_mode,
            entry_modulation_jump_rad=phase_scale * details["modulation_start_rad"],
            exit_modulation_jump_rad=(
                -phase_scale * details["modulation_end_rad"]
                if modulation_mode == "local"
                else 0.0
            ),
        )
        self.last_end_ticks = start_ticks + duration_ticks
        self.cycles = end_cycles
        self.frequency_hz = frequency_hz
        self.modulation_phase_rad = terminal
        return details
