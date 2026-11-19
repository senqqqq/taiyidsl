# Design decisions

## Authoring and configuration

A Python experiment builds a source tree. Compilation snapshots the inputs, schedules each point and lowers it to a selected target. Export and inspection work offline. A hardware session begins with an explicit connection.

Pass qubits, named pulses, scan values and preparation/readout parameters to a function that returns an `Experiment`. Keep the sequence visible in the function. `LabConfig` and `DeviceSetup` own wiring, clocks, resources, the backend and device calibration. `OpticsConfig` holds shared optical operating points. `Qubit.parameters` holds calibration for a qubit role. Loops, execution order and the choice of readout belong in experiment code.

Users may edit qubit parameters. An Experiment, Section or compiled artifact retains its own snapshot. If a pulse captured an amplitude or duration, rebuild the pulse and experiment after updating the calibration. `QubitStore` provides optional versioned records. Configuration files use declared SI numbers. The DSL accepts quantities with units and validates dimensions at its boundary. The compiler checks the selected time grid, shared resources and device preparation windows.

The descriptor declares whether the RF, clock or Rydberg field is shared. A shared physical output emits one waveform. Registered qubits and wiring determine the affected targets. An explicit target list asserts that relationship. Separate qubit IDs and transport into an optical zone do not confer independent RF addressing.

## Time, resources and phase

- Serial and parallel branches, reusable Sections, layers and barriers share one source model. ASAP placement and local barriers respect both qubit occupancy and shared output resources. `dsl.sections` defines the structure. `compiler.section_schedule` places it.
- A sweep defines a static host axis. Repeat and case expand point by point during compilation. Average retains each shot and its explicit execution order. Measurement and analysis determine how values are aggregated.
- The persistent frame F gains delta after `shift_phase(delta)` and loses theta after `Rz(theta)`. `reset_frame` clears F. `reset_phase` also clears inherited modulation and establishes a carrier reference at its final scheduled position. A reset cannot interrupt an active pulse. These operations change the phase description of subsequent control.
- Spectrum `triggered_sequence` defaults to a phase reference at sequence entry. Select `phase_reference="shot"` for a common shot reference. Sections, operations, segments and chunks preserve the selected reference. Each shot starts with a fresh frame.
- Handle the logical timebase, native ARTIQ mu, AWG sample clock and DDS command grid explicitly. Hybrid compilation requires exact timing and rejects `allow_quantization=True`. A request that cannot fit the target grid or preparation window must fail with its cause. See [Spectrum phase modes](spectrum_phase_modes.md).

## Targets, native code and transport

Select `artiq` or `artiq_spectrum` explicitly. Backends are stateless. Include target configuration and backend version in the compiled snapshot. Change the backend version when its semantics change; hashes of artifact content do not inspect implementation files.

ARTIQ lowering, source generation and offline execution have separate owners. The exporter invokes the generator and verifier. Spectrum's AWG checker uses an independent mathematical expectation and finite replay of final samples. DDS checks rebuild command intent and verify program integrity. The shared calibration module supplies both analysis and runtime parsing.

A native program follows `prefix* → timed* → DSL control → suffix*`. Discover and audit external source without importing user ARTIQ modules in the DSL environment. A duration contract checks the actual returned cursor. Participants sharing a resource declare one initialization owner. `NativeProgram.initialize/finalize` run once per session and `after_shot` once per shot. The existing `NativeKernelPackage.finalize_method` runs once per shot. Put core reset in the root initialization when designing a component. Review legacy packages that reset the core before reusing their hooks.

Spectrum DDS drives the crossed X/Y AOD for transport. AWG cards provide qubit control. Compilation tracks position per executed branch and leaves the user's `q.position` intact. Each shot explicitly returns to the initial layout. DDS frequency and amplitude control use a separate reference from the AWG. After arrival the traps hold a nonzero output through shot completion, bank changes and return from a run. Closing the session stops output. Failure invalidates known position and prevents another run in that session. See [DDS transport](transport_dds.md).

## Results, recovery and evidence

A camera trigger, an acquisition declaration, frame retrieval and durable observation commit are separate actions. An empty site is a valid zero. A missing frame is missing data. Compute joint probabilities from observations belonging to the same shot.

`RunStore` distinguishes hardware completion from data commit. It associates assets with run, point, shot and attempt. The default response to unknown hardware state is to stop. Recovery retrieves data for the original attempt. `compile_stream` publishes a complete store after compiling points to disk. Its budget bounds managed workspace; input trees, RB descriptions and global indexes can still grow. Interrupted compilation starts again.

Reports show exported code, its source hashes and origins. AWG plots use final quantized DAC samples. DDS plots predict commands. Preserve unknown host/native durations as unknown. Report mock execution, compilation with the real ARTIQ compiler, browser inspection and physical acceptance separately. Recheck source identity before reusing compilation receipts.

Keep general capabilities in the library and specialized components in laboratory packages. The [public API](public_api.md), [source map](source_layout.md) and [developer guide](development.md) describe these boundaries.
