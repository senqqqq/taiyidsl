# Backends and laboratory configuration

The experiment describes the intended operations. A backend maps them to a target schedule, physical events and offline programs. The built-in targets are `artiq` (ARTIQ 9, AD9910 and TTL) and `artiq_spectrum` (ARTIQ with Spectrum AWG/DDS). Backend construction, compilation and export are offline. Connecting instruments and executing a program require an explicit session or runner.

| Capability | Implementation and limits |
| --- | --- |
| AD9910 and TTL | `artiq`. Square and RAM paths have distinct timing and protocol constraints. |
| Multi-card AWG | `artiq_spectrum`. Synchronized group triggers and static banks. Physical synchronization requires measurement. |
| Crossed-AOD transport | Spectrum DDS transport on the hybrid backend. Dedicated transport instructions address the DDS outputs. |
| Native preparation and readout | `NativeKernelPackage` or `NativeProgram` at defined stage boundaries. Arbitrary interleaving between gates is unsupported. |
| Host execution | Explicit session or runner. The lab integrates its camera SDK and device configuration. |
| Real-time feedback and cross-shot coherence | Currently unsupported. |

See the [public API](public_api.md) and the [developer guide](development.md).

## Compile with a lab configuration

```python
from taiyidsl import Qubit, rabi, reference_lab, us

q0 = Qubit("q0")
lab = reference_lab([q0])
experiment = rabi(q0, [0 * us, 4 * us, 8 * us], shots=100)
compiled = lab.compile(experiment)
compiled.visualize("output/rabi.html")
compiled.export("output/rabi_artiq")
```

| `LabConfig` field | Responsibility |
| --- | --- |
| `setup` | Devices, clocks, protocols and shared calibration |
| `signal_map` | Bindings and transforms from logical signals to physical outputs |
| `backend` | Backend instance or registered name, default `"artiq"` |
| `qubits` | Registered signal identities and participants in shared outputs |
| `emulation`, `artiq_python` | Connection mode and separate ARTIQ interpreter |
| `transport` | AOD geometry, sites, device calibration and bindings |

`reference_lab(qubits)` provides one example board per qubit. `initialization_lab(qubit)` supplies example pumping, push-out and imaging triggers. Both factories in `src/taiyidsl/lab.py` return independent configurations. Labs should supply their own device names, budgets, protocols and calibration.

Compilation copies the setup, mapping and backend policy. Editing the lab configuration affects subsequent builds. Existing plans retain their snapshots. Options such as `lab.compile(..., allow_quantization=True)` go to the compiler. Select the backend on `lab.backend`.

```python
from taiyidsl import compile_experiment, reference_setup

setup, mapping = reference_setup([q0])
compiled = compile_experiment(experiment, setup, mapping)
compiled.export_artiq("output/rabi_artiq")
```

`reference_setup` retains the tuple-returning interface. `compiled.export(...)` dispatches to the backend that produced the plan. `compiled.export_artiq(...)` accepts an ARTIQ plan only. Switching targets requires recompilation from the source experiment.

## Representations and timing

```text
DSL / Node source tree
    ↓
compiler: experiment structure, sweep points, acquisition indices
    ↓
Backend.schedule: logical schedule
    ↓
Backend.lower: target physical schedule / PhysicalPlan
    ↓
Backend.export: offline target artifacts
```

The default `Backend.schedule` uses `compiler.scheduling.Scheduler` for serial and parallel placement, static repeat/case selection, software frames and protocol expansion. Times use `setup.tick_seconds`. The default `Backend.signal_grid(signal, setup, mapping)` returns the least common multiple of the mapped devices' grids. A target can override its grid policy or the entire scheduler.

| Representation | Contents | When changing targets |
| --- | --- | --- |
| DSL `Node` tree | Experiment structure and parameter expressions | Recompile it |
| `compiled.ir` | Inspectable JSON representation of the source tree | No public loader or compiler entry point accepts it |
| `logical_events` | Logical signals with times already on the target's tick and grids | Recompute the schedule for the new target |
| `physical_events` | Target device calls, resources and preparation actions | Regenerate them |

`ir.py` provides shared `ScheduledEvent`, `CompiledPoint` and `PhysicalPlan` containers. The meaning of the enclosed times and commands depends on the backend.

ARTIQ lowering in `backends/artiq/backend.py` and its device-path modules reserves DDS programming windows and models SPI, commit and shared-update resources. It quantizes AD9910 FTW/POW/ASF and applies calibrated latency and physical phase. `backends/artiq/codegen*.py` emits source. `mock_drivers.py` and `verification*.py` run or check the generated program independently. `export.py` coordinates publication. The hybrid implementation uses `backends/artiq_spectrum/backend.py` and `scheduling.py`, with `awg.py`, `dds.py`, `banks.py` and `export.py` for samples, commands, banks and exports. A new target can define its own device constraints.

## Dependencies within a target

- Lowering and code generation are independent of mock drivers and verification. A verifier may call the generator to obtain default source. The exporter coordinates both, and shared contract modules hold common record types.
- Internal consumers import the owning modules for sample or command data. `backends.artiq` and `backends.artiq_spectrum` expose backend components on demand.
- `backends.artiq_spectrum.validation_awg` calculates expected mathematical samples independently and compares them with finite playback. It uses the playback utility in `awg` to read the compiled data.
- `dds.validate_dds_program` reconstructs commands, constraints and hashes. This verifies DDS program consistency. An independent physical model or an instrument measurement answers a different question.
- Hardware connections, vendor SDKs and execution belong to `runtime`. Reports belong to `reporting`. Shared `spectrum_calibration` supports calibration analysis and runtime resolution, and `spectrum_identity` computes identity data.

See the [source layout](source_layout.md). Build, real-compiler and physical acceptance results belong to the validation record for the particular program.

