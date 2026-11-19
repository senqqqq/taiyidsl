# Developing TaiyiDSL

An experiment should read in physical order. Start with the [plain Ramsey example](tutorials/ramsey_clock_plain.ipynb), then use [the operations version](tutorials/ramsey_clock_walkthrough.ipynb) to see where reusable gates help. The author first sees editable device databases and a wiring descriptor, then the lab configuration, qubit parameters, native ARTIQ routines, experiment, compiler and viewer. The final connection step is explicit. Default tutorial execution stays offline.

## Set up a checkout

Run `uv sync --locked` for core development or add `--group notebooks` to work on tutorials. The default environment needs no hardware SDK or ARTIQ installation. Install ARTIQ in a [separate environment](artiq_toolchain.md). See [CONTRIBUTING](../CONTRIBUTING.md) for Node, CI and supported checks. The [tools index](../scripts/README.md) lists generators, audit scripts and release tools.

## Choose an extension point

| What you want to change | Where to start |
| --- | --- |
| Define an experiment or a scan | An ordinary function accepting qubits, named pulses and explicit control values and returning an `Experiment`. See [authoring](experiment_authoring.md). |
| Reuse a gate or short timing block | `Section`, `qubit_operation` and `QubitOperations`. An operation invoked in an active builder inserts its Section. See [composition](sections_and_operations.md). |
| Implement MOT, cooling or preparation | Put laboratory-specific kernels in a separate package. Declare source, timing, resources and lifecycle with `NativeModule` and `NativeProgram`. See [native components](native_components.md). |
| Add camera or scalar observations | A host result adapter handles retrieval and commit. Emit the trigger explicitly. Use `ImageResults` and `ResultSet` for [imaging and analysis](results_and_imaging.md). |
| Add AOD trajectories | `MoveProfile` and `TransportConfig` declare hardware constraints. `move` records the request. See [DDS transport](transport_dds.md). |
| Add a hardware target | Implement the [backend contract](backends.md). For a new runtime device, integrate at the adapter boundary. |
| Add a tutorial | Keep the generator, unexecuted notebook and source inputs together. See the [tutorial maintenance index](tutorials/README.md). |

Standard library features belong in the shared package. Keep physical wiring, real calibrations, device addresses, detector SDKs and specialized operations in a laboratory-owned package. The [standalone lab example](../examples/lab_extension/README.md) shows how to use explicit dependency injection and run `uv run --no-sync python -m my_lab.demo` after installing the example.

## Experiment and configuration changes

Name pulses before the experiment body. Pass control quantities as parameters and keep compilation, visualization and connection outside the experiment function. A decorated experiment returns a whole experiment. A qubit operation returns a Section. Write the detuning in one place to avoid applying the same frequency offset again as a phase correction. Updates to a qubit's role parameters affect subsequently constructed experiments. Rebuild a pulse if its amplitude or duration captured the old calibration.

Devices, signal mappings and optical working points are configuration. Repetition, order and choice of readout belong to the experiment. Role-parameter views validate edits. A parameter snapshot detaches mutable maps and sequences. Preserve the different encodings used for configuration JSON, source IR and final artifacts, including numeric fractions and exact numerator/denominator representations.

## Native components and failure handling

Separate host declarations from kernel implementation. Only the kernel imports `artiq.experiment`. `NativeModule.from_package` discovers static local dependencies from a package root and `module:Class` entry. Pin the source and check it again at export. Dynamic imports and external dependencies must be declared or installed in the appropriate environment. Participants declare shared devices/buses and one owner for initialization. Build keywords describe fixed host configuration; stage parameters may bind scan values.

Place native work in the supported prefix, timed or suffix positions. Measure the cursor returned by a timed component against the declared duration. Session initialization/finalization and per-shot cleanup have different lifetimes. The GreenMOT example uses the legacy package contract and a core reset in initialization. Recheck those semantics before adapting an existing component.

Arm a detector before emitting its trigger. Fetch the matching frame and save its asset before committing the observation. Associate data with point, shot and attempt. A failed acquisition and a valid empty site have different meanings. Regular exported programs use host `before_shot(point, shot)`, `after_shot(point, shot)` and `finish()`. The persistent runner can pass a `ShotContext` and `RunStore`. Recovery retrieves an existing observation for its original attempt. `HardwareSession.run()` currently has no detector injection interface and rejects acquisitions. See the [readout contracts](readout_adapters.md).

| Owner | Resource and cleanup |
| --- | --- |
| `HardwareSession` | Worker process, pipes, reader thread and log. Communication failure invalidates the session. Close attempts each resource and tolerates repeated calls. |
| Worker | DeviceManager and adapters it creates, including partially initialized connections. Module imports added by one load are removed when that load fails. |
| `StreamingRunner` | Current chunk, native session context and temporary cache changes. Restore the cache and finish the session on exit. The caller owns any supplied manager, adapter and store. |
| Camera adapter owner | Camera connection, buffers and persistence directory. Use a context manager or finally block. `finish()` indicates successful completion. |
| `RunStore` | SQLite connection, execution lock, journal and observation commits. Validate assets before marking a shot committed. |

Cleanup runs all applicable actions. Preserve the primary exception, attaching cleanup failures as notes. See [persistent execution](streamed_execution.md) for failure boundaries and recovery.

## Compiler, backend and report changes

The compiler prepares once, then schedules a point, checks the acquisition signature, lowers to the target and binds native values. The same sequence serves in-memory and disk compilation. A point sink controls storage. Input validation still applies to references in inactive branches. Only complete stores can run. Section placement queries `section_grid()`, `signal_grid()`, `signal_frame()`, `frame_owner()` and `time_origin` on the scheduler.

Check event order, logical and physical times, preparation windows, phase state and samples when changing lowering. Keep the generator independent of its verifier. A backend version identifies its semantic contract. The [source map](source_layout.md) identifies owners for actual imports. Preserve the user's source locations, deployment independence of generated kernels and explicit configuration boundaries.

Changes to reports should preserve actual compiled data. Use the final DAC bytes for AWG and predicted commands for DDS. Reusable seeded Clifford and recovery mathematics belongs in `experiments` and remains independent of the DSL.

## Validate a contribution

Choose an assertion that would detect the change. Check prose and local links for documentation work. Run affected behavior tests and Ruff on changed Python files. Regenerate and execute affected notebooks in a fresh kernel after editing a generator or its inputs. When documenting an integration checkpoint, include the tested source version and the exact checks performed. For native changes, use the real offline compiler for affected generated calls when relevant. Distinguish static HTML, browser behavior, offline simulation, real compilation and device measurements. See [CONTRIBUTING](../CONTRIBUTING.md).
