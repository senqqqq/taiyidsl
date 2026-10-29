"""Shot-resolved offline results. No hardware or detector is accessed here."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np

STATUSES = frozenset(
    {"valid", "missing", "failed", "unexecuted", "atom_lost", "loading_failed"}
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


class ResultSet:
    """Numeric observations indexed by ``[point, shot, qubit]`` per handle.

    Image data are referenced through ``assets``; status and labels are never
    averaged. Unexecuted and invalid observations remain NaN, not a dark-state 0.
    """

    def __init__(
        self,
        *,
        axes,
        coordinates,
        shots: int,
        qubits,
        handles,
        metadata=None,
        provenance=None,
        qubit_snapshots=None,
    ):
        if isinstance(shots, bool) or not isinstance(shots, int) or shots <= 0:
            raise ValueError("shots must be a positive integer")
        self.axes = _jsonable(axes)
        self.coordinates = _jsonable(coordinates)
        self.shots = shots
        self.qubits = tuple(qubits)
        self.handles = tuple(handles)
        if not self.coordinates or not self.qubits or not self.handles:
            raise ValueError("Results need points, qubits and measurement handles")
        if len(set(self.qubits)) != len(self.qubits) or len(set(self.handles)) != len(
            self.handles
        ):
            raise ValueError("Duplicate qubits or measurement handles")
        shape = (len(self.coordinates), shots, len(self.qubits))
        self.data = {h: np.full(shape, np.nan, dtype=float) for h in self.handles}
        self.status = {
            h: np.full(shape, "unexecuted", dtype="<U16") for h in self.handles
        }
        self.metadata = _jsonable(metadata or {})
        self.provenance = _jsonable(provenance or {})
        self.qubit_snapshots = _jsonable(qubit_snapshots or {})
        self.execution_order: list[tuple[int, int]] = []
        self.labels: dict[str, dict] = {}
        self.assets: dict[str, str] = {}

    def _index(self, point, shot, qubit):
        qi = self.qubits.index(qubit) if isinstance(qubit, str) else qubit
        if any(
            isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer))
            for v in (point, shot, qi)
        ):
            raise TypeError("Observation indices must be integers")
        if not (
            0 <= point < len(self.coordinates)
            and 0 <= shot < self.shots
            and 0 <= qi < len(self.qubits)
        ):
            raise IndexError("Observation index outside result dimensions")
        return int(point), int(shot), int(qi)

    @staticmethod
    def _key(handle, idx):
        return json.dumps([handle, *idx], separators=(",", ":"))

    def record(
        self,
        handle,
        point,
        shot,
        qubit,
        value=None,
        *,
        status="valid",
        labels=None,
        asset=None,
    ):
        if handle not in self.data:
            raise KeyError(f"Unknown measurement handle {handle!r}")
        if status not in STATUSES:
            raise ValueError(f"Unknown observation status {status!r}")
        idx = self._index(point, shot, qubit)
        if status == "valid":
            if value is None and asset is None:
                raise ValueError(
                    "A valid observation needs a finite numeric value "
                    "or asset reference"
                )
            if value is not None and (
                not np.isscalar(value) or not np.isfinite(float(value))
            ):
                raise ValueError("Numeric observations must be finite")
            self.data[handle][idx] = float(value) if value is not None else np.nan
        else:
            self.data[handle][idx] = np.nan
        self.status[handle][idx] = status
        key = self._key(handle, idx)
        self.labels.pop(key, None)
        self.assets.pop(key, None)
        if labels is not None:
            self.labels[key] = _jsonable(labels)
        if asset is not None:
            self.assets[key] = str(asset)

    def valid_mask(self, handle, *, postselection=None):
        mask = (self.status[handle] == "valid") & np.isfinite(self.data[handle])
        if postselection:
            mask = mask.copy()
            for idx in np.ndindex(mask.shape):
                labels = self.labels.get(self._key(handle, idx), {})
                mask[idx] &= all(labels.get(k) == v for k, v in postselection.items())
        return mask

    def aggregate(self, handle="population", *, postselection=None):
        mask = self.valid_mask(handle, postselection=postselection)
        counts = mask.sum(axis=1)
        sums = np.where(mask, self.data[handle], 0).sum(axis=1)
        values = np.divide(
            sums, counts, out=np.full(sums.shape, np.nan), where=counts > 0
        )
        return {
            "mean": values,
            "valid_counts": counts,
            "postselection": dict(postselection or {}),
        }

    def means(self, handle="population", *, postselection=None):
        return self.aggregate(handle, postselection=postselection)["mean"]

    def valid_counts(self, handle="population", *, postselection=None):
        return self.aggregate(handle, postselection=postselection)["valid_counts"]

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "axes": self.axes,
            "coordinates": self.coordinates,
            "shots": self.shots,
            "qubits": self.qubits,
            "handles": self.handles,
            "metadata": self.metadata,
            "provenance": self.provenance,
            "qubit_snapshots": self.qubit_snapshots,
            "execution_order": self.execution_order,
            "labels": self.labels,
            "assets": self.assets,
        }
        arrays = {}
        for i, handle in enumerate(self.handles):
            arrays[f"data_{i}"] = self.data[handle]
            arrays[f"status_{i}"] = self.status[handle]
        np.savez_compressed(directory / "observations.npz", **arrays)
        (directory / "results.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        return directory

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        payload = json.loads((directory / "results.json").read_text(encoding="utf-8"))
        if payload.pop("schema_version", None) != 1:
            raise ValueError("Unsupported result schema version")
        extra = {k: payload.pop(k) for k in ("execution_order", "labels", "assets")}
        result = cls(**payload)
        with np.load(directory / "observations.npz", allow_pickle=False) as arrays:
            for i, handle in enumerate(result.handles):
                data, status = arrays[f"data_{i}"], arrays[f"status_{i}"]
                if (
                    data.shape != result.data[handle].shape
                    or status.shape != data.shape
                ):
                    raise ValueError(
                        "Saved result array dimensions disagree with metadata"
                    )
                if (
                    data.dtype.kind not in "fiu"
                    or status.dtype.kind != "U"
                    or not set(np.unique(status)) <= STATUSES
                ):
                    raise ValueError("Invalid result array type or status")
                if np.any(np.isinf(data)):
                    raise ValueError("Infinite result observations are invalid")
                if np.any((status != "valid") & np.isfinite(data)):
                    raise ValueError(
                        "Invalid results cannot contain finite numeric observations"
                    )
                result.data[handle] = data.astype(float)
                result.status[handle] = status.astype("<U16")
        result.execution_order = [tuple(pair) for pair in extra["execution_order"]]
        result.labels, result.assets = extra["labels"], extra["assets"]
        for handle in result.handles:
            for idx in zip(
                *np.where(
                    (result.status[handle] == "valid") & np.isnan(result.data[handle])
                )
            ):
                if result._key(handle, tuple(int(i) for i in idx)) not in result.assets:
                    raise ValueError(
                        "A valid observation needs numeric data or an asset reference"
                    )
        return result


def _probability(model, coordinates, q, parameters):
    x = next(iter(coordinates.values()), 0.0)
    if model == "rabi":
        t = coordinates.get("duration", x)
        pi_duration = parameters.get("pi_duration", q.params.raman.pi_duration)
        if pi_duration <= 0:
            raise ValueError("pi_duration must be positive")
        probability = (
            parameters.get("offset", 0.02)
            + parameters.get("contrast", 0.96)
            * np.sin(np.pi * t / (2 * pi_duration)) ** 2
        )
    elif model == "ramsey":
        tau = coordinates.get("tau", x)
        t2 = parameters.get("t2star", 100e-6)
        if t2 <= 0:
            raise ValueError("t2star must be positive")
        probability = parameters.get("offset", 0.5) + parameters.get(
            "contrast", 0.45
        ) * np.exp(-tau / t2) * np.cos(
            2 * np.pi * parameters.get("detuning", 50e3) * tau
            + parameters.get("phase", 0.0)
        )
    elif model == "rb":
        length = coordinates.get("length", x)
        p = parameters.get("p", 0.99)
        if not 0 <= p <= 1:
            raise ValueError("RB p must lie in [0, 1]")
        probability = parameters.get("amplitude", 0.45) * p**length + parameters.get(
            "offset", 0.5
        )
    else:
        raise ValueError(f"Unknown synthetic model {model!r}")
    if not np.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("Synthetic model produced a probability outside [0, 1]")
    return float(probability)


def synthesize(compiled, *, model=None, seed=0, model_parameters=None, max_shots=None):
    """Generate synthetic classified observations, never real hardware data.

    Randomness is keyed by coordinate, shot, handle and qubit, so averaging
    order does not change synthetic observations. ``max_shots`` bounds executed
    point/shot pairs and leaves remaining observations explicitly unexecuted.
    """
    model = model or compiled.metadata.get("experiment_type", "rabi")
    if model not in {"rabi", "ramsey", "rb"}:
        raise ValueError(f"Unsupported synthetic model {model!r}")
    parameters = dict(model_parameters or {})
    defaults = {
        "rabi": {"offset": 0.02, "contrast": 0.96},
        "ramsey": {"offset": 0.5, "contrast": 0.45, "t2star": 100e-6, "phase": 0.0},
        "rb": {"amplitude": 0.45, "offset": 0.5, "p": 0.99},
    }
    for name, value in defaults[model].items():
        parameters.setdefault(name, value)
    if model == "ramsey":
        parameters.setdefault("detuning", compiled.metadata.get("detuning", 50e3))
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if max_shots is not None and (
        isinstance(max_shots, bool) or not isinstance(max_shots, int) or max_shots < 0
    ):
        raise ValueError("max_shots must be a non-negative integer")
    for name in (
        "missing_probability",
        "loss_probability",
        "loading_failure_probability",
    ):
        if not 0 <= parameters.get(name, 0.0) <= 1:
            raise ValueError(f"{name} must lie in [0, 1]")
    handles = sorted(
        {
            e.params["handle"]
            for p in compiled.points
            for e in p.logical_events
            if e.kind == "measure"
        }
    )
    result = ResultSet(
        axes=compiled.axes,
        coordinates=[p.coordinates for p in compiled.points],
        shots=compiled.shots,
        qubits=tuple(compiled.qubits),
        handles=handles,
        qubit_snapshots={uid: q.to_dict() for uid, q in compiled.qubits.items()},
        metadata={
            "synthetic": True,
            "model": model,
            "seed": seed,
            "model_parameters": parameters,
            "experiment_name": compiled.name,
            "experiment_metadata": _jsonable(compiled.metadata),
            "averaging_mode": compiled.averaging_mode,
            "validation_scope": (
                "data and analysis workflow only; not physical performance"
            ),
        },
        provenance=compiled.provenance,
    )
    for execution_index, (point_index, shot_index) in enumerate(
        compiled.execution_order()
    ):
        if max_shots is not None and execution_index >= max_shots:
            break
        point = compiled.points[point_index]
        result.execution_order.append((point_index, shot_index))
        seen = set()
        for event in point.logical_events:
            if event.kind != "measure":
                continue
            handle = event.params["handle"]
            uid = event.params.get("qubit", event.signal.rsplit(".", 1)[0])
            if (handle, uid) in seen:
                raise ValueError(
                    "Multiple measurements of the same handle/qubit in one shot "
                    "require distinct handles"
                )
            seen.add((handle, uid))
            key = json.dumps(
                [seed, point_index, point.coordinates, shot_index, handle, uid],
                sort_keys=True,
            )
            entropy = int.from_bytes(sha256(key.encode()).digest()[:16], "little")
            rng = np.random.default_rng(entropy)
            labels = {"loaded": True, "atom_present": True}
            if rng.random() < parameters.get("loading_failure_probability", 0):
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    status="loading_failed",
                    labels={"loaded": False},
                )
            elif rng.random() < parameters.get("loss_probability", 0):
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    status="atom_lost",
                    labels={"loaded": True, "atom_present": False},
                )
            elif rng.random() < parameters.get("missing_probability", 0):
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    status="missing",
                    labels=labels,
                )
            else:
                probability = _probability(
                    model, point.coordinates, compiled.qubits[uid], parameters
                )
                result.record(
                    handle,
                    point_index,
                    shot_index,
                    uid,
                    float(rng.random() < probability),
                    labels=labels,
                )
    return result