## Implement another backend

Subclass `taiyidsl.Backend`. Give it a unique valid `name`. The built-in names `artiq` and `artiq_spectrum` are reserved. Maintain a semantic `version`, a `target` description and a `phase_policy` explaining how physical phase is realized. Declare supported devices, pulse shapes, operations and average/sweep execution locations through `BackendCapabilities`.

| Method | Contract |
| --- | --- |
| `validate_setup(setup, mapping)` | Check target clocks, device configuration and connectivity. The default performs no target-specific validation. |
| `signal_grid(signal, setup, mapping)` | Return a positive integer grid in target ticks. The default uses the LCM of mapped device grids. |
| `schedule(experiment, body, setup, mapping, coordinates, allow_quantization, diagnostics, max_events)` | Return `(logical_events, duration_mu)`. The default delegates to the shared scheduler. |
| `lower(logical_events, setup, mapping, diagnostics)` | Required. Return `PhysicalPlan(events, origin_mu)`. |
| `export(compiled, output_dir, **options)` | Required. Write offline target artifacts. Submission and execution are separate operations. |

`lower` owns target binding, composite output transforms, shared-resource conflicts, feasibility checks, calibration, configuration reservation, numeric quantization and phase realization. If a plan reserves preparation time before its logical beginning, `origin_mu` is the physical offset of the logical origin. Physical event times must follow that convention. Reject unsupported operations and infeasible timing without silently changing the requested pulse intervals.

The shared compiler validates the unique top-level average, nested sweep chain, point limits, and acquisition handle/qubit set at each point. A custom scheduler must meet these contracts, enforce its event budget and honor its timing policy. Capabilities select a subset of the existing DSL. For example, `pulse_shapes={"square"}` accepts square pulses only. New language constructs, execution positions or measurement feedback require their own syntax, implementation and result semantics.

Keep backend policy stateless. Store clock, latency, registers and sample rate in the setup. The common `Device` retains DDS fields for existing configurations. New targets can add fields using a frozen dataclass subclass. Fields must support deep copying and JSON serialization to enter setup snapshots and content hashes. Construction validates basic names and integer grids. Compilation checks target-specific rules such as device types, DDS frequency limits and programming grids.

Pass a backend instance directly:

```python
from taiyidsl import LabConfig
from taiyidsl.backends.artiq.backend import ArtiqBackend

lab = LabConfig(setup, mapping, backend=ArtiqBackend())
compiled = lab.compile(experiment)
```

Or register an implemented instance explicitly and select it by name:

```python
from taiyidsl import register_backend

register_backend(my_backend)
lab = LabConfig(setup, mapping, backend=my_backend.name)
```

Duplicate names raise an error. Plugin discovery and implicit imports are unsupported. The compiled artifact captures its backend and uses that snapshot on export. `tests/test_backends.py` illustrates a custom event type, a grid override and offline export. It provides an interface example without a hardware driver.

Backend name, version, target, capabilities and phase policy enter provenance and the content hash. Implementers must maintain the version when scheduling, quantization or export policies change. Hashing does not scan backend Python source or establish correctness on hardware.

## Physical wiring, readout and execution

```text
play("optics.pump_556", ...)
    → SignalMap["optics.pump_556"] == "urukul0_ch0"
    → DeviceSetup.devices["urukul0_ch0"]
    → get_device("urukul0_ch0") in generated ARTIQ source
    → laboratory device_db and physical device
```

`DeviceSetup` records the compilation contract. `bus` and `update_group` label resource relationships. The laboratory maintains the core address, complete RTIO and driver configuration, board wiring and optical paths. Device names must match the external `device_db`. Generated code checks selected clocks. Software checks cannot establish actual cabling or optical timing.

The shared scheduler expands a `Protocol` into trigger steps with offsets and durations. ARTIQ requires those steps to bind to TTL outputs. `measure` records acquisition intent. The lab handles detector loading, arming, transfer and result association through `result_adapter.before_shot`, `after_shot` and `finish` where supported.

`lab.connect()` creates a hardware session. `SpectrumRunner` executes configured cards. `StreamingRunner` and `RunStore` manage chunks and durable commits. Instruments must be explicitly configured. `HardwareSession` currently rejects compiled DSL acquisitions because it has no detector adapter. For result-adapter contracts on export and persistent execution see [Readout adapters](readout_adapters.md). Record ARTIQ mock equivalence, real offline compilation and measured hardware timing separately.

## Port an experiment to a new target

1. Keep experiment builders expressed in logical signals. Collect devices, mappings, protocols and shared calibration in the laboratory configuration.
2. Implement and test the target's clock, resource, phase and acquisition contracts.
3. Recompile the original experiment. Check quantization, resource contention and explicit pulse intervals. A previously feasible interval may exceed the new device's throughput.
4. Compare logical and physical events. Verify sweep, shot and handle indexing. Independently reconstruct or simulate the target's exported events.
5. Integrate deployment, real drivers and a detector adapter. Verify real compilation and physical operation separately.

## Shared and target-specific scheduling

`compiler.scheduling.Scheduler` implements common rules. `dsl.sections` defines Sections and composition. `compiler.section_schedule` places them. `section_grid()` supplies the base container grid, and `signal_grid()` enforces signal-specific constraints. Inside a triggered Spectrum region the scheduler uses the sample grid. Outside it uses the native grid. The base scheduler uses a configured Spectrum native grid when present, otherwise one tick. `compiler.timebase` performs exact conversions without depending on a particular backend.

The common scheduler uses `signal_frame()`, `frame_owner()` and read-only `time_origin` for resource and time queries. A target override must account for actual placement and final phase. Reject an unrepresentable request instead of increasing an interval or silently changing its grid.
