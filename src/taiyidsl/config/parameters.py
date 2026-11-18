"""Domain-independent immutable copies of experiment parameter mappings."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType

from ..units import Quantity, fraction


def _freeze(value, path="parameters"):
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or not key for key in value):
            raise TypeError(f"{path} requires nonempty string keys")
        return MappingProxyType(
            {key: _freeze(item, f"{path}.{key}") for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item, f"{path}[{i}]") for i, item in enumerate(value))
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if isinstance(value, Quantity):
        if not isinstance(value.dimension, str) or not value.dimension:
            raise TypeError(f"{path} requires a named quantity dimension")
        return Quantity(fraction(value.value), value.dimension)
    if value is None or isinstance(value, (str, bool, int, float, Fraction)):
        return value
    raise TypeError(f"{path}: unsupported parameter type {type(value).__name__}")


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, eq=False)
class ParameterSnapshot(Mapping):
    """Internal snapshot representation; callers can supply ordinary dictionaries."""

    _data: Mapping = field(repr=False)

    def __post_init__(self):
        if not isinstance(self._data, Mapping):
            raise TypeError("parameters must be a mapping")
        object.__setattr__(self, "_data", _freeze(self._data))

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def to_dict(self):
        """Return detached data; unit-bearing values remain immutable quantities."""
        return _thaw(self._data)

    def snapshot(self):
        """Copy without losing the configuration subtype or its validation contract."""
        return type(self)(self.to_dict())
