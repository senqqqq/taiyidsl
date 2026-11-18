"""Immutable pulse and pulse-local phase declarations, without scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..units import as_fraction, ns
from .values import SweepParameter, validate_value


@dataclass(frozen=True)
class HalfSinePhase:
    """Pulse-local phase ``amplitude * sin(pi*t/total_duration)`` in radians."""

    amplitude: object

    def __post_init__(self):
        validate_value(self.amplitude, "phase", "amplitude", minimum=None)


@dataclass(frozen=True)
class PolynomialPhase:
    """Pulse-local phase with ascending coefficients in ``u=t/total_duration``."""

    coefficients: tuple

    def __post_init__(self):
        coefficients = tuple(self.coefficients)
        if not coefficients:
            raise ValueError("PolynomialPhase requires nonempty coefficients")
        for value in coefficients:
            validate_value(value, "phase", "coefficient", minimum=None)
        object.__setattr__(self, "coefficients", coefficients)


def _phase_modulation(value):
    if value is not None and not isinstance(value, (HalfSinePhase, PolynomialPhase)):
        raise TypeError(
            "phase_modulation must be HalfSinePhase, PolynomialPhase or None"
        )


def _phase_modulation_mode(value):
    if value not in {"local", "carry"}:
        raise ValueError("phase_modulation_mode must be local or carry")


@dataclass(frozen=True)
class Square:
    duration: object
    amplitude: object = None
    phase: object = 0
    frequency: object = None
    phase_modulation: object = field(default=None, kw_only=True)
    phase_modulation_mode: str = field(default="local", kw_only=True)

    def __post_init__(self):
        validate_value(self.duration, "time", "duration")
        if self.amplitude is not None:
            validate_value(self.amplitude, "dimensionless", "amplitude", maximum=1)
        validate_value(self.phase, "phase", "phase", minimum=None)
        if self.frequency is not None:
            validate_value(self.frequency, "frequency", "frequency")
        _phase_modulation(self.phase_modulation)
        _phase_modulation_mode(self.phase_modulation_mode)


@dataclass(frozen=True)
class FlatTopGaussian:
    """Flat plateau with shifted, normalized Gaussian edges ending at zero.

    Each edge lasts ``cutoff_sigma * sigma``. ``flat_duration`` may be a sweep;
    sigma, cutoff and sample_interval are static. The interval is a requested
    hardware sampling interval, never silently coarsened to fit a RAM buffer.
    """

    flat_duration: object
    sigma: object
    cutoff_sigma: object = 4
    amplitude: object = None
    phase: object = 0
    frequency: object = None
    sample_interval: object = 20 * ns
    phase_modulation: object = field(default=None, kw_only=True)
    phase_modulation_mode: str = field(default="local", kw_only=True)

    def __post_init__(self):
        validate_value(self.flat_duration, "time", "flat_duration")
        for name, dimension in (
            ("sigma", "time"),
            ("cutoff_sigma", "dimensionless"),
            ("sample_interval", "time"),
        ):
            value = getattr(self, name)
            if isinstance(value, SweepParameter):
                raise TypeError(f"{name} must be static")
            if as_fraction(value, dimension) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.amplitude is not None:
            validate_value(self.amplitude, "dimensionless", "amplitude", maximum=1)
        validate_value(self.phase, "phase", "phase", minimum=None)
        if self.frequency is not None:
            validate_value(self.frequency, "frequency", "frequency")
        _phase_modulation(self.phase_modulation)
        _phase_modulation_mode(self.phase_modulation_mode)
