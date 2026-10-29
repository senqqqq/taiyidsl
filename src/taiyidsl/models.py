"""Immutable qubit parameters and transactional calibration persistence.

All built-in parameter values use SI units. A Qubit is a value object: edits
through QubitStore produce a new revision, so experiments can retain snapshots.
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

from .units import Quantity, as_fraction

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_DRIVE_FIELDS = {"frequency", "amplitude", "pi_duration", "pi2_duration"}
_SCHEMA_VERSION = 1


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

    def to_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in sorted(_DRIVE_FIELDS)}

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
class QubitParameters:
    raman: DriveParameters = field(default_factory=DriveParameters)
    rf: DriveParameters = field(default_factory=DriveParameters)
    readout: ReadoutParameters = field(default_factory=ReadoutParameters)
    extensions: Mapping[str, Any] = field(default_factory=dict)

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "raman": self.raman.to_dict(),
            "rf": self.rf.to_dict(),
            "readout": self.readout.to_dict(),
            "extensions": _plain(self.extensions),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QubitParameters:
        _keys(data, {"raman", "rf", "readout", "extensions"}, "qubit parameter")
        return cls(
            raman=DriveParameters.from_dict(data.get("raman", {})),
            rf=DriveParameters.from_dict(data.get("rf", {})),
            readout=ReadoutParameters.from_dict(data.get("readout", {})),
            extensions=data.get("extensions", {}),
        )


@dataclass(frozen=True)
class Qubit:
    uid: str
    params: QubitParameters = field(default_factory=QubitParameters)
    revision: int = 0

    def __post_init__(self) -> None:
        _identifier(self.uid)
        _revision(self.revision)
        if not isinstance(self.params, QubitParameters):
            raise TypeError("params must be QubitParameters")

    @property
    def raman(self) -> str:
        return f"{self.uid}.raman"

    @property
    def rf(self) -> str:
        return f"{self.uid}.rf"

    @property
    def readout(self) -> str:
        return f"{self.uid}.readout"

    @property
    def frame(self) -> str:
        return self.uid

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "params": self.params.to_dict(),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Qubit:
        _keys(data, {"uid", "params", "revision"}, "qubit")
        return cls(
            uid=data["uid"],
            params=QubitParameters.from_dict(data.get("params", {})),
            revision=data.get("revision", 0),
        )

    def snapshot(self) -> Qubit:
        return Qubit.from_dict(self.to_dict())


def _expected_unit(path: str) -> set[str] | None:
    parts = path.split(".")
    if len(parts) == 2 and parts[0] in {"raman", "rf"} and parts[1] in _DRIVE_FIELDS:
        return (
            {"Hz"}
            if parts[1] == "frequency"
            else ({"1", "dimensionless"} if parts[1] == "amplitude" else {"s"})
        )
    if path == "readout.duration":
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
    for path, value in updates.items():
        if not isinstance(path, str):
            raise TypeError("parameter paths must be strings")
        dimension = _parameter_dimension(path)
        if dimension is not None:
            value = _number(value, path, dimension=dimension)
        parts = path.split(".")
        cursor = data
        for part in parts[:-1]:
            if part not in cursor or not isinstance(cursor[part], dict):
                raise ValueError(f"Parameter namespace does not exist: {path}")
            cursor = cursor[part]
        if parts[-1] not in cursor:
            raise ValueError(f"Parameter does not exist: {path}")
        cursor[parts[-1]] = _plain(_freeze(value))
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
            "schema_version": _SCHEMA_VERSION,
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
        if (
            type(data.get("schema_version")) is not int
            or data["schema_version"] != _SCHEMA_VERSION
        ):
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
        previous = self[uid]
        updated = _updated_qubit(previous, updates)
        normalized = {}
        for path in updates:
            value = updated.params.to_dict()
            for part in path.split("."):
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
