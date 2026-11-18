"""Editable calibration objects, isolated snapshots and optional persistence.

Built-in parameter values use SI units. New-interface Qubits support validated
parameter assignment; experiments and QubitStore retain read-only snapshots.
"""

from __future__ import annotations

import hashlib
import json
import math
import numbers
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..units import Quantity, as_fraction

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_LEGACY_DRIVE_FIELDS = {"frequency", "amplitude", "pi_duration", "pi2_duration"}
_DRIVE_FIELDS = _LEGACY_DRIVE_FIELDS | {
    "pi_amplitude",
    "pi2_amplitude",
    "waveform",
    "waveform_parameters",
}
_SCHEMA_VERSION = 2


def _number(
    value: Any, name: str, *, positive: bool = False, dimension: str | None = None
) -> float:
    if isinstance(value, Quantity):
        if dimension is None:
            raise TypeError(f"{name} requires a numeric value without a physical unit")
        value = as_fraction(value, dimension)
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(
            f"{name} must be a finite SI numeric value, not {type(value).__name__}"
        )
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("revision must be a non-negative integer")
    return value


def _identifier(value: Any, label: str = "qubit ID") -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{label} must begin with a letter and contain letters, digits, '_' or '-'"
        )
    return value


def _freeze(value: Any) -> Any:
    """Deep-copy JSON values into immutable containers and reject non-finite data."""
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON mapping keys must be strings")
            result[key] = _freeze(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, numbers.Real):
        return _number(value, "JSON numeric value")
    raise TypeError(f"Unsupported configuration value: {type(value).__name__}")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"Unknown {name} fields: {sorted(extra)}")


@dataclass(frozen=True)
class DriveParameters:
    frequency: float = 80e6
    amplitude: float = 0.5
    pi_duration: float = 10e-6
    pi2_duration: float = 5e-6
    pi_amplitude: float | None = field(default=None, kw_only=True)
    pi2_amplitude: float | None = field(default=None, kw_only=True)
    waveform: str = field(default="square", kw_only=True)
    waveform_parameters: Mapping[str, Any] = field(default_factory=dict, kw_only=True)

    def __post_init__(self) -> None:
        for name in ("frequency", "pi_duration", "pi2_duration"):
            dimension = "frequency" if name == "frequency" else "time"
            object.__setattr__(
                self,
                name,
                _number(getattr(self, name), name, positive=True, dimension=dimension),
            )
        amplitude = _number(self.amplitude, "amplitude", dimension="dimensionless")
        if not 0 <= amplitude <= 1:
            raise ValueError(
                "amplitude must be between 0 and 1 (normalized full scale)"
            )
        object.__setattr__(self, "amplitude", amplitude)
        for name in ("pi_amplitude", "pi2_amplitude"):
            value = getattr(self, name)
            if value is not None:
                value = _number(value, name, dimension="dimensionless")
                if not 0 <= value <= 1:
                    raise ValueError(f"{name} must be between 0 and 1")
                object.__setattr__(self, name, value)
        if self.waveform not in {"square", "flat_top_gaussian"}:
            raise ValueError("waveform must be square or flat_top_gaussian")
        allowed = (
            set()
            if self.waveform == "square"
            else {"sigma", "cutoff_sigma", "sample_interval"}
        )
        _keys(self.waveform_parameters, allowed, "waveform parameter")
        shape = {}
        for name, value in self.waveform_parameters.items():
            shape[name] = _number(
                value,
                name,
                positive=True,
                dimension=("dimensionless" if name == "cutoff_sigma" else "time"),
            )
        if (
            self.waveform == "flat_top_gaussian"
            and not {"sigma", "sample_interval"} <= shape.keys()
        ):
            raise ValueError("flat_top_gaussian requires sigma and sample_interval")
        object.__setattr__(self, "waveform_parameters", _freeze(shape))

    def rotation_amplitude(self, rotation: str) -> float:
        """Resolve an independently calibrated pi/pi2 amplitude, including zero."""
        if rotation not in {"pi", "pi2"}:
            raise ValueError("rotation must be pi or pi2")
        value = getattr(self, f"{rotation}_amplitude")
        return self.amplitude if value is None else value

    def to_dict(self) -> dict[str, float]:
        data = {name: getattr(self, name) for name in sorted(_LEGACY_DRIVE_FIELDS)}
        for name in ("pi_amplitude", "pi2_amplitude"):
            if getattr(self, name) is not None:
                data[name] = getattr(self, name)
        if self.waveform != "square":
            data["waveform"] = self.waveform
            data["waveform_parameters"] = _plain(self.waveform_parameters)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DriveParameters:
        _keys(data, _DRIVE_FIELDS, "drive parameter")
        return cls(**data)


