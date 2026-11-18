"""Legacy JSON encoding for already resolved configuration dictionaries.

Configuration uses floating-point SI numbers, including Fraction values. This
wire format differs from the exact-value experiment/archive serializer; keeping
it here avoids depending on experiment nodes merely to serialize a setup.
"""

from fractions import Fraction
from types import MappingProxyType

from ..units import Quantity


def config_json_value(value):
    """Encode configuration primitives without changing their stored schema."""
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, Quantity):
        return {"value": float(value), "dimension": value.dimension}
    if isinstance(value, dict) or isinstance(value, MappingProxyType):
        return {key: config_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [config_json_value(item) for item in value]
    return value
