"""Uniform, explicitly supplied calibration parameters for signal roles.

The legacy ``QubitParameters`` model remains readable. This model is used by
new ``Qubit(signals=..., parameters=...)`` construction and never invents pulse
calibration values for omitted fields.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .models import DriveParameters, TransitionParameters, _freeze, _identifier, _number


@dataclass(frozen=True)
class RoleDriveParameters:
    frequency: float | None = None
    amplitude: float | None = None
    pi_duration: float | None = None
    pi2_duration: float | None = None
    pi_amplitude: float | None = None
    pi2_amplitude: float | None = None
    duration: float | None = None
    waveform: str = "square"
    waveform_parameters: Mapping = field(default_factory=dict)
    resonance_frequency: float | None = None
    frame: str | None = None
    transition: TransitionParameters | None = None

    def __post_init__(self):
        for name in (
            "frequency",
            "resonance_frequency",
            "pi_duration",
            "pi2_duration",
            "duration",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _number(
                        value,
                        name,
                        positive=True,
                        dimension="frequency" if "frequency" in name else "time",
                    ),
                )
        for name in ("amplitude", "pi_amplitude", "pi2_amplitude"):
            value = getattr(self, name)
            if value is not None:
                value = _number(value, name, dimension="dimensionless")
                if not 0 <= value <= 1:
                    raise ValueError(f"{name} must be between 0 and 1")
                object.__setattr__(self, name, value)
        # Reuse shape validation only; its legacy pulse defaults are not stored.
        validated = DriveParameters(
            waveform=self.waveform, waveform_parameters=self.waveform_parameters
        )
        object.__setattr__(self, "waveform_parameters", validated.waveform_parameters)
        if self.frame is not None:
            _identifier(self.frame, "control frame name")
        transition = self.transition
        if isinstance(transition, Mapping):
            transition = TransitionParameters(**transition)
        if transition is not None and not isinstance(transition, TransitionParameters):
            raise TypeError("transition must be a mapping or TransitionParameters")
        if (
            transition is not None
            and transition.resonance_frequency is not None
            and self.resonance_frequency is not None
            and transition.resonance_frequency != self.resonance_frequency
        ):
            raise ValueError("Conflicting resonance_frequency in transition")
        object.__setattr__(self, "transition", transition)

    def rotation_amplitude(self, rotation):
        if rotation not in {"pi", "pi2"}:
            raise ValueError("rotation must be pi or pi2")
        value = getattr(self, f"{rotation}_amplitude")
        return self.amplitude if value is None else value

    def to_dict(self):
        result = {
            name: getattr(self, name)
            for name in (
                "frequency",
                "amplitude",
                "pi_duration",
                "pi2_duration",
                "pi_amplitude",
                "pi2_amplitude",
                "resonance_frequency",
                "duration",
            )
            if getattr(self, name) is not None
        }
        result.update(waveform=self.waveform, frame=self.frame)
        if self.waveform_parameters:
            result["waveform_parameters"] = dict(self.waveform_parameters)
        if self.transition is not None:
            result["transition"] = self.transition.to_dict()
        return result


class RoleParameters(Mapping):
    """Read-only role namespace supporting both ``p.rf`` and ``p['rf']``."""

    __slots__ = ("_roles", "_extensions")

    def __init__(self, parameters, *, roles):
        if not isinstance(parameters, Mapping):
            raise TypeError("parameters must be a role-to-parameters mapping")
        unknown = set(parameters) - set(roles) - {"extensions"}
        if unknown:
            raise ValueError(
                f"Parameters have undeclared signal roles: {sorted(unknown)}"
            )
        values = {}
        for role in roles:
            raw = parameters.get(role, {})
            if isinstance(raw, RoleDriveParameters):
                values[role] = raw
            elif isinstance(raw, Mapping):
                data = dict(raw)
                data.setdefault("frame", "spin" if role in {"rf", "raman"} else None)
                values[role] = RoleDriveParameters(**data)
            else:
                raise TypeError(f"parameters.{role} must be a mapping")
        extensions = parameters.get("extensions", {})
        if not isinstance(extensions, Mapping):
            raise TypeError("extensions must be a mapping of namespaces")
        for name, value in extensions.items():
            _identifier(name, "extension namespace")
            if not isinstance(value, Mapping):
                raise TypeError("each extension namespace must contain a mapping")
        object.__setattr__(self, "_roles", MappingProxyType(values))
        object.__setattr__(self, "_extensions", _freeze(extensions))

    def __setattr__(self, name, value):
        raise AttributeError("Qubit parameter snapshots are read-only")

    def __getitem__(self, name):
        if name == "extensions":
            return self._extensions
        return self._roles[name]

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __iter__(self):
        return iter((*self._roles, "extensions"))

    def __len__(self):
        return len(self._roles) + 1

    def to_dict(self):
        from .models import _plain

        return {
            **{name: value.to_dict() for name, value in self._roles.items()},
            "extensions": _plain(self._extensions),
        }
