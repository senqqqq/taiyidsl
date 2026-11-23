"""Shot-resolved observations and context snapshots, with no execution policy."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..host_plan import execution_plan_for
from .archives import _atomic_hdf5, _jsonable

STATUSES = frozenset(
    {"valid", "missing", "failed", "unexecuted", "atom_lost", "loading_failed"}
)


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
        context=None,
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
        # A serialized snapshot prevents later mutation of input or returned data.
        self._context_json = json.dumps(
            _jsonable(context or {}), ensure_ascii=False, allow_nan=False
        )
        self.execution_order: list[tuple[int, int]] = []
        self.labels: dict[str, dict] = {}
        self.assets: dict[str, str] = {}

    @property
    def context(self):
        """Detached configuration and program snapshots, never live objects."""
        return json.loads(self._context_json)

    @classmethod
    def from_compiled(cls, compiled, *, metadata=None, handles=None):
        """Create empty observations and capture the complete compiled context.

        Serialized experiment IR and compiled events are preserved, not pickled
        Python executables. Lightweight synthetic fixtures lacking ``to_dict``
        are accepted but explicitly marked as having incomplete context.
        """
        from ..lab import LabConfig

        if hasattr(compiled, "assert_integrity"):
            compiled.assert_integrity()
        complete = hasattr(compiled, "to_dict")
        snapshot = compiled.to_dict() if complete else None
        qubits = {uid: q.to_dict() for uid, q in compiled.qubits.items()}
        parameters = snapshot["parameters"] if complete else {}
        context = {
            "complete": complete,
            "lab_config": (
                LabConfig(
                    compiled.setup,
                    compiled.signal_map,
                    backend=compiled.provenance.get("backend", {}).get("name", "artiq"),
                ).to_dict()
                if complete
                else None
            ),
            "parameters": parameters,
            "optics_config": parameters
            if parameters.get("schema_version") == 2
            else None,
            "qubits": qubits,
            "experiment": {
                "name": compiled.name,
                "metadata": _jsonable(compiled.metadata),
                "parameters": parameters,
                "qubits": qubits,
                "ir": snapshot["ir"] if complete else None,
            },
            "compiled_experiment": snapshot,
        }
        if handles is None:
            handles = sorted(
                {
                    event.params["handle"]
                    for point in compiled.points
                    for event in point.logical_events
                    if event.kind in {"measure", "acquire"}
                }
            )
        return cls(
            axes=compiled.axes,
            coordinates=[point.coordinates for point in compiled.points],
            shots=compiled.shots,
            qubits=tuple(compiled.qubits),
            handles=handles,
            metadata={
                "experiment_name": compiled.name,
                "experiment_metadata": _jsonable(compiled.metadata),
                "averaging_mode": compiled.averaging_mode,
                "host_execution_plan": _jsonable(execution_plan_for(compiled)),
                **(metadata or {}),
            },
            provenance=compiled.provenance,
            qubit_snapshots=qubits,
            context=context,
        )

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

    def _payload(self):
        return {
            "schema_version": 2,
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
            "context": self.context,
        }

    def to_xarray(self):
        """Return an independent named-array view; sweep coordinates use SI.

        ``point`` remains an index so repeated or irregular coordinates are not
        silently merged. Select points with ``isel`` or ``where`` on coordinates.
        """
        import xarray as xr

        dims = ("handle", "point", "shot", "qubit")
        value = np.stack([self.data[h] for h in self.handles])
        status = np.stack([self.status[h] for h in self.handles])
        ds = xr.Dataset(
            {
                "value": (dims, value),
                "status": (dims, status),
                "valid": (dims, (status == "valid") & np.isfinite(value)),
            },
            coords={
                "handle": list(self.handles),
                "point": np.arange(len(self.coordinates)),
                "shot": np.arange(self.shots),
                "qubit": list(self.qubits),
            },
            attrs={
                "metadata_json": json.dumps(self.metadata, ensure_ascii=False),
                "context_json": self._context_json,
                "provenance_json": json.dumps(self.provenance, ensure_ascii=False),
            },
        )
        for axis in self.axes:
            for parameter in (axis, *axis.get("paired_parameters", ())):
                name = parameter["name"]
                if name in ds:
                    raise ValueError(
                        f"Sweep axis {name!r} conflicts with a result dimension"
                    )
                ds = ds.assign_coords(
                    {name: ("point", [c[name] for c in self.coordinates])}
                )
                ds[name].attrs["units"] = parameter.get(
                    "values_unit", parameter.get("unit", "")
                )
        return ds

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        payload = self._payload()
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
        with np.load(directory / "observations.npz", allow_pickle=False) as arrays:
            return cls._restore(payload, arrays)

    @classmethod
    def _restore(cls, payload, arrays):
        if payload.pop("schema_version", None) not in {1, 2}:
            raise ValueError("Unsupported result schema version")
        extra = {k: payload.pop(k) for k in ("execution_order", "labels", "assets")}
        result = cls(**payload)
        for i, handle in enumerate(result.handles):
            data, status = arrays[f"data_{i}"], arrays[f"status_{i}"]
            if data.shape != result.data[handle].shape or status.shape != data.shape:
                raise ValueError("Saved result array dimensions disagree with metadata")
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

    def _write_hdf5(self, archive):
        import h5py

        archive.attrs["format"] = "taiyidsl-results"
        archive.create_dataset(
            "manifest",
            data=json.dumps(self._payload(), ensure_ascii=False, allow_nan=False),
        )
        observations = archive.create_group("observations")
        for i, handle in enumerate(self.handles):
            group = observations.create_group(str(i))
            group.attrs["handle"] = handle
            group.create_dataset("data", data=self.data[handle], compression="gzip")
            group.create_dataset(
                "status",
                data=self.status[handle].astype(object),
                dtype=h5py.string_dtype("utf-8"),
                compression="gzip",
            )

    def save_hdf5(self, path):
        """Atomically save observations and complete context in one HDF5 file."""
        with _atomic_hdf5(path) as archive:
            self._write_hdf5(archive)
        return Path(path)

    @classmethod
    def _read_hdf5(cls, archive):
        if archive.attrs.get("format") != "taiyidsl-results":
            raise ValueError("Not a TaiyiDSL result archive")
        payload = json.loads(archive["manifest"].asstr()[()])
        arrays = {}
        for i, handle in enumerate(payload["handles"]):
            group = archive[f"observations/{i}"]
            if group.attrs.get("handle") != handle:
                raise ValueError("Observation handle does not match manifest")
            arrays[f"data_{i}"] = group["data"][()]
            arrays[f"status_{i}"] = group["status"].asstr()[()].astype(str)
        return cls._restore(payload, arrays)

    @classmethod
    def load_hdf5(cls, path):
        import h5py

        with h5py.File(path, "r") as archive:
            return cls._read_hdf5(archive)
