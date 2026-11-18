"""Offline disk-backed compilation with bounded retained point programs.

The managed budget covers retained records and a reserved sample workspace,
not process RSS, caller-owned DSL trees, or external compiler caches.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

from taiyidsl.backends.artiq_spectrum.awg import (
    RENDER_BYTES_PER_SAMPLE_CHANNEL,
    materialize_segment,
    render_work_budget,
)
from taiyidsl.backends.artiq_spectrum.banks import BankBudget

from .._artifact_io import atomic_json as atomic_json
from .._artifact_io import canonical_bytes as canonical_bytes
from .._artifact_io import file_sha256 as file_sha256
from ..ir import CompileError
from .store import ArtifactSegments as ArtifactSegments
from .store import CompilationStore as CompilationStore
from .store import restore_compiled as restore_compiled
from .store import restore_point as restore_point

DEFAULT_HOST_MEMORY_BUDGET = 256 * 1024**2


class _PointWriter:
    def __init__(self, directory, budget, sample_workspace):
        self.directory = directory
        self.budget = budget
        self.sample_workspace = sample_workspace
        self.metadata_budget = budget - sample_workspace
        self.segments = {}
        self.chunks = []
        self.current = []
        self.current_bytes = 0
        self.current_budget = BankBudget()
        self.unit = []
        self.unit_bytes = 0
        self.unit_budget = BankBudget()
        self.peak_managed_bytes = 0
        (directory / "points").mkdir()

    def prepare(self, plan):
        self.plan = copy.deepcopy(plan)
        self.auto = plan.get("chunk_policy") == "auto"
        self.width = plan.get("atomic_point_count", 1)
        self.ends = {chunk["points"][-1] for chunk in plan["chunks"]}

    def _seal(self, reason):
        if not self.current:
            return
        self.chunks.append(
            {
                "index": len(self.chunks),
                "points": self.current,
                "point_metadata_bytes": self.current_bytes,
                "managed_bytes": self.current_bytes + self.sample_workspace,
                "boundary_reason": reason,
            }
        )
        self.current, self.current_bytes, self.current_budget = [], 0, BankBudget()

    def add(self, point, native, diagnostics):
        record = {
            "point": asdict(point),
            "native": native,
            "diagnostics": copy.deepcopy(diagnostics),
        }
        data = canonical_bytes(record)
        size = len(data)
        if size > self.metadata_budget:
            raise CompileError(
                f"Point {point.index} exceeds managed host memory budget; "
                "a single shot cannot be split"
            )
        (self.directory / "points" / f"{point.index}.json").write_bytes(data)
        if not self.auto:
            self.current.append(point.index)
            self.current_bytes += size
            self.current_budget.add_point(point)
            self.current_budget.validate()
            if self.current_bytes > self.metadata_budget:
                raise CompileError(
                    "Explicit chunk exceeds managed host budget; use smaller chunks"
                )
            self.peak_managed_bytes = max(
                self.peak_managed_bytes, self.current_bytes + self.sample_workspace
            )
            if point.index in self.ends:
                self._seal("declared_boundary")
            return
        self.unit.append(point.index)
        self.unit_bytes += size
        self.unit_budget.add_point(point)
        if self.unit_bytes > self.metadata_budget:
            raise CompileError(
                "One auto axis value and its inner points exceed the host budget; "
                "choose an inner axis"
            )
        if len(self.unit) != self.width:
            return
        try:
            self.unit_budget.validate()
        except CompileError as exc:
            raise CompileError(
                "One auto axis value exceeds capacity; choose an inner axis: "
                + str(exc)
            ) from exc
        combined = self.current_budget.merged(self.unit_budget)
        reason = None
        try:
            combined.validate()
        except CompileError as exc:
            reason = str(exc)
        if self.current_bytes + self.unit_bytes > self.metadata_budget:
            reason = "managed_host_budget"
        if reason and self.current:
            self._seal(reason)
            combined = self.unit_budget
        self.current.extend(self.unit)
        self.current_bytes += self.unit_bytes
        self.current_budget = combined
        self.peak_managed_bytes = max(
            self.peak_managed_bytes, self.current_bytes + self.sample_workspace
        )
        self.unit, self.unit_bytes, self.unit_budget = [], 0, BankBudget()
        if point.index in self.ends:
            self._seal("outer_prefix_boundary")

    def finish(self, header):
        from ..lab import LabConfig
        from ._stream_initializer import write_union_initializer

        native_package = header.metadata.get("native_artiq", {}).get("package")
        if (
            native_package is not None
            and native_package.get("kind") != "native_program"
        ):
            raise CompileError(
                "compile_stream requires NativeProgram for shared native context; "
                "legacy NativeKernelPackage remains supported by compile()"
            )

        self._seal("end")
        if self.unit:
            raise CompileError("Incomplete atomic axis unit")
        plan = {
            **self.plan,
            "chunks": [
                {"index": chunk["index"], "points": chunk["points"]}
                for chunk in self.chunks
            ],
        }
        profile = LabConfig(
            header.setup, header.signal_map, header.provenance["backend"]["name"]
        ).to_dict()
        atomic_json(
            self.directory / "header.json",
            {
                "compiled": header.to_dict(),
                "lab_profile": profile,
            },
        )
        for chunk in self.chunks:
            points, native, diagnostics = [], [], []
            for local, global_point in enumerate(chunk["points"]):
                record = json.loads(
                    (self.directory / "points" / f"{global_point}.json").read_text(
                        encoding="utf-8"
                    )
                )
                point = restore_point(record["point"])
                point.index = local
                points.append(point)
                if record["native"] is not None:
                    native.append({**record["native"], "index": local})
                diagnostics.extend(record["diagnostics"])
            metadata = copy.deepcopy(header.metadata)
            metadata["stream_global_points"] = chunk["points"]
            if "native_artiq" in metadata:
                metadata["native_artiq"]["points"] = native
            chunk_plan = {
                **plan,
                "point_count": len(points),
                "chunks": [{"index": 0, "points": list(range(len(points)))}],
            }
            compiled = replace(
                header,
                points=points,
                metadata=metadata,
                diagnostics=diagnostics,
                host_execution_plan=chunk_plan,
            )
            directory = self.directory / "chunks" / str(chunk["index"])
            compiled.export(directory)
            self._store_segments(directory)
            chunk["directory"] = directory.relative_to(self.directory).as_posix()
            del compiled, points, native, diagnostics
        write_union_initializer(self.directory, self.chunks)
        # A complete manifest is published only after every chunk was exported
        # and its existing offline validators succeeded.
        files = {
            path.relative_to(self.directory).as_posix(): file_sha256(path)
            for path in self.directory.rglob("*")
            if path.is_file()
        }
        manifest = {
            "schema_version": 1,
            "kind": "taiyidsl_compilation_store",
            "status": "complete",
            "name": header.name,
            "host_execution_plan": plan,
            "chunks": self.chunks,
            "host_memory_budget_bytes": self.budget,
            "managed_peak_bytes": self.peak_managed_bytes,
            "sample_workspace_bytes": self.sample_workspace,
            "segments": self.segments,
            "memory_accounting": (
                "canonical_point_native_diagnostics_plus_reserved_sample_workspace"
            ),
            "files": files,
        }
        manifest["sha256"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        atomic_json(self.directory / "store.json", manifest)
        return CompilationStore.open(self.directory)

    def _store_segments(self, chunk_directory):
        """One immutable packed file per content hash across every chunk/card."""
        manifest = json.loads((chunk_directory / "manifest.json").read_text())
        directory = self.directory / "segments"
        directory.mkdir(exist_ok=True)
        for bank in manifest["banks"]:
            for group in bank["groups"]:
                for card in group["cards"].values():
                    program = card["program"]
                    if program.get("kind") == "dds_bank":
                        continue
                    for segment in program["segments"]:
                        digest = segment["sha256"]
                        if digest in self.segments:
                            continue
                        values = materialize_segment(program, segment["id"])
                        raw = memoryview(values).cast("B")
                        if hashlib.sha256(raw).hexdigest() != digest:
                            raise CompileError("Stored sample hash differs from bank")
                        path = directory / f"{digest}.bin"
                        temporary = path.with_suffix(".partial")
                        with temporary.open("wb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, path)
                        self.segments[digest] = {
                            "path": path.relative_to(self.directory).as_posix(),
                            "byte_length": len(raw),
                        }


def compile_stream(
    experiment,
    setup,
    signal_map,
    *,
    directory,
    host_memory_budget_bytes=DEFAULT_HOST_MEMORY_BUDGET,
    backend="artiq_spectrum",
    **options,
):
    """Compile and validate every chunk offline before publishing an artifact."""
    from .pipeline import compile_experiment

    if type(host_memory_budget_bytes) is not int or host_memory_budget_bytes < 1:
        raise ValueError("host_memory_budget_bytes must be a positive integer")
    name = backend if isinstance(backend, str) else backend.name
    if name != "artiq_spectrum":
        raise CompileError("compile_stream currently requires artiq_spectrum")
    setup = copy.deepcopy(setup)
    groups = setup.spectrum.groups
    cards = [card for group in groups.values() for card in group.cards.values()]
    width = max(len(card.active_channels) for card in cards)
    # Two minimum segments permit every aligned tail to be partitioned legally.
    minimum_tile = 2 * max(384 // len(card.active_channels) for card in cards)
    unit_bytes = width * RENDER_BYTES_PER_SAMPLE_CHANNEL
    minimum_workspace = max(64 * 1024, minimum_tile * unit_bytes)
    requested = max(group.max_tile_samples for group in groups.values()) * unit_bytes
    workspace = min(requested, max(minimum_workspace, host_memory_budget_bytes // 2))
    if workspace >= host_memory_budget_bytes:
        raise CompileError(
            "Host budget cannot accommodate the minimum sample workspace"
        )
    tile_limit = workspace // unit_bytes // 32 * 32
    workspace = tile_limit * unit_bytes
    setup.spectrum = replace(
        setup.spectrum,
        groups={
            name: replace(
                group, max_tile_samples=min(group.max_tile_samples, tile_limit)
            )
            for name, group in groups.items()
        },
    )
    target = Path(directory).resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(
            "Use an empty compilation directory; "
            "existing artifacts are never overwritten"
        )
    target.mkdir(parents=True, exist_ok=True)
    writer = _PointWriter(target, host_memory_budget_bytes, workspace)
    with render_work_budget(workspace):
        header = compile_experiment(
            experiment,
            setup,
            signal_map,
            backend=backend,
            _point_sink=writer,
            **options,
        )
        return writer.finish(header)
