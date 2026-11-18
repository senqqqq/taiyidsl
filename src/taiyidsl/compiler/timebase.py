"""Exact conversion between the native ARTIQ and hybrid scheduling clocks."""

from fractions import Fraction

from ..config.spectrum import HYBRID_TICK_SECONDS
from ..ir import CompileError


def _exact_integer(value, label):
    value = Fraction(value)
    if value.denominator != 1:
        raise CompileError(f"{label} is not on the required timing grid")
    return value.numerator


def native_factor(setup):
    return _exact_integer(
        Fraction(str(setup.spectrum.artiq_tick_seconds))
        / Fraction(str(HYBRID_TICK_SECONDS)),
        "ARTIQ tick",
    )
