"""Small optical parameter tables, without execution order or recipe types.

File values are SI numbers: frequency_hz, duration_s and phase_rad. Amplitude is
the current adapter's normalized DDS setting, not a calibrated optical power.
"""

import json
import math
from collections.abc import Mapping
from pathlib import Path

from .parameters import ParameterSnapshot


def _fields(data, allowed, required, path):
    if not isinstance(data, Mapping):
        raise TypeError(f"{path} must be an object")
    unknown, missing = set(data) - set(allowed), set(required) - set(data)
    if unknown or missing:
        raise ValueError(
            f"{path}: unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    return dict(data)


def _number(value, path, *, minimum=0, maximum=math.inf, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path} requires a numeric SI value")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{path} is outside its finite allowed range")
    if positive and value == 0:
        raise ValueError(f"{path} must be positive")


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} requires nonempty text")


def _json_data(value, path="optics"):
    """Require JSON-compatible inputs, including user extension namespaces."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or not key for key in value):
            raise TypeError(f"{path} requires nonempty string keys")
        return {key: _json_data(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_data(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"{path} requires JSON data; use SI numbers in OpticsConfig")


_BEAM = {"signal", "frequency_hz", "amplitude", "phase_rad"}
_PULSE = _BEAM | {"duration_s", "setup_wait_s", "settle_s"}


def validate_beam(data, path="beam"):
    result = _fields(data, _BEAM, _BEAM - {"phase_rad"}, path)
    _text(result["signal"], f"{path}.signal")
    _number(result["frequency_hz"], f"{path}.frequency_hz")
    _number(result["amplitude"], f"{path}.amplitude", maximum=1)
    result.setdefault("phase_rad", 0)
    _number(result["phase_rad"], f"{path}.phase_rad", minimum=-math.inf)
    return result


def validate_pulse(data, path="pulse"):
    result = _fields(data, _PULSE, _PULSE - {"phase_rad"}, path)
    result.update(validate_beam({k: v for k, v in result.items() if k in _BEAM}, path))
    for name in ("duration_s", "setup_wait_s", "settle_s"):
        _number(result[name], f"{path}.{name}")
    return result


def validate_pair(data, path="raman"):
    keys = {"a", "b", "duration_s", "setup_wait_s"}
    result = _fields(data, keys, keys, path)
    for name in ("a", "b"):
        _text(result[name], f"{path}.{name}")
    if result["a"] == result["b"]:
        raise ValueError(f"{path}: Raman beams must be distinct")
    _number(result["duration_s"], f"{path}.duration_s", positive=True)
    _number(result["setup_wait_s"], f"{path}.setup_wait_s")
    return result


def _validate(data):
    keys = {
        "schema_version",
        "revision",
        "conditions",
        "pump",
        "pushout",
        "beams",
        "raman",
        "imaging",
        "camera",
        "extensions",
    }
    result = _fields(data, keys, {"schema_version"}, "optics")
    if type(result["schema_version"]) is not int or result["schema_version"] != 2:
        raise ValueError(
            "OpticsConfig requires schema_version=2 (flat parameter tables)"
        )
    if "revision" in result:
        _text(result["revision"], "revision")
    if "conditions" in result and not isinstance(result["conditions"], str):
        raise TypeError("conditions must be text")
    for name in ("pump", "pushout"):
        if name in result:
            result[name] = validate_pulse(result[name], name)
    for name in ("beams", "raman", "extensions"):
        if name in result and not isinstance(result[name], Mapping):
            raise TypeError(f"{name} must be an object")
    if "beams" in result:
        result["beams"] = {
            name: validate_beam(value, f"beams.{name}")
            for name, value in result["beams"].items()
        }
    if "raman" in result:
        result["raman"] = {
            name: validate_pair(value, f"raman.{name}")
            for name, value in result["raman"].items()
        }
        beams = result.get("beams", {})
        for name, pair in result["raman"].items():
            for beam in (pair["a"], pair["b"]):
                if beam not in beams:
                    raise ValueError(f"raman.{name}: unknown beam {beam}")
            if beams[pair["a"]]["signal"] == beams[pair["b"]]["signal"]:
                raise ValueError(f"raman.{name}: Raman beams need distinct signals")
    for section, field in (("imaging", "exposure_s"), ("camera", "transfer_wait_s")):
        if section in result:
            result[section] = _fields(result[section], {field}, {field}, section)
            _number(
                result[section][field],
                f"{section}.{field}",
                positive=section == "imaging",
            )
    return result


class OpticsConfig(ParameterSnapshot):
    """Validated shared optical values; every operation family is optional."""

    __slots__ = ()

    def __init__(self, data):
        super().__init__(_validate(_json_data(data)))

    @classmethod
    def from_dict(cls, data):
        return cls(data)

    @classmethod
    def coerce(cls, data):
        """Normalize a plain mapping once; reuse an already immutable config."""
        return data if isinstance(data, cls) else cls.from_dict(data)

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
