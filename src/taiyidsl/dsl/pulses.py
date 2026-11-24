"""Immutable pulse and pulse-local phase declarations, without scheduling."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import numpy as np

from ..sample_assets import sample_digest, validate_sample_values
from ..units import Quantity, as_fraction, ns
from .values import SweepParameter, validate_value

_UNSET = object()


class Pulse:
    """Base type for reusable pulse declarations accepted by the DSL."""

    @staticmethod
    def from_array(
        samples,
        *,
        sample_interval,
        kind,
        resample="strict",
        pad_before=None,
        pad_after=None,
        pad_both=None,
        amplitude=None,
        frequency=_UNSET,
        phase=_UNSET,
        phase_modulation=None,
        phase_modulation_mode="local",
    ) -> SampledPulse:
        """Snapshot a normalized real envelope, complex IQ, or final real waveform.

        The last sample occupies one full sample interval. Strict playback uses
        the declared grid. Linear interpolation must be selected explicitly.
        Envelope and IQ samples use the drive's carrier and phase controls.
        pad_before and pad_after reserve zero-output time around the samples.
        Supply pad_both alone to use the same duration on each side.
        """
        if pad_both is not None:
            if pad_before is not None or pad_after is not None:
                raise ValueError("Use pad_both alone or use pad_before and pad_after")
            pad_before = pad_after = pad_both
        if kind not in ("envelope", "iq", "waveform"):
            raise ValueError("Sample kind must be envelope, iq or waveform")
        if kind == "waveform" and (frequency is not _UNSET or phase is not _UNSET):
            raise ValueError("A final waveform cannot accept frequency or phase")
        if isinstance(samples, (list, tuple)) and any(
            isinstance(value, (bool, np.bool_)) for value in samples
        ):
            raise TypeError("Boolean samples are unsupported")
        if np.ma.is_masked(samples):
            raise ValueError("Masked samples are unsupported")
        array = np.asarray(samples)
        if array.ndim != 1 or array.size == 0:
            raise ValueError("Expected a nonempty one-dimensional sample array")
        if array.dtype.kind not in ("iufc" if kind == "iq" else "iuf"):
            raise TypeError(f"Unsupported sample dtype {array.dtype} for {kind}")
        dtype = "<c16" if kind == "iq" else "<f8"
        with np.errstate(over="ignore", invalid="ignore"):
            array = np.asarray(array, dtype=dtype)
            validate_sample_values(array)
        raw = array.tobytes()
        asset = _SampleBuffer(dtype, raw, sample_digest(dtype, raw))
        return SampledPulse(
            _buffer=asset,
            sample_interval=sample_interval,
            kind=kind,
            resample=resample,
            pad_before=0 if pad_before is None else pad_before,
            pad_after=0 if pad_after is None else pad_after,
            amplitude=amplitude,
            frequency=None if frequency is _UNSET else frequency,
            phase=0 if phase is _UNSET else phase,
            phase_modulation=phase_modulation,
            phase_modulation_mode=phase_modulation_mode,
        )


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
class Square(Pulse):
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
class FlatTopGaussian(Pulse):
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


GaussianSquare = FlatTopGaussian


def Gaussian(
    sigma,
    *,
    cutoff_sigma=4,
    amplitude=None,
    phase=0,
    frequency=None,
    sample_interval=20 * ns,
    phase_modulation=None,
    phase_modulation_mode="local",
) -> FlatTopGaussian:
    """Return the normalized zero-plateau Gaussian of width 2 * cutoff_sigma * sigma."""
    return FlatTopGaussian(
        0,
        sigma,
        cutoff_sigma=cutoff_sigma,
        amplitude=amplitude,
        phase=phase,
        frequency=frequency,
        sample_interval=sample_interval,
        phase_modulation=phase_modulation,
        phase_modulation_mode=phase_modulation_mode,
    )


@dataclass(frozen=True)
class _SampleBuffer:
    dtype: str
    raw: bytes = field(repr=False)
    digest: str


@dataclass(frozen=True, kw_only=True)
class SampledPulse(Pulse):
    """Immutable sample data with independently replaceable playback controls.

    Construct with Pulse.from_array(). Each access to samples returns a
    read-only view backed by immutable little-endian bytes.
    """

    _buffer: _SampleBuffer = field(repr=False)
    sample_interval: Quantity
    kind: str
    resample: str = "strict"
    pad_before: Quantity = 0 * ns
    pad_after: Quantity = 0 * ns
    amplitude: object = None
    phase: object = 0
    frequency: object = None
    phase_modulation: object = None
    phase_modulation_mode: str = "local"

    def __post_init__(self):
        if self.kind not in ("envelope", "iq", "waveform"):
            raise ValueError("Sample kind must be envelope, iq or waveform")
        if self._buffer.dtype != ("<c16" if self.kind == "iq" else "<f8"):
            raise ValueError("Sample buffer type does not match the requested kind")
        if self.resample not in ("strict", "linear"):
            raise ValueError("resample must be strict or linear")
        interval = as_fraction(self.sample_interval, "time")
        if interval <= 0:
            raise ValueError("sample_interval must be a positive static time")
        object.__setattr__(self, "sample_interval", Quantity(interval, "time"))
        for name in ("pad_before", "pad_after"):
            value = getattr(self, name)
            if isinstance(value, SweepParameter):
                raise TypeError(f"{name} must be a static time")
            seconds = as_fraction(value, "time")
            if seconds < 0:
                raise ValueError(f"{name} must be nonnegative")
            object.__setattr__(self, name, Quantity(seconds, "time"))
        if self.kind == "waveform":
            if (
                self.frequency not in (None, 0)
                or self.phase != 0
                or self.phase_modulation is not None
                or self.phase_modulation_mode != "local"
            ):
                raise ValueError(
                    "A final waveform cannot use carrier or phase controls"
                )
            object.__setattr__(self, "frequency", 0)
            if self.amplitude is None:
                object.__setattr__(self, "amplitude", 1)
        if self.amplitude is not None:
            validate_value(self.amplitude, "dimensionless", "amplitude", maximum=1)
        if self.frequency is not None:
            validate_value(self.frequency, "frequency", "frequency")
        validate_value(self.phase, "phase", "phase", minimum=None)
        _phase_modulation(self.phase_modulation)
        _phase_modulation_mode(self.phase_modulation_mode)

    @property
    def sample_count(self):
        return len(self._buffer.raw) // np.dtype(self._buffer.dtype).itemsize

    @property
    def duration(self):
        return (
            self.pad_before + self.sample_interval * self.sample_count + self.pad_after
        )

    @property
    def samples(self):
        return np.frombuffer(self._buffer.raw, dtype=self._buffer.dtype)

    @property
    def sample_digest(self):
        return self._buffer.digest

    def sample_asset(self):
        return {
            "dtype": self._buffer.dtype,
            "count": self.sample_count,
            "data": base64.b64encode(self._buffer.raw).decode("ascii"),
        }


class PulseLibrary:
    """A laboratory's named factories. Factories run when make() is called."""

    def __init__(self):
        self._factories = {}

    def register(self, name, factory):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A pulse factory requires a nonempty name")
        if name in self._factories:
            raise ValueError(f"Pulse factory already registered: {name}")
        if not callable(factory):
            raise TypeError("A pulse factory must be callable")
        self._factories[name] = factory

    def make(self, name, *args, **kwargs) -> Pulse:
        factory = self._factories[name]
        result = factory(*args, **kwargs)
        if not isinstance(result, Pulse):
            raise TypeError(f"Pulse factory {name!r} must return a Pulse")
        return result