@dataclass(frozen=True)
class ReadoutParameters:
    duration: float = 10e-6

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "duration",
            _number(self.duration, "readout.duration", positive=True, dimension="time"),
        )

    def to_dict(self) -> dict[str, float]:
        return {"duration": self.duration}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReadoutParameters:
        _keys(data, {"duration"}, "readout parameter")
        return cls(**data)


@dataclass(frozen=True)
class TransitionParameters:
    """Physical level labels; resonance is not a waveform generator frequency."""

    lower: str
    upper: str
    resonance_frequency: float | None = None
    description: str = ""

    def __post_init__(self):
        if not all(isinstance(x, str) and x.strip() for x in (self.lower, self.upper)):
            raise ValueError("Transition level labels must be nonempty")
        if self.lower == self.upper:
            raise ValueError("Transition levels must differ")
        if not isinstance(self.description, str):
            raise TypeError("Transition description must be text")
        if self.resonance_frequency is not None:
            object.__setattr__(
                self,
                "resonance_frequency",
                _number(
                    self.resonance_frequency,
                    "resonance_frequency",
                    positive=True,
                    dimension="frequency",
                ),
            )

    def to_dict(self):
        return {
            "lower": self.lower,
            "upper": self.upper,
            "resonance_frequency": self.resonance_frequency,
            "description": self.description,
        }


@dataclass(frozen=True)
class Position:
    """Static coordinates in metres, in a named laboratory coordinate system."""

    x: float
    y: float
    z: float
    coordinate_system: str

    def __post_init__(self):
        if (
            not isinstance(self.coordinate_system, str)
            or not self.coordinate_system.strip()
        ):
            raise ValueError("Position requires a coordinate system")
        for name in ("x", "y", "z"):
            object.__setattr__(
                self, name, _number(getattr(self, name), name, dimension="length")
            )

    def to_dict(self):
        return {
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "coordinate_system": self.coordinate_system,
        }


