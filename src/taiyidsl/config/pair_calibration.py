"""Immutable, ordered two-qubit calibration snapshots for laboratory gate bodies."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .models import (
    QubitStore,
    _canonical,
    _freeze,
    _identifier,
    _number,
    _plain,
    _revision,
)


@dataclass(frozen=True)
class PairCalibration:
    """A gate contract, not evidence that an arbitrary waveform realizes CZ.

    Endpoint order retains the physical roles even for a symmetric ideal gate.
    Calibrated entries must pin both single-qubit snapshots. A laboratory gate
    callable consumes its parameters explicitly; the DSL never invents a CZ.
    """

    qubits: tuple[str, str]
    implementation: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    revision: int = 0
    qubit_revisions: Mapping[str, int] = field(default_factory=dict)
    qubit_fingerprints: Mapping[str, str] = field(default_factory=dict)
    phase_corrections: Mapping[str, float] = field(default_factory=dict)
    status: str = "unverified"
    source: str = ""

    def __post_init__(self):
        identifiers = tuple(self.qubits)
        if len(identifiers) != 2 or len(set(identifiers)) != 2:
            raise ValueError("PairCalibration needs two distinct ordered qubits")
        for uid in identifiers:
            _identifier(uid)
        object.__setattr__(self, "qubits", identifiers)
        _revision(self.revision)
        if not isinstance(self.implementation, str) or not self.implementation.strip():
            raise ValueError("PairCalibration requires an implementation name")
        if self.status not in {"unverified", "demonstration", "calibrated"}:
            raise ValueError("Invalid pair calibration status")
        if not isinstance(self.source, str):
            raise TypeError("Pair calibration source must be text")
        for field_name in (
            "qubit_revisions",
            "qubit_fingerprints",
            "phase_corrections",
        ):
            if not set(getattr(self, field_name)) <= set(identifiers):
                raise ValueError(f"{field_name} refers to another qubit")
        for value in self.qubit_revisions.values():
            _revision(value)
        for value in self.qubit_fingerprints.values():
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("Pair dependencies must be SHA-256 fingerprints")
        if self.status == "calibrated" and any(
            set(getattr(self, name)) != set(identifiers)
            for name in ("qubit_revisions", "qubit_fingerprints")
        ):
            raise ValueError(
                "Calibrated pairs must pin both qubit revisions and fingerprints"
            )
        object.__setattr__(
            self,
            "phase_corrections",
            {
                k: _number(v, "phase_correction", dimension="phase")
                for k, v in self.phase_corrections.items()
            },
        )
        for name in (
            "parameters",
            "qubit_revisions",
            "qubit_fingerprints",
            "phase_corrections",
        ):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    def validate(self, q0, q1):
        if (q0.uid, q1.uid) != self.qubits:
            raise ValueError("CZ endpoint order differs from pair calibration")
        for q in (q0, q1):
            if (
                q.uid in self.qubit_revisions
                and self.qubit_revisions[q.uid] != q.revision
            ):
                raise ValueError(f"Stale pair calibration for {q.uid}")
            if (
                q.uid in self.qubit_fingerprints
                and self.qubit_fingerprints[q.uid] != q.fingerprint
            ):
                raise ValueError(f"Pair calibration snapshot differs for {q.uid}")

    def to_dict(self):
        return {name: _plain(getattr(self, name)) for name in self.__dataclass_fields__}

    @property
    def fingerprint(self):
        import hashlib

        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    def revised(self, *, expected_revision, parameters=None, phase_corrections=None):
        if expected_revision != self.revision:
            raise ValueError("Stale pair calibration revision")
        return replace(
            self,
            revision=self.revision + 1,
            parameters=self.parameters if parameters is None else parameters,
            phase_corrections=self.phase_corrections
            if phase_corrections is None
            else phase_corrections,
            status="unverified",
        )

    def save(self, path):
        target = Path(path)
        QubitStore._atomic_write(target, {"schema_version": 1, "pair": self.to_dict()})
        return target

    @classmethod
    def load(cls, path):
        import json

        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if value.get("schema_version") != 1 or set(value) != {"schema_version", "pair"}:
            raise ValueError("Unsupported pair calibration schema")
        return cls(**value["pair"])
