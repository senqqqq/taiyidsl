# Detector adapters, images and durable observations

Native or DSL code schedules a camera TTL. A host adapter arms the camera, retrieves the matching frame and saves it. `ImageResults` integrates ROIs and classifies occupancy from saved frames.

| Integration | Callbacks | Point and return contract |
| --- | --- | --- |
| Generated host program, `ResultAdapter` | `before_shot(point, shot)`, `after_shot(point, shot)`, `finish()` | The adapter saves data itself. A separately executed chunk may have local point numbers. |
| `StreamingRunner`, `PersistentResultAdapter` | `after_shot(point, shot, *, context, run_store)` | Point and shot refer to the complete store. Return a JSON-serializable dict or an `ObservationCommit`. |
| Recovery, `RecoverableResultAdapter` | `recover_shot(point, shot, *, context, run_store)` | Return data or a receipt for the original attempt, or `None` if it cannot be found. |

Import these protocols from `taiyidsl.runtime.readout`. Implement the methods directly. The persistent runner's before-shot and finish hooks are optional. It supplies `context` and `run_store` when the callback explicitly names them. Existing two-argument callbacks remain valid. Use an outer context manager or `try/finally` to own camera resources; `finish()` runs after a normally completed loop. `HardwareSession.run()` currently has no detector adapter injection and rejects acquisitions.

## Frame and commit sequence

1. Arm in `before_shot` and identify the current run, point, shot and attempt. Use the declared TTL edge for the trigger. Its width does not set exposure or transfer time.
2. After hardware completion, retrieve the matching frame. An empty site is a valid classification of zero. For a missing frame keep the observation unavailable, with NaN or JSON null for occupancy.
3. Flush the raw frame to stable storage under its complete attempt identity. Call `run_store.commit_observation(context, values, assets=[raw_path])` and return the receipt. The journal records asset hashes and commits the observation. Small scalar observations can use a dict.
4. After an interrupted commit, the runner can recover an existing receipt. If hardware completed but data is missing, `recover_shot` can look up the original attempt. It must not arm, trigger or rerun a kernel. Unknown hardware status requires an explicit decision.

`ShotContext` holds run/session identity, execution index, point, shot and attempt. `ObservationCommit` and `RecoveryRequired` live with it in `taiyidsl.runtime.contracts`. `RunStore` owns the execution journal.

The [standalone lab example](../examples/lab_extension/README.md) includes a camera TTL declaration, a two-argument adapter, a persistent NPZ adapter and recovery for occupied, empty and missing observations. It uses an explicit result schema for native triggers. For DSL `acquire()` use `ImageResults.from_compiled(...)` to validate the handle-to-acquisition mapping. Match frames with detector identifiers or timestamps where required by the lab. See [imaging](results_and_imaging.md) and [streamed execution](streamed_execution.md).
