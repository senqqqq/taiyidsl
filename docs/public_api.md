# Public API and extension points

Import the experiment-authoring API from `taiyidsl`. Importing the library works offline without ARTIQ or the Spectrum SDK. The [source map](source_layout.md) identifies owners for maintainers.

| Task | Interface | Guide |
| --- | --- | --- |
| Define an experiment | `Experiment`, `Square`, `FlatTopGaussian`, units, `play`, `delay`, `trig`, `acquire` | [Authoring](experiment_authoring.md) |
| Use function-style scopes | `experiment`, `average`, `sweep`, `realtime`, `triggered_sequence` | [Authoring and triggers](authoring_and_triggers.md) |
| Set physical parameters and wiring | `Qubit`, `Position`, `DeviceSetup`, `LabConfig`, `OpticsConfig` | [Descriptors](device_setup_descriptor.md) |
| Reuse a sequence | `Section`, `section`, `add`, `layer`, `barrier`, `qubit_operation`, `QubitOperations`, `PairCalibration` | [Sections and operations](sections_and_operations.md) |
| Reuse an experiment or RB sequence | `rabi`, `ramsey`, `randomized_benchmarking`, `ramsey_experiment`, `generate_rb_sequences`, `rb_sequence_section` | [Randomized benchmarking](randomized_benchmarking.md) |
| Incorporate an existing ARTIQ kernel | `NativeModule`, `NativeStage`, `NativeProgram`, `NativeKernelPackage` | [Native components](native_components.md) |
| Read observations | `ResultSet`, `ImageResults` and the protocols in `taiyidsl.runtime.readout` | [Results](results_and_imaging.md) and [adapters](readout_adapters.md) |
| Compile or run a large scan | `compile_stream`, `CompilationStore`, `StreamingRunner`, `RunStore`, `ShotContext`, `ObservationCommit` | [Storage and recovery](streamed_execution.md) |
| Move atoms | `IZ`, `SiteRef`, `MoveProfile`, `TransportConfig`, `move` | [DDS transport](transport_dds.md) |
| Implement a backend | `Backend`, `BackendCapabilities`, `register_backend`, `get_backend` | [Backend contracts](backends.md) |
| Integrate Spectrum hardware | `SpectrumAdapter`, `SpectrumRunner` and `SpcmAdapter` in `taiyidsl.runtime.spectrum.awg` and `SpcmDDSAdapter` in `taiyidsl.runtime.spectrum.dds` | [Spectrum](spectrum_hybrid.md) |

`lab.compile(exp)` returns an artifact with inspection and export methods such as `export()`, `export_report()`, `visualize()` and `waveform_simulator()`. The lower-level entry point is `compile_experiment(exp, setup, mapping, ...)`. `CompiledExperiment.export_artiq()` supports an artifact compiled for ARTIQ. To change targets, compile the experiment again. Establish a connection with `lab.connect()`. A session's execution receipt records what ran. Detector retrieval follows the relevant runner's readout contract.

## Migrating from the laboratory preview

The pre-collaboration snapshot used flat forwarding modules. Update external imports when moving to the collaboration baseline. Public names such as `from taiyidsl import Section, ramsey, reference_setup` retain their owners and meanings. `ramsey` builds the Raman template. `ramsey_experiment` builds the RF Ramsey experiment with native preparation/readout.

| Former module or symbol | Current owner |
| --- | --- |
| `taiyidsl.sections.Section` and flat authoring/operation imports | The top-level authoring API or `taiyidsl.dsl.sections`, `.authoring` and `.operations` |
| Flat hardware, descriptor and parameter modules | `taiyidsl.config` submodules. Reference setup factories live in `taiyidsl.lab` |
| Flat scheduling, chunking, streaming and store helpers | `taiyidsl.compiler` submodules. Host shot-order queries live in `taiyidsl.host_plan` |
| ARTIQ and Spectrum waveform, export and validation helpers | The corresponding `taiyidsl.backends.artiq` or `taiyidsl.backends.artiq_spectrum` submodule. For example, `awg.reconstruct_window` replays samples and `validation_awg.validate_program` checks them |
| Flat native, session, reporting or analysis helpers | `taiyidsl.native`, `taiyidsl.runtime`, `taiyidsl.reporting` or `taiyidsl.results` |
| Flat Ramsey, cooling, templates and RB modules | `taiyidsl.experiments.ramsey`, `.cooling`, `.templates`, `.rb_sequences` and `.rb_sections` |

`Protocol` and `ProtocolStep` still describe the existing trigger protocol alongside `TriggerSequence` and `TriggerStep`. The older Qubit and QubitStore parameter representations retain their read and version semantics. A laboratory extension should use declarations, configuration factories and explicit compile/execute contracts. Treat the source `Node` tree, private audit helpers, export templates and viewer payloads as implementation details. `ScheduledEvent` and `PhysicalPlan` carry target ticks and resource semantics. `compiled.ir` is an inspection record. Regenerate bank, DDS and journal artifacts from their inputs to preserve integrity checks.
