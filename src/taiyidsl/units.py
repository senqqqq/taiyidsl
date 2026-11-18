"""Small dimension-aware quantities with exact decimal time arithmetic.

Bare pulse numbers are SI; SweepParameter.unit specifies units of bare values.
"""

import math
import numbers
from dataclasses import dataclass
from fractions import Fraction


def fraction(value):
    if isinstance(value, bool) or not isinstance(value, (numbers.Real, Fraction)):
        raise TypeError(f"Expected a finite real number, got {value!r}")
    if not math.isfinite(float(value)):
        raise ValueError("Values must be finite")
    return value if isinstance(value, Fraction) else Fraction(str(value))


@dataclass(frozen=True)
class Quantity:
    value: Fraction
    dimension: str

    def __float__(self):
        return float(self.value)

    def __mul__(self, other):
        return Quantity(self.value * fraction(other), self.dimension)

    __rmul__ = __mul__

    def __truediv__(self, other):
        return Quantity(self.value / fraction(other), self.dimension)

    def __add__(self, other):
        return Quantity(self.value + as_fraction(other, self.dimension), self.dimension)

    __radd__ = __add__

    def __sub__(self, other):
        return Quantity(self.value - as_fraction(other, self.dimension), self.dimension)

    def __neg__(self):
        return Quantity(-self.value, self.dimension)


def as_fraction(value, dimension):
    if isinstance(value, Quantity):
        if value.dimension != dimension:
            raise ValueError(f"Expected {dimension}, got {value.dimension}")
        return value.value
    return fraction(value)


s = Quantity(Fraction(1), "time")
ms = s / 1000
us = s / 1_000_000
ns = s / 1_000_000_000
Hz = Quantity(Fraction(1), "frequency")
kHz = Hz * 1000
MHz = Hz * 1_000_000
rad = Quantity(Fraction(1), "phase")
deg = rad * (math.pi / 180)
turn = rad * (2 * math.pi)
m = Quantity(Fraction(1), "length")
mm = m / 1000
um = m / 1_000_000
UNIT_TABLE = {
    "s": s,
    "ms": ms,
    "us": us,
    "ns": ns,
    "Hz": Hz,
    "kHz": kHz,
    "MHz": MHz,
    "rad": rad,
    "deg": deg,
    "turn": turn,
    "m": m,
    "mm": mm,
    "um": um,
    "dimensionless": Quantity(Fraction(1), "dimensionless"),
    "": Quantity(Fraction(1), "dimensionless"),
}
