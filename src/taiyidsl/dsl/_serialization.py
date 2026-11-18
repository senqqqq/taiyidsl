"""DSL IR/provenance encoding; distinct from generic artifact serialization."""

from dataclasses import fields
from fractions import Fraction
from types import MappingProxyType

from ..units import Quantity
from .nodes import Node
from .pulses import FlatTopGaussian, HalfSinePhase, PolynomialPhase, Square
from .values import SweepParameter


def json_value(value):
    from ..config.models import Position
    from ..config.transport import IZ, MoveProfile, SiteRef

    if isinstance(value, (Position, IZ, SiteRef, MoveProfile)):
        return value.to_dict()
    """Portable representation for IR/provenance; never pickle live Python objects."""
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, Quantity):
        return {"value": float(value), "dimension": value.dimension}
    if isinstance(value, SweepParameter):
        return {
            "parameter": value.name,
            "values": [float(v) for v in value.values],
            "unit": value.unit,
        }
    if isinstance(value, HalfSinePhase):
        return {"kind": "half_sine", "amplitude": json_value(value.amplitude)}
    if isinstance(value, PolynomialPhase):
        return {"kind": "polynomial", "coefficients": json_value(value.coefficients)}
    if isinstance(value, FlatTopGaussian):
        return {
            "shape": "flat_top_gaussian",
            **{
                f.name: json_value(getattr(value, f.name))
                for f in fields(value)
                if (f.name != "phase_modulation" or value.phase_modulation is not None)
                and (
                    f.name != "phase_modulation_mode"
                    or value.phase_modulation_mode != "local"
                )
            },
        }
    if isinstance(value, Square):
        return {
            k: json_value(getattr(value, k))
            for k in (
                "duration",
                "amplitude",
                "phase",
                "frequency",
                "phase_modulation",
                "phase_modulation_mode",
            )
            if (k != "phase_modulation" or value.phase_modulation is not None)
            and (k != "phase_modulation_mode" or value.phase_modulation_mode != "local")
        }
    if isinstance(value, Node):
        return {
            "kind": value.kind,
            "params": json_value(value.params),
            "children": [json_value(n) for n in value.children],
            "source": value.source,
        }
    if isinstance(value, dict) or isinstance(value, MappingProxyType):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value
