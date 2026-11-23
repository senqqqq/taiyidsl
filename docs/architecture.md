# Architecture and compilation contracts

TaiyiDSL shares an authoring language, configuration models and artifact format across the built-in `artiq` and `artiq_spectrum` targets. The compiler produces a target-specific plan. Generators and viewers consume that plan offline. Runtime entry points have different execution and data capabilities. See the [public API](public_api.md) and [source map](source_layout.md).

## From experiment to artifact

1. The author calls Python functions under an experiment builder. Instructions append nodes with caller locations. An exception rolls back the incomplete block.
2. Qubit and parameter snapshots detach the experiment from subsequent edits. `LabConfig` owns `DeviceSetup`, `SignalMap` and the selected backend. `OpticsConfig` holds shared optical values. Generic experiment parameters are independent of the optics schema.
3. The compiler checks structure and acquisition indices, resolves the finite Cartesian product of host axes, and invokes `schedule` and `lower` at each point. `Backend.schedule` uses the integer-grid scheduler by default. A target may supply its own scheduling policy.
4. The scheduler assigns logical ticks using declared grids and resources. `PhysicalPlan(events, origin_mu)` adds preparation before logical time zero. The lowering policy owns latency compensation, calibration, physical collisions and register quantization. Logical ticks refer to the selected timebase.
5. `CompiledExperiment` includes the source record, inputs, points, axes, diagnostics, shot order and target identity. Export from that snapshot. Switching targets requires compiling the original experiment again. A generic point sink can write points to disk without introducing a second scheduling algorithm.
6. Generators emit host and kernel source. The ARTIQ verifier executes generated source with mock drivers and compares digital events. Spectrum AWG validation independently computes expected samples and checks finite replay. The DDS validator reconstructs expected commands and checks program integrity. The report displays the published files and their hashes.

## Timing contracts

ARTIQ lowerers own DDS/TTL resources, preparation durations, AD9910 tuning words and tracking phase. Output timing and hardware submission time have separate constraints. Conventional square pulses need RF-off time to configure the DDS. Timed square pulses use prepare, commit and effect events, allowing preparation during earlier output when buffer and resource constraints permit. `origin_mu` reserves pre-roll. Explicit reservations constrain preparation as well as output.

Separate SPI buses can prepare concurrently, subject to shared update lines, channel busy intervals and actual submission capacity. The bounded scheduling retry may reorder configurations with the same output deadline. For example, if two preceding gates end at 4 and 6 µs and both new gates start at 7 µs, two 1 µs configurations can fit at 5–6 and 6–7 µs. A successful reorder records `preparation_reordered`. The algorithm can still reject complex interleavings. See [instruction timing](artiq_instruction_timing.md), [square timing](square_timing.md), [mixed paths](mixed_pulse_paths.md) and [RAM boundaries](ram_boundary_timing.md).

The shot reference for tracking is `t0 + origin_mu`. Frequency changes use the new frequency at that reference. Timed square commits can update phase during a long pulse. RAM boundary modes allow specific RF-off and gate-boundary updates. A single persistent drive frame implements positive `shift_phase` and negative `Rz`. `reset_frame` and `reset_phase` have distinct carrier semantics. Preserve diagnostics and the frame history when tracing a gate to its source. See [phase decisions](design_decisions.md) and [Spectrum phase modes](spectrum_phase_modes.md).

The compiler rejects a mismatch in `exact_duration`. Explicitly enabled ARTIQ quantization rounds to a supported grid and then reschedules and validates. External alignment stays explicit. Hybrid operation requires exact conversions across its own grid, ARTIQ mu, AWG sample clocks and DDS command clocks.

## Code generation and hardware integration

The conventional square generator can group matching event structures and pass point-specific arguments to a shared kernel. Timed square paths can produce per-point kernels. Long RAM sequences may use per-point event tables. Debug exports can expand the sequence for inspection. Compile-time repeat expansion and actual kernel loops are different mechanisms. Report source count and compiler receipts for the program actually exported.

Logical signals map through `SignalMap` to device names. The generated program calls `get_device(name)` on the deployment device database. The lab configuration's bus and update-group declarations describe compilation resources. Real device addresses, driver and gateware configuration, routing and calibration belong to deployment. Only touched outputs are initialized. A shared bus or update group with unused neighbors may require an explicit `prepared_shared_resources` declaration. Core reset requires exclusive access to the RTIO domain.

For hybrid control, ARTIQ handles its native subplan and triggers the declared Spectrum group. Simultaneous planned edges are a digital invariant; card synchronization and the optical phase at the atoms require measurements. DDS transport maintains site occupancy, an explicit return path and the terminal trapping hold. See [hybrid control](spectrum_hybrid.md) and [transport](transport_dds.md).

## Execution and observation boundaries

| Entry | Responsibility |
| --- | --- |
| `lab.compile`, export and report | Offline planning, source generation, verification and inspection. |
| `lab.connect()` / `HardwareSession` | Explicit session, resource ownership, program execution and receipts. This runner rejects acquisition requests requiring a detector adapter. |
| Generated host program with `result_adapter` | Call before-shot, after-shot and finish hooks. The adapter owns the transfer of observed data. |
| `StreamingRunner` with `RunStore` | Load chunks, preserve native lifecycle, journal attempts and commit associated observations. |

Native code discovery pins local sources without importing the kernel in the DSL environment. Components run in the supported prefix/timed/control/suffix positions. A session initializes and finalizes `NativeProgram` once, with per-shot `after_shot`. Legacy `NativeKernelPackage.finalize_method` runs per shot. Confirm the returned cursor against each duration contract. See [native integration](native_artiq_integration.md).

Result indices preserve acquisition handles and point/shot identity. Compare joint probabilities only among observations from the same shot. Separate hardware completion, successful data retrieval and durable commit. Keep measured data, synthetic results, program predictions and calibration proposals distinguishable. See [results and imaging](results_and_imaging.md).

Before commissioning, record ARTIQ/gateware/board revisions and the device database. Measure the reference period, DDS clock, SPI behavior, updates, switch and optical delays for the selected path. Compile the actual generated ARTIQ source, then check TTL, a single DDS channel, Ramsey and cross-channel alignment against analyzer traces and physical measurements. Driver references are available for [RTIO](https://m-labs.hk/artiq/manual/rtio.html), [AD9910](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html) and [Urukul](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html).
