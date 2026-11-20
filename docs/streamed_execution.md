# Disk compilation, shot journals and paged waveforms

`compile()` returns an in-memory `CompiledExperiment`. `compile_stream()` writes points to disk and loads one chunk at a time to export its programs, banks, manifests and offline checks. A complete, hash-checked `store.json` is published atomically at the end. Use an empty or new directory. An interrupted compile has no runnable index. Start a new compilation rather than reusing an incomplete store.

```python
from taiyidsl import CompilationStore

with exp.average(100, scope="chunk"):
    with exp.sweep(length, chunks="auto"):
        with exp.realtime():
            ...

store = lab.compile_stream(
    exp,
    directory="output/large_scan/compiled",
    host_memory_budget_bytes=256 * 1024**2,
)
store = CompilationStore.open("output/large_scan/compiled")
store.export_report("output/large_scan/viewer", waveform_mode="paged")
```

## Bounds and point identity

The disk-backed compiler currently supports `artiq_spectrum`. Opening a completed store loads its global point, chunk, hash and sample indexes. The input RB circuits and expanded case bodies, temporary device maps and shared initialization fragments also grow with experiment size. Include these allocations when estimating total process memory.

Automatic chunking marks one host sweep axis. Keep outer coordinates fixed and retain every inner combination for each value of the marked axis. Add adjacent values until the next one would exceed a per-card slot, segment, step or memory limit. Identical quantized segments share content hashes. Manual `chunks=N` and `chunk_size=N` retain their explicit boundaries. If an entire group exceeds its budget, choose an inner axis. A sweep split cannot subdivide a single coherent shot.

The default 256 MiB managed budget counts canonical per-point metadata bytes (logical and physical IR, native bindings and diagnostics) and a reserved sample workspace. The workspace allows 128 bytes per sample per channel for current NumPy generation and comparison buffers. Array pulses also require space for decoded source samples and temporary decoding buffers. The source interval may span many tiles. Tiles are bounded before allocation. A budget below the smallest legal tile is rejected. The index records `point_metadata_bytes`, `sample_workspace_bytes`, `managed_bytes`, `managed_peak_bytes` and `boundary_reason`. Process RSS can also include Python objects, the input tree, indexes and axis values, decoders and external compiler caches. `max_points` and per-point event limits still apply.

`store.load_chunk(i)` returns a compiled experiment with local point indices. Map them to the global points in `store.manifest["chunks"][i]["points"]`. `store.execution_order()` returns global point/shot pairs. See [chunk scheduling](chunked_execution.md) for cyclic, sequential and chunk-local averaging.

## Execute and commit

```python
from taiyidsl import RunStore, StreamingRunner

journal = RunStore("output/large_scan/run_001")
runner = StreamingRunner(spectrum_runner, managers=artiq_managers)
try:
    runner.run(store, result_adapter=detector, run_store=journal)
finally:
    journal.close()
```

Supply configured managers, Spectrum runner and detector. Before the first shot requiring execution, the runner preflights all chunks and initializes the union of required devices once. The root host object, managers and native context persist across chunk changes. Only the chunk program is replaced. Each bank retains its own entries. Published, checked sample assets can be loaded through read-only `ArtifactSegments`. Older stores without this index use the cache path. The caller retains ownership of supplied managers, detector and journal.

```python
def after_shot(self, point, shot, *, context, run_store):
    # The context identifies the run, session, execution index, point, shot and attempt.
    path, population = self.camera.collect_and_save(context)
    return run_store.commit_observation(
        context, {"population": population}, assets=[path]
    )
```

The journal binds one execution process to the compilation store's hash. SQLite WAL/FULL records preparation, entry into the kernel, hardware completion and observation commit separately. Flush raw assets, then atomically publish the observation with its context and file hashes. The runner validates the receipt before updating the journal. An interrupted journal commit can recover an observation already on disk. Preserve the primary error if cleanup also fails.

## Resume a run

`runner.resume(...)` verifies existing committed assets and skips completed or explicitly skipped work. If hardware completed and no observation has yet been published, the runner may ask the detector for data from the original attempt:

```python
def recover_shot(self, point, shot, *, context, run_store):
    existing = self.camera.find_saved_attempt(
        context.run_id, context.execution_index, context.attempt
    )
    if existing is None:
        return None
    return run_store.commit_observation(context, existing.values, assets=existing.files)
```

An unknown hardware outcome or an unrecoverable observation raises `RecoveryRequired` by default. After investigating, record a deliberate decision:

```python
journal.resolve(
    execution_index,
    "retry",
    reason="Detector confirmed that the previous frame is unusable",
)
# Use "skip" to record a missing observation explicitly.
```

Retry creates a new attempt, preserving the earlier record. Bulk `unresolved="retry"` or `"skip"` is an explicit recovery policy. Physical execution and a durable database update cannot be made one atomic transaction.

## Inspect and compare

Open the exported viewer directory's `index.html`. Chunk selection loads its metadata, and a waveform window loads the intersecting packed DAC segments. Local classic-script shards support offline viewing. The per-channel window limit is 100,000 samples with bounded decoding cache. Each chunk links to actual banks, entries, generated ARTIQ and its pulse sheet. Export verifies segment hashes. Nominal volts use the configured DAC range. Attach independent capture comparisons via `store.export_report(..., captures=[comparison_html])`. See [capture comparison](capture_comparison.md).