@dataclass(frozen=True)
class QubitParameters:
    raman: DriveParameters = field(default_factory=DriveParameters)
    rf: DriveParameters = field(default_factory=DriveParameters)
    readout: ReadoutParameters = field(default_factory=ReadoutParameters)
    extensions: Mapping[str, Any] = field(default_factory=dict)
    drives: Mapping[str, DriveParameters] = field(default_factory=dict, kw_only=True)
    transitions: Mapping[str, TransitionParameters] = field(
        default_factory=dict, kw_only=True
    )
    drive_transitions: Mapping[str, str] = field(default_factory=dict, kw_only=True)
    drive_frames: Mapping[str, str | None] = field(default_factory=dict, kw_only=True)
    position: Position | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.raman, DriveParameters) or not isinstance(
            self.rf, DriveParameters
        ):
            raise TypeError("raman and rf must be DriveParameters")
        if not isinstance(self.readout, ReadoutParameters):
            raise TypeError("readout must be ReadoutParameters")
        if not isinstance(self.extensions, Mapping):
            raise TypeError("extensions must be a mapping of namespaces to mappings")
        for namespace, values in self.extensions.items():
            _identifier(namespace, "extension namespace")
            if not isinstance(values, Mapping):
                raise TypeError("each extension namespace must contain a mapping")
        object.__setattr__(self, "extensions", _freeze(self.extensions))
        for name, value in self.drives.items():
            _identifier(name, "drive name")
            if name in {"rf", "raman", "readout"}:
                raise ValueError(f"Reserved drive name: {name}")
            if not isinstance(value, DriveParameters):
                raise TypeError("drives must contain DriveParameters")
        for name, value in self.transitions.items():
            _identifier(name, "transition name")
            if not isinstance(value, TransitionParameters):
                raise TypeError("transitions must contain TransitionParameters")
        routes = {"rf", "raman", *self.drives}
        for route, transition in self.drive_transitions.items():
            if route not in routes or transition not in self.transitions:
                raise ValueError(
                    "drive_transitions requires declared drives and transitions"
                )
        for route, frame in self.drive_frames.items():
            if route not in routes:
                raise ValueError(f"Unknown frame drive: {route}")
            if frame is not None:
                _identifier(frame, "control frame name")
        if self.position is not None and not isinstance(self.position, Position):
            raise TypeError("position must be Position")
        for name in ("drives", "transitions", "drive_transitions", "drive_frames"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "raman": self.raman.to_dict(),
            "rf": self.rf.to_dict(),
            "readout": self.readout.to_dict(),
            "extensions": _plain(self.extensions),
        }
        for name in ("drives", "transitions"):
            if getattr(self, name):
                data[name] = {k: v.to_dict() for k, v in getattr(self, name).items()}
        for name in ("drive_transitions", "drive_frames"):
            if getattr(self, name):
                data[name] = dict(getattr(self, name))
        if self.position is not None:
            data["position"] = self.position.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QubitParameters:
        _keys(
            data,
            {
                "raman",
                "rf",
                "readout",
                "extensions",
                "drives",
                "transitions",
                "drive_transitions",
                "drive_frames",
                "position",
            },
            "qubit parameter",
        )
        return cls(
            raman=DriveParameters.from_dict(data.get("raman", {})),
            rf=DriveParameters.from_dict(data.get("rf", {})),
            readout=ReadoutParameters.from_dict(data.get("readout", {})),
            extensions=data.get("extensions", {}),
            drives={
                k: DriveParameters.from_dict(v)
                for k, v in data.get("drives", {}).items()
            },
            transitions={
                k: TransitionParameters(**v)
                for k, v in data.get("transitions", {}).items()
            },
            drive_transitions=data.get("drive_transitions", {}),
            drive_frames=data.get("drive_frames", {}),
            position=Position(**data["position"])
            if data.get("position") is not None
            else None,
        )


