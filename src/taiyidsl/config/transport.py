"""Static atom transport geometry and calibration declarations.

Positions here are planned positions, never observations of trapped atoms.
Building a move records intent; it does not edit a user's Qubit or touch hardware.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass

from ..ir import CompileError
from ..units import as_fraction
from .models import Position


def _number(value, dimension, name, *, positive=False):
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    value = float(as_fraction(value, dimension))
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"Invalid {name}")
    return value


def _name(value, name):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be nonempty text")
    return value


@dataclass(frozen=True)
class IZ:
    """Two calibrated interaction sites in one named coordinate system."""

    uid: str
    sites: tuple[Position, ...]

    def __post_init__(self):
        _name(self.uid, "IZ uid")
        sites = tuple(
            Position(**site) if isinstance(site, Mapping) else site
            for site in self.sites
        )
        if len(sites) != 2 or any(not isinstance(site, Position) for site in sites):
            raise ValueError("IZ requires exactly two Position sites")
        if sites[0] == sites[1]:
            raise ValueError("IZ sites must be distinct")
        if len({site.coordinate_system for site in sites}) != 1:
            raise ValueError("IZ sites must share one coordinate system")
        object.__setattr__(self, "sites", sites)

    def __getitem__(self, index):
        return SiteRef(self, index)

    def to_dict(self):
        return {"uid": self.uid, "sites": [site.to_dict() for site in self.sites]}


@dataclass(frozen=True)
class SiteRef:
    zone: IZ
    index: int

    def __post_init__(self):
        if not isinstance(self.zone, IZ):
            raise TypeError("SiteRef requires an IZ")
        if type(self.index) is not int or not 0 <= self.index < len(self.zone.sites):
            raise ValueError("Invalid IZ site index")

    @property
    def position(self):
        return self.zone.sites[self.index]

    def to_dict(self):
        return {"zone": self.zone.to_dict(), "index": self.index}


@dataclass(frozen=True)
class MoveProfile:
    """A requested normalized curve, sampled for linear DDS frequency ramps.

    ``segments`` controls the approximation of a minimum-jerk curve. Position
    tolerance bounds hardware realization and endpoint/site matching; it is not
    a bound on the interpolation error relative to the continuous ideal curve.
    """

    samples: tuple[tuple[float, float], ...]
    kind: str = "samples"

    def __post_init__(self):
        samples = tuple(tuple(float(v) for v in pair) for pair in self.samples)
        if len(samples) < 2 or any(len(pair) != 2 for pair in samples):
            raise ValueError("MoveProfile needs time/progress pairs")
        if samples[0] != (0.0, 0.0) or samples[-1] != (1.0, 1.0):
            raise ValueError("MoveProfile endpoints must be (0, 0) and (1, 1)")
        if any(not math.isfinite(v) or not 0 <= v <= 1 for p in samples for v in p):
            raise ValueError("MoveProfile samples must be finite and normalized")
        if any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(samples, samples[1:])):
            raise ValueError("MoveProfile time must increase and progress be monotone")
        if self.kind not in {"linear", "minimum_jerk", "samples"}:
            raise ValueError("Unknown MoveProfile kind")
        object.__setattr__(self, "samples", samples)

    @classmethod
    def linear(cls):
        return cls(((0.0, 0.0), (1.0, 1.0)), "linear")

    @classmethod
    def minimum_jerk(cls, segments=32):
        if type(segments) is not int or segments < 2:
            raise ValueError("minimum_jerk needs at least two segments")
        return cls(
            tuple(
                (u, 10 * u**3 - 15 * u**4 + 6 * u**5)
                for u in (i / segments for i in range(segments + 1))
            ),
            "minimum_jerk",
        )

    @classmethod
    def from_samples(cls, samples):
        return cls(tuple(samples))

    def progress(self, time):
        if self.kind == "minimum_jerk":
            return 10 * time**3 - 15 * time**4 + 6 * time**5
        return _interpolate(self.samples, time, 1)

    def time_at(self, progress):
        if self.kind == "minimum_jerk":
            lo, hi = 0.0, 1.0
            for _ in range(55):
                mid = (lo + hi) / 2
                if self.progress(mid) < progress:
                    lo = mid
                else:
                    hi = mid
            return (lo + hi) / 2
        for a, b in zip(self.samples, self.samples[1:]):
            if a[1] <= progress <= b[1]:
                return (
                    a[0]
                    if a[1] == b[1]
                    else (a[0] + (b[0] - a[0]) * (progress - a[1]) / (b[1] - a[1]))
                )
        raise ValueError("Progress is outside the profile")

    def to_dict(self):
        return {"kind": self.kind, "samples": [list(pair) for pair in self.samples]}


def _interpolate(table, value, column):
    if value < table[0][0] or value > table[-1][0]:
        raise CompileError(f"Coordinate {value:g} is outside calibrated AOD range")
    for a, b in zip(table, table[1:]):
        if a[0] <= value <= b[0]:
            ratio = (value - a[0]) / (b[0] - a[0])
            return a[column] + ratio * (b[column] - a[column])
    return table[-1][column]


def _axis_table(values, name):
    rows = []
    for row in values:
        if len(row) not in {2, 3}:
            raise ValueError(
                f"{name} rows need position, frequency, optional amplitude"
            )
        rows.append(
            (
                _number(row[0], "length", name),
                _number(row[1], "frequency", name, positive=True),
                _number(
                    row[2] if len(row) == 3 else 1, "dimensionless", name, positive=True
                ),
            )
        )
    if len(rows) < 2 or any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        raise ValueError(f"{name} positions must strictly increase")
    increments = [b[1] - a[1] for a, b in zip(rows, rows[1:])]
    if not (all(v > 0 for v in increments) or all(v < 0 for v in increments)):
        raise ValueError(f"{name} frequencies must be strictly monotone")
    return tuple(rows)


@dataclass(frozen=True)
class TransportConfig:
    """One crossed X/Y AOD controller with explicit shared DDS-core bindings.

    Axis calibration rows are (position_m, frequency_hz[, amplitude_scale]).
    Base amplitudes are per core; compensation scales multiply them.
    """

    card: str
    x_calibration: tuple
    y_calibration: tuple
    bindings: dict
    initial_amplitudes: dict
    zones: tuple[IZ, ...] = ()
    coordinate_system: str = "tweezer_plane"
    x_channel: int = 0
    y_channel: int = 1
    position_tolerance: float = 1e-9
    frequency_tolerance_hz: float = 1.0
    min_separation: float = 1e-6

    def __post_init__(self):
        _name(self.card, "Transport card")
        _name(self.coordinate_system, "Transport coordinate_system")
        if (self.x_channel, self.y_channel) not in {(0, 1), (1, 0)}:
            raise ValueError("Crossed AOD axes require distinct channels 0 and 1")
        for name in ("x_calibration", "y_calibration"):
            object.__setattr__(self, name, _axis_table(getattr(self, name), name))
        bindings = copy.deepcopy(dict(self.bindings))
        if not bindings:
            raise ValueError("Transport requires explicit qubit/core bindings")
        axis_cores = {"x": set(), "y": set()}
        for uid, values in bindings.items():
            _name(uid, "Transport qubit")
            if set(values) != {"x_core", "y_core"}:
                raise ValueError("Each transport binding needs x_core and y_core")
            for axis in axis_cores:
                core = values[f"{axis}_core"]
                if type(core) is not int or core < 0:
                    raise ValueError("DDS core indices must be nonnegative integers")
                axis_cores[axis].add(core)
        if axis_cores["x"] & axis_cores["y"]:
            raise ValueError("An AOD DDS core cannot serve both axes")
        amplitudes = {}
        for key, value in self.initial_amplitudes.items():
            if isinstance(key, str) and key.isdecimal():
                key = int(key)
            if type(key) is not int or key in amplitudes:
                raise ValueError("Amplitude keys must be unique DDS core indices")
            value = _number(value, "dimensionless", "amplitude", positive=True)
            if value > 1:
                raise ValueError("Transport amplitudes must be in (0, 1]")
            amplitudes[key] = value
        if set(amplitudes) != axis_cores["x"] | axis_cores["y"]:
            raise ValueError("Provide one amplitude for every bound DDS core")
        zones = tuple(IZ(**z) if isinstance(z, Mapping) else z for z in self.zones)
        if any(not isinstance(z, IZ) for z in zones):
            raise TypeError("Transport zones must be IZ objects")
        if len({z.uid for z in zones}) != len(zones):
            raise ValueError("Transport zone IDs must be unique")
        if any(
            p.coordinate_system != self.coordinate_system
            for z in zones
            for p in z.sites
        ):
            raise ValueError("Transport zones use a different coordinate system")
        for name, dimension in (
            ("position_tolerance", "length"),
            ("frequency_tolerance_hz", "frequency"),
            ("min_separation", "length"),
        ):
            object.__setattr__(
                self, name, _number(getattr(self, name), dimension, name, positive=True)
            )
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "initial_amplitudes", amplitudes)
        object.__setattr__(self, "zones", zones)

    def snapshot(self):
        return TransportConfig.from_dict(self.to_dict())

    def to_dict(self):
        return {
            "card": self.card,
            "coordinate_system": self.coordinate_system,
            "x_channel": self.x_channel,
            "y_channel": self.y_channel,
            "x_calibration": [list(row) for row in self.x_calibration],
            "y_calibration": [list(row) for row in self.y_calibration],
            "bindings": copy.deepcopy(self.bindings),
            "initial_amplitudes": {
                str(k): v for k, v in self.initial_amplitudes.items()
            },
            "zones": [zone.to_dict() for zone in self.zones],
            "position_tolerance": self.position_tolerance,
            "frequency_tolerance_hz": self.frequency_tolerance_hz,
            "min_separation": self.min_separation,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(**copy.deepcopy(dict(data)))


def normalize_transport(value):
    if value is None:
        return None
    if isinstance(value, TransportConfig):
        return value.snapshot()
    if isinstance(value, Mapping):
        return TransportConfig.from_dict(value)
    raise TypeError("transport must be a TransportConfig or mapping")
