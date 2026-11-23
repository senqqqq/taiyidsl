"""Static sweep values and dimension/count checks shared by DSL consumers."""

from __future__ import annotations

from dataclasses import dataclass

from ..units import UNIT_TABLE, Quantity, as_fraction, fraction


@dataclass(frozen=True)
class SweepParameter:
    name: str
    values: tuple
    unit: str = "s"

    def __post_init__(self):
        if not self.name or not self.name.isidentifier():
            raise ValueError("Sweep name must be a Python identifier")
        if self.unit not in UNIT_TABLE:
            raise ValueError(f"Unsupported unit: {self.unit}")
        unit = UNIT_TABLE[self.unit]
        values = tuple(
            as_fraction(v, unit.dimension)
            if isinstance(v, Quantity)
            else as_fraction(v, unit.dimension) * unit.value
            for v in self.values
        )
        if not values:
            raise ValueError("Sweep values cannot be empty")
        object.__setattr__(self, "values", values)

    @property
    def dimension(self):
        return UNIT_TABLE[self.unit].dimension

    def __bool__(self):
        raise TypeError(
            "SweepParameter is bound per point; use exp.case(), not Python if"
        )

    def __index__(self):
        raise TypeError(
            "SweepParameter is bound per point; use exp.repeat(), not range()"
        )


def sweep_parameter_group(parameter) -> tuple[SweepParameter, ...]:
    """Collect parameters that share one host sweep index."""
    if isinstance(parameter, SweepParameter):
        return (parameter,)
    if not isinstance(parameter, (list, tuple)) or not all(
        isinstance(item, SweepParameter) for item in parameter
    ):
        raise TypeError("sweep requires a SweepParameter or a list/tuple of them")
    if not parameter:
        raise ValueError("A sweep parameter group cannot be empty")
    if len({item.name for item in parameter}) != len(parameter):
        raise ValueError("Sweep parameter names must be unique within a group")
    if len({len(item.values) for item in parameter}) != 1:
        raise ValueError("Paired sweep parameters must have equal lengths")
    return tuple(parameter)


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def validate_value(value, dimension, name, *, minimum=0, maximum=None):
    """Shared build-time checks for scalar and static-sweep operation inputs."""
    if isinstance(value, SweepParameter):
        if value.dimension != dimension:
            raise ValueError(f"Expected {dimension} for {name}, got {value.dimension}")
        values = value.values
    else:
        values = (as_fraction(value, dimension),)
    for number in values:
        number = fraction(number)
        if minimum is not None and number < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        if maximum is not None and number > maximum:
            raise ValueError(f"{name} must be <= {maximum}")
    return value


def validate_count(value, name="count", *, minimum=0):
    """Validate a static integer or every value of a dimensionless count sweep."""
    if isinstance(value, SweepParameter):
        if value.dimension != "dimensionless":
            raise ValueError(f"{name} requires a dimensionless sweep")
        values = (fraction(v) for v in value.values)
        if any(v.denominator != 1 or v < minimum for v in values):
            raise ValueError(f"{name} must contain integers >= {minimum}")
    elif type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def resolve(value, coordinates, dimension):
    if isinstance(value, SweepParameter):
        if value.dimension != dimension:
            raise ValueError(
                f"Parameter {value.name} has dimension {value.dimension}, "
                f"expected {dimension}"
            )
        if value.name not in coordinates:
            raise ValueError(f"Unbound sweep parameter {value.name}")
        return coordinates[value.name]
    return as_fraction(value, dimension)


def _signal(signal):
    if not isinstance(signal, str) or not signal:
        raise TypeError("Signal must be a nonempty logical signal name")
    return signal