@dataclass(frozen=True, init=False)
class Qubit:
    uid: str
    params: QubitParameters = field(default_factory=QubitParameters)
    revision: int = 0
    _signals: Mapping[str, str] = field(default_factory=dict, repr=False)
    _position: Position | None = field(default=None, repr=False)
    _unified: bool = field(default=False, repr=False)
    _editable: bool = field(default=True, repr=False, compare=False)

    def __init__(
        self,
        uid,
        params=None,
        revision=0,
        *,
        signals=None,
        parameters=None,
        position=None,
        _signals=None,
        _position=None,
        _unified=False,
        _editable=True,
    ):
        from .role_parameters import RoleParameters

        _identifier(uid)
        _revision(revision)
        # Private arguments let dataclasses.replace preserve the value object.
        unified = signals is not None or parameters is not None or _unified
        if unified:
            if params is not None and not isinstance(params, RoleParameters):
                raise TypeError("Use either legacy params or signals/parameters")
            signals = signals if signals is not None else _signals
            if not isinstance(signals, Mapping) or not signals:
                raise ValueError("New Qubit construction requires explicit signals")
            for role, signal in signals.items():
                _identifier(role, "signal role")
                if role.startswith("_") or role in {
                    "uid",
                    "params",
                    "parameters",
                    "position",
                    "revision",
                    "signals",
                    "drive",
                    "frame",
                    "drive_names",
                    "fingerprint",
                    "signal",
                    "snapshot",
                    "to_dict",
                    "from_dict",
                    "drive_frame",
                    "phase_frame",
                    "extensions",
                    "items",
                    "keys",
                    "values",
                    "get",
                }:
                    raise ValueError(f"Reserved Qubit signal role: {role}")
                if (
                    not isinstance(signal, str)
                    or not signal
                    or signal.strip() != signal
                ):
                    raise ValueError("Logical signals must be nonempty strings")
            if len(set(signals.values())) != len(signals):
                raise ValueError(
                    "Qubit signal roles must have distinct logical signals"
                )
            parameters = (
                parameters
                if parameters is not None
                else (params.to_dict() if params is not None else {})
            )
            params = RoleParameters(parameters, roles=signals)
            position = position if position is not None else _position
            if isinstance(position, Mapping):
                position = Position(**position)
            if position is not None and not isinstance(position, Position):
                raise TypeError("position must be Position or a coordinate mapping")
        else:
            if position is not None:
                raise TypeError(
                    "Top-level position requires the signals/parameters API"
                )
            params = QubitParameters() if params is None else params
            if not isinstance(params, QubitParameters):
                raise TypeError("params must be QubitParameters")
            signals = {
                name: f"{uid}.{name}"
                for name in ("rf", "raman", "readout", *params.drives)
            }
            position = params.position
        for name, value in (
            ("uid", uid),
            ("params", params),
            ("revision", revision),
            ("_signals", MappingProxyType(dict(signals))),
            ("_position", position),
            ("_unified", unified),
            ("_editable", bool(_editable and unified)),
        ):
            object.__setattr__(self, name, value)

    @property
    def signals(self):
        return self._signals

    @property
    def parameters(self):
        if self._unified:
            if self._editable:
                from .editable_parameters import ParameterNamespace

                return ParameterNamespace(self)
            return self.params
        from .role_parameters import RoleParameters

        values = {}
        for role in self.drive_names:
            data = self.drive(role).to_dict()
            data["frame"] = self.params.drive_frames.get(
                role, "spin" if role in {"rf", "raman"} else None
            )
            transition = self.params.drive_transitions.get(role)
            if transition is not None:
                data["transition"] = self.params.transitions[transition].to_dict()
            values[role] = data
        values["readout"] = self.params.readout.to_dict()
        values["extensions"] = _plain(self.params.extensions)
        return RoleParameters(values, roles=self.signals)

    @property
    def position(self):
        return self._position

    def __getattr__(self, name):
        signals = object.__getattribute__(self, "_signals")
        if name in signals:
            return signals[name]
        raise AttributeError(name)

    @property
    def raman(self) -> str:
        return self.signal("raman")

    @property
    def rf(self) -> str:
        return self.signal("rf")

    @property
    def readout(self) -> str:
        return self.signal("readout")

    @property
    def frame(self) -> str:
        return self.uid

    @property
    def drive_names(self) -> tuple[str, ...]:
        if self._unified:
            return tuple(name for name in self.signals if name != "readout")
        return ("rf", "raman", *self.params.drives)

    def drive(self, name: str) -> DriveParameters:
        if self._unified:
            if name not in self.drive_names:
                raise ValueError(f"Qubit {self.uid} has no declared drive {name}")
            return self.params[name]
        if name in {"rf", "raman"}:
            return getattr(self.params, name)
        if name not in self.params.drives:
            raise ValueError(f"Qubit {self.uid} has no declared drive {name}")
        return self.params.drives[name]

    def signal(self, name: str) -> str:
        if name not in self.signals:
            raise ValueError(f"Qubit {self.uid} has no declared signal {name}")
        return self.signals[name]

    def phase_frame(self, name: str = "spin") -> str:
        """An explicitly declared control phase, not an automatic level rotation."""
        if name == "spin":
            return self.uid
        labels = (
            [self.drive(role).frame for role in self.drive_names]
            if self._unified
            else self.params.drive_frames.values()
        )
        if name not in labels:
            raise ValueError(f"Qubit {self.uid} has no declared control frame {name}")
        return f"{self.uid}__phase__{name}"

    def drive_frame(self, name: str) -> str | None:
        self.drive(name)
        if self._unified:
            label = self.drive(name).frame
            return None if label is None else self.phase_frame(label)
        label = self.params.drive_frames.get(
            name, "spin" if name in {"rf", "raman"} else None
        )
        return None if label is None else self.phase_frame(label)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        if self._unified:
            return {
                "schema_version": 3,
                "uid": self.uid,
                "signals": dict(self.signals),
                "parameters": self.params.to_dict(),
                "position": self.position.to_dict() if self.position else None,
                "revision": self.revision,
            }
        return {
            "uid": self.uid,
            "params": self.params.to_dict(),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Qubit:
        if "schema_version" in data:
            _keys(
                data,
                {
                    "schema_version",
                    "uid",
                    "signals",
                    "parameters",
                    "position",
                    "revision",
                },
                "qubit",
            )
            if type(data["schema_version"]) is not int or data["schema_version"] != 3:
                raise ValueError("Unsupported Qubit schema_version")
            return cls(
                data["uid"],
                signals=data["signals"],
                parameters=data["parameters"],
                position=data.get("position"),
                revision=data.get("revision", 0),
            )
        _keys(data, {"uid", "params", "revision"}, "qubit")
        return cls(
            uid=data["uid"],
            params=QubitParameters.from_dict(data.get("params", {})),
            revision=data.get("revision", 0),
        )

    def snapshot(self) -> Qubit:
        """Capture the current values without allowing later edits through them."""
        result = Qubit.from_dict(self.to_dict())
        object.__setattr__(result, "_editable", False)
        return result

    def _edit_parameters(self, change):
        """Publish one validated edit; failed writes leave values/version intact."""
        from .role_parameters import RoleParameters

        if not self._editable:
            raise AttributeError("Qubit parameter snapshots are read-only")
        data = self.params.to_dict()
        change(data)
        parameters = RoleParameters(data, roles=self.signals)
        if parameters.to_dict() == self.params.to_dict():
            return
        object.__setattr__(self, "params", parameters)
        object.__setattr__(self, "revision", self.revision + 1)


def _expected_unit(path: str) -> set[str] | None:
    parts = path.split(".")
    if parts[0] == "parameters":
        parts = parts[1:]
    if len(parts) >= 3 and parts[0] == "drives":
        _identifier(parts[1], "drive name")
        parts = ["rf", *parts[2:]]
    if (
        len(parts) == 2
        and _IDENTIFIER.fullmatch(parts[0])
        and parts[1]
        in (_DRIVE_FIELDS | {"resonance_frequency", "duration"})
        - {"waveform", "waveform_parameters"}
    ):
        return (
            {"Hz"}
            if parts[1] in {"frequency", "resonance_frequency"}
            else ({"1", "dimensionless"} if parts[1].endswith("amplitude") else {"s"})
        )
    if (
        len(parts) == 3
        and _IDENTIFIER.fullmatch(parts[0])
        and parts[1] == "waveform_parameters"
        and parts[2] in {"sigma", "cutoff_sigma", "sample_interval"}
    ):
        return {"1", "dimensionless"} if parts[2] == "cutoff_sigma" else {"s"}
    if (
        len(parts) == 3
        and parts[0] == "transitions"
        and parts[2] == "resonance_frequency"
    ):
        return {"Hz"}
    if len(parts) == 2 and parts[0] == "position" and parts[1] in {"x", "y", "z"}:
        return {"m"}
    if parts == ["readout", "duration"]:
        return {"s"}
    if (
        len(parts) >= 3
        and parts[0] == "extensions"
        and all(_IDENTIFIER.fullmatch(p) for p in parts[1:])
    ):
        return None  # Extension dimensions are supplied explicitly by the caller.
    raise ValueError(f"Unknown calibration parameter path: {path!r}")


def _parameter_dimension(path: str) -> str | None:
    units = _expected_unit(path)
    if units is None:
        return None
    if "Hz" in units:
        return "frequency"
    if "m" in units:
        return "length"
    return "time" if "s" in units else "dimensionless"


@dataclass(frozen=True)
class CalibrationProposal:
    qubit_id: str
    base_revision: int
    base_fingerprint: str
    updates: Mapping[str, float]
    units: Mapping[str, str]
    quality: Mapping[str, float]
    source_experiment: str
    success: bool = True
    analysis_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.qubit_id)
        _revision(self.base_revision)
        if not isinstance(self.base_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.base_fingerprint
        ):
            raise ValueError("base_fingerprint must be a Qubit SHA-256 fingerprint")
        if not isinstance(self.success, bool):
            raise TypeError("success must be boolean")
        if not isinstance(self.source_experiment, str) or not self.source_experiment:
            raise ValueError("source_experiment is required")
        if not isinstance(self.updates, Mapping) or not self.updates:
            raise ValueError("a proposal must contain at least one parameter update")
        if not isinstance(self.units, Mapping) or set(self.units) != set(self.updates):
            raise ValueError("units must specify exactly the updated parameter paths")
        values = {}
        for path, value in self.updates.items():
            if not isinstance(path, str):
                raise TypeError("parameter paths must be strings")
            accepted = _expected_unit(path)
            unit = self.units[path]
            if (
                not isinstance(unit, str)
                or not unit
                or (accepted is not None and unit not in accepted)
            ):
                raise ValueError(
                    f"Invalid SI unit {unit!r} for {path}; "
                    f"expected {accepted or 'an explicit extension unit'}"
                )
            values[path] = _number(value, path, dimension=_parameter_dimension(path))
        if not isinstance(self.quality, Mapping):
            raise TypeError("quality must be a mapping of numeric fit metrics")
        quality = {
            name: _number(value, f"quality.{name}")
            for name, value in self.quality.items()
        }
        if any(not isinstance(name, str) or not name for name in quality):
            raise ValueError("quality metric names must be nonempty strings")
        if not isinstance(self.analysis_metadata, Mapping):
            raise TypeError("analysis_metadata must be a JSON-safe mapping")
        object.__setattr__(self, "updates", _freeze(values))
        object.__setattr__(self, "units", _freeze(self.units))
        object.__setattr__(self, "quality", _freeze(quality))
        object.__setattr__(self, "analysis_metadata", _freeze(self.analysis_metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "qubit_id": self.qubit_id,
            "base_revision": self.base_revision,
            "base_fingerprint": self.base_fingerprint,
            "updates": _plain(self.updates),
            "units": _plain(self.units),
            "quality": _plain(self.quality),
            "source_experiment": self.source_experiment,
            "success": self.success,
            "analysis_metadata": _plain(self.analysis_metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CalibrationProposal:
        _keys(
            data,
            {
                "qubit_id",
                "base_revision",
                "base_fingerprint",
                "updates",
                "units",
                "quality",
                "source_experiment",
                "success",
                "analysis_metadata",
            },
            "calibration proposal",
        )
        return cls(**data)


@dataclass(frozen=True)
class CalibrationPolicy:
    """Explicit opt-in acceptance thresholds used only by apply_auto."""

    min_r_squared: float = 0.95
    max_relative_uncertainty: float | None = None

    def __post_init__(self) -> None:
        minimum = _number(self.min_r_squared, "min_r_squared")
        if not 0 <= minimum <= 1:
            raise ValueError("min_r_squared must lie in [0, 1]")
        object.__setattr__(self, "min_r_squared", minimum)
        if self.max_relative_uncertainty is not None:
            maximum = _number(
                self.max_relative_uncertainty, "max_relative_uncertainty", positive=True
            )
            object.__setattr__(self, "max_relative_uncertainty", maximum)

    def validate(self, proposal: CalibrationProposal) -> None:
        if not proposal.success:
            raise ValueError("Cannot apply an unsuccessful calibration")
        score = proposal.quality.get("r_squared")
        if score is None or not self.min_r_squared <= score <= 1:
            raise ValueError(
                "Calibration does not satisfy the r_squared quality threshold"
            )
        if self.max_relative_uncertainty is not None:
            uncertainty = proposal.quality.get("relative_uncertainty")
            if (
                uncertainty is None
                or not 0 <= uncertainty <= self.max_relative_uncertainty
            ):
                raise ValueError(
                    "Calibration does not satisfy the "
                    "relative_uncertainty quality threshold"
                )


def _updated_qubit(qubit: Qubit, updates: Mapping[str, Any]) -> Qubit:
    if not isinstance(updates, Mapping) or not updates:
        raise ValueError("updates must be a nonempty mapping")
    data = qubit.params.to_dict()
    if qubit._unified and qubit.position is not None:
        data["position"] = qubit.position.to_dict()
    for path, value in updates.items():
        if not isinstance(path, str):
            raise TypeError("parameter paths must be strings")
        dimension = _parameter_dimension(path)
        if dimension is not None:
            value = _number(value, path, dimension=dimension)
        parts = path.split(".")
        if qubit._unified and parts[0] == "parameters":
            parts = parts[1:]
        cursor = data
        # Custom namespaced values may be introduced after a Qubit is created.
        # Typed drive fields remain strict; existing scalar values are never
        # replaced by dictionaries just to make a deeper path writable.
        create_namespaces = qubit._unified and parts[0] == "extensions"
        for part in parts[:-1]:
            if part not in cursor and create_namespaces:
                cursor[part] = {}
            if part not in cursor or not isinstance(cursor[part], dict):
                raise ValueError(f"Parameter namespace does not exist: {path}")
            cursor = cursor[part]
        if (
            parts[-1] not in cursor
            and not qubit._unified
            and parts[-1]
            not in {
                "pi_amplitude",
                "pi2_amplitude",
            }
        ):
            raise ValueError(f"Parameter does not exist: {path}")
        cursor[parts[-1]] = _plain(_freeze(value))
    if qubit._unified:
        return Qubit(
            qubit.uid,
            signals=qubit.signals,
            parameters={
                name: value for name, value in data.items() if name != "position"
            },
            position=data.get("position"),
            revision=qubit.revision + 1,
        )
    return Qubit(qubit.uid, QubitParameters.from_dict(data), qubit.revision + 1)


class QubitStore(Mapping[str, Qubit]):
    """Mapping with immutable entries and atomic, revisioned updates.

    Use ``store[uid]`` again after an update to obtain the latest revision.
    References obtained earlier deliberately remain unchanged.
    """

    def __init__(
        self,
        qubits: Iterable[Qubit] | Mapping[str, Qubit] = (),
        path: str | Path | None = None,
    ):
        values = qubits.values() if isinstance(qubits, Mapping) else qubits
        self._qubits: dict[str, Qubit] = {}
        for qubit in values:
            if not isinstance(qubit, Qubit):
                raise TypeError("QubitStore entries must be Qubit objects")
            if qubit.uid in self._qubits:
                raise ValueError(f"Duplicate qubit ID: {qubit.uid}")
            self._qubits[qubit.uid] = qubit.snapshot()
        self.path = Path(path) if path is not None else None
        self._history: tuple[Mapping[str, Any], ...] = ()
        self._disk_digest: str | None = None

    def __getitem__(self, uid: str) -> Qubit:
        return self._qubits[uid]

    def __iter__(self) -> Iterator[str]:
        return iter(self._qubits)

    def __len__(self) -> int:
        return len(self._qubits)

    @property
    def history(self) -> tuple[Mapping[str, Any], ...]:
        return self._history

    def to_dict(self) -> dict[str, Any]:
        return self._payload(self._qubits, self._history)

    @staticmethod
    def _payload(
        qubits: Mapping[str, Qubit], history: Iterable[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return {
            "schema_version": 3
            if any(q._unified for q in qubits.values())
            else _SCHEMA_VERSION,
            "qubits": [qubits[uid].to_dict() for uid in sorted(qubits)],
            "history": [_plain(entry) for entry in history],
        }

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> str:
        contents = (
            json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return hashlib.sha256(contents).hexdigest()

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("A path is required to save the QubitStore")
        digest = self._atomic_write(target, self.to_dict())
        self.path = target
        self._disk_digest = digest
        return target

    @classmethod
    def load(cls, path: str | Path) -> QubitStore:
        target = Path(path)
        contents = target.read_bytes()
        data = json.loads(contents)
        _keys(data, {"schema_version", "qubits", "history"}, "QubitStore")
        if type(data.get("schema_version")) is not int or data[
            "schema_version"
        ] not in {1, 2, 3}:
            raise ValueError("Unsupported QubitStore schema_version")
        if not isinstance(data.get("qubits"), list) or not isinstance(
            data.get("history", []), list
        ):
            raise ValueError("QubitStore qubits and history must be lists")
        store = cls((Qubit.from_dict(item) for item in data["qubits"]), path=target)
        if any(not isinstance(entry, Mapping) for entry in data.get("history", [])):
            raise ValueError("History entries must be mappings")
        store._history = tuple(_freeze(entry) for entry in data.get("history", []))
        store._disk_digest = hashlib.sha256(contents).hexdigest()
        return store

    def _check_disk_unchanged(self) -> None:
        assert self.path is not None
        if self.path.exists():
            contents = self.path.read_bytes()
            if self._disk_digest is not None:
                if hashlib.sha256(contents).hexdigest() != self._disk_digest:
                    raise ValueError(
                        "QubitStore file changed externally; "
                        "reload before applying updates"
                    )
            elif _canonical(json.loads(contents)) != _canonical(self.to_dict()):
                raise ValueError(
                    "QubitStore file does not match the current store; "
                    "reload before applying updates"
                )
        elif self._disk_digest is not None:
            raise ValueError(
                "QubitStore file disappeared; "
                "explicitly save or reload before applying updates"
            )

    def _commit(
        self, previous: Qubit, updated: Qubit, record: Mapping[str, Any], persist: bool
    ) -> Qubit:
        if not isinstance(persist, bool):
            raise TypeError("persist must be boolean")
        updated = updated.snapshot()
        next_qubits = dict(self._qubits)
        next_qubits[updated.uid] = updated
        entry = {
            "qubit_id": updated.uid,
            "old_revision": previous.revision,
            "new_revision": updated.revision,
            "old_fingerprint": previous.fingerprint,
            "new_fingerprint": updated.fingerprint,
            "before": previous.to_dict(),
            "after": updated.to_dict(),
            "timestamp_utc": datetime.now(UTC).isoformat(),
            **record,
        }
        next_history = self._history + (_freeze(entry),)
        digest = self._disk_digest
        if persist:
            if self.path is None:
                raise ValueError("persist=True requires a configured QubitStore path")
            self._check_disk_unchanged()
            digest = self._atomic_write(
                self.path, self._payload(next_qubits, next_history)
            )
        # Publish the new in-memory revision only after persistence succeeded.
        self._qubits = next_qubits
        self._history = next_history
        self._disk_digest = digest
        return updated

    def update(
        self, uid: str, updates: Mapping[str, Any], *, persist: bool = False
    ) -> Qubit:
        """Return a new revision from dotted parameter paths.

        Unified Qubits can add custom values under ``extensions.<namespace>``;
        missing extension namespaces are created, but typed drive paths and
        existing scalar namespaces remain strict. Extensions are JSON values;
        their physical units must be documented explicitly by the laboratory.
        """
        previous = self[uid]
        updated = _updated_qubit(previous, updates)
        normalized = {}
        for path in updates:
            value = updated.params.to_dict()
            if updated._unified and updated.position is not None:
                value["position"] = updated.position.to_dict()
            parts = path.split(".")
            if updated._unified and parts[0] == "parameters":
                parts = parts[1:]
            for part in parts:
                value = value[part]
            normalized[path] = value
        return self._commit(
            previous, updated, {"kind": "manual", "updates": normalized}, persist
        )

    def apply(self, proposal: CalibrationProposal, *, persist: bool = True) -> Qubit:
        if not isinstance(proposal, CalibrationProposal):
            raise TypeError("proposal must be CalibrationProposal")
        if not proposal.success:
            raise ValueError("Cannot apply an unsuccessful calibration")
        previous = self[proposal.qubit_id]
        if (
            previous.revision != proposal.base_revision
            or previous.fingerprint != proposal.base_fingerprint
        ):
            raise ValueError(
                "Stale calibration proposal: Qubit revision or fingerprint changed"
            )
        updated = _updated_qubit(previous, proposal.updates)
        return self._commit(
            previous,
            updated,
            {"kind": "calibration", "proposal": proposal.to_dict()},
            persist,
        )

    def apply_auto(
        self,
        proposal: CalibrationProposal,
        policy: CalibrationPolicy,
        *,
        persist: bool = True,
    ) -> Qubit:
        if not isinstance(policy, CalibrationPolicy):
            raise TypeError(
                "Automatic calibration requires an explicit CalibrationPolicy"
            )
        policy.validate(proposal)
        return self.apply(proposal, persist=persist)
