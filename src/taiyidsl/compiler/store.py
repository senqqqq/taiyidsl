"""Read and verify immutable disk compilation artifacts without executing them."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np

from .._artifact_io import canonical_bytes, file_sha256
from ..host_plan import execution_order
from ..ir import CompiledPoint, CompileError, ScheduledEvent
from ..sample_assets import point_programs, restore_program_assets


def restore_point(value, sample_assets=None):
    result = dict(value)
    if sample_assets:
        result["physical_events"] = copy.deepcopy(value["physical_events"])
        restore_program_assets(point_programs(result), sample_assets)
    for key in ("logical_events", "physical_events"):
        result[key] = [
            ScheduledEvent(
                **{
                    **event,
                    "resources": tuple(event.get("resources", ())),
                    "logical_ids": tuple(event.get("logical_ids", ())),
                }
            )
            for event in result[key]
        ]
    result["axis_indices"] = tuple(result["axis_indices"])
    return CompiledPoint(**result)


def restore_compiled(value, profile):
    from ..config.models import Qubit
    from ..lab import LabConfig
    from .artifact import CompiledExperiment

    lab = LabConfig.from_dict(profile)
    # Scheduled ticks refer to the compiled timebase, including the Spectrum
    # backend's finer clock when the author supplied a native lab profile.
    lab.setup.tick_seconds = value["setup"]["tick_seconds"]
    assets = copy.deepcopy(value.get("sample_assets"))
    if assets:
        lab.setup._sample_assets = assets
    return CompiledExperiment(
        value["name"],
        [restore_point(p, assets) for p in value["points"]],
        value["shots"],
        value["averaging_mode"],
        value["axes"],
        {
            key: Qubit.from_dict(item).snapshot()
            for key, item in value["qubits"].items()
        },
        lab.setup,
        lab.signal_map,
        value["metadata"],
        value["provenance"],
        value["diagnostics"],
        value["ir"],
        parameters=value.get("parameters", {}),
        host_execution_plan=value["host_execution_plan"],
    )


class ArtifactSegments:
    """Read-only runtime loader for already compiled, content-addressed samples."""

    def __init__(self, store):
        self.store = store

    def load(self, program, segment_id):
        segment = next(s for s in program["segments"] if s["id"] == segment_id)
        digest = segment["sha256"]
        entry = self.store.manifest.get("segments", {}).get(digest)
        if entry is None:
            raise CompileError("Compiled sample is missing from the artifact store")
        path = self.store.check_file(entry["path"])
        shape = (segment["sample_count"], len(program["card_spec"]["active_channels"]))
        if entry["byte_length"] != shape[0] * shape[1] * 2:
            raise CompileError("Compiled sample shape differs from its entry")
        return np.fromfile(path, dtype="<i2").reshape(shape)


class CompilationStore:
    """An immutable complete disk artifact; it deliberately has no points list."""

    def __init__(self, directory, manifest):
        self.directory = directory
        self.manifest = manifest
        self.sha256 = manifest["sha256"]
        self.host_execution_plan = manifest["host_execution_plan"]

    @classmethod
    def open(cls, directory):
        directory = Path(directory).resolve()
        path = directory / "store.json"
        if not path.is_file():
            raise CompileError("Compilation store is incomplete; no completed index")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        expected = hashlib.sha256(
            canonical_bytes({k: v for k, v in manifest.items() if k != "sha256"})
        ).hexdigest()
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != "taiyidsl_compilation_store"
            or manifest.get("status") != "complete"
            or manifest.get("sha256") != expected
        ):
            raise CompileError("Compilation store index is invalid")
        result = cls(directory, manifest)
        result.verify()
        return result

    def verify(self):
        expected = hashlib.sha256(
            canonical_bytes({k: v for k, v in self.manifest.items() if k != "sha256"})
        ).hexdigest()
        if expected != self.sha256 or self.manifest.get("sha256") != self.sha256:
            raise CompileError("Compilation store index changed after opening")
        for relative in self.manifest["files"]:
            self.check_file(relative)

    def check_file(self, relative):
        path = (self.directory / relative).resolve()
        if (
            not path.is_relative_to(self.directory)
            or not path.is_file()
            or file_sha256(path) != self.manifest["files"].get(relative)
        ):
            raise CompileError(f"Compilation store file changed or missing: {relative}")
        return path

    def execution_order(self):
        return execution_order(self.host_execution_plan)

    def chunk_for_point(self, point):
        for chunk in self.manifest["chunks"]:
            if point in chunk["points"]:
                return chunk
        raise IndexError("Point is outside compilation store")

    def load_chunk(self, index):
        if type(index) is not int or not 0 <= index < len(self.manifest["chunks"]):
            raise IndexError("Chunk is outside compilation store")
        chunk = self.manifest["chunks"][index]
        header = json.loads(self.check_file("header.json").read_text(encoding="utf-8"))
        path = self.check_file(chunk["directory"] + "/compiled.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        return restore_compiled(value, header["lab_profile"])

    def export_report(self, directory, *, waveform_mode="paged", captures=()):
        from ..reporting.paged_output import export_store_report

        if waveform_mode != "paged":
            raise ValueError("CompilationStore reports require waveform_mode='paged'")
        self.verify()
        return export_store_report(self, directory, captures=captures)
