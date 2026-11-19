# Source layout

Start with the [Ramsey tutorial](tutorials/ramsey_clock_plain.ipynb) to write an experiment. Use this map when changing the library. The [tools index](../scripts/README.md) covers notebook generation, compilation and export inspection.

| Package under `src/taiyidsl/` | Responsibility | Entry points for maintainers |
| --- | --- | --- |
| `dsl/` | Build the experiment's source tree and capture caller locations. | `nodes`, `experiment`, `instructions`, `pulses`, `values`, `sections`, `operations`, `authoring` |
| `config/` | Validate qubit parameters, devices, wiring, optics, calibration and transport. | `models`, `parameters`, `editable_parameters`, `role_parameters`, `hardware`, `descriptor`, `spectrum`, `transport` |
| `compiler/` | Snapshot inputs, schedule each point, lower it through a selected target, and assemble an artifact. | `preparation`, `points`, `pipeline`, `artifact`, `timebase`, `resources`, `section_schedule` |
| `compiler/` storage | Plan chunks, compile to disk and verify a completed store. | `chunking`, `streaming`, `store`, `_stream_initializer` |
| `backends/` | Declare the target contract and explicitly register or select a target. | `base`, `register_backend`, `get_backend` |
| `backends/artiq/` | Lower AD9910 and TTL operations, generate ARTIQ source and verify it offline. | `backend`, `codegen*`, `submission`, `mock_drivers`, `verification*`, `export` |
| `backends/artiq_spectrum/` | Coordinate the two time domains, create AWG/DDS programs, check capacity and export banks. | `scheduling`, `phase`, `awg`, `validation_awg`, `dds`, `banks`, `export` |
| `native/` | Declare and pin external kernels, audit sources, compose and export components. | `declarations`, `components`, `sources`, `audit`, `component_export`, `export` |
| `runtime/` | Own connections, execute programs, manage readout, journal attempts and recover results. | `session`, `worker`, `contracts`, `readout`, `run_store`, `streaming`, `spectrum/` |
| `results/` | Interpret scalar and image observations, archives and analysis. | `scalar`, `imaging`, `archives`, `analysis`, `rb_results`, `rb_analysis` |
| `reporting/` | Show exported programs, samples, device settings and execution evidence. | `experiment_report`, `resources`, `gate_annotations`, `assets/` and the specialized viewers |
| `experiments/` | Provide reusable operations, experiments and ideal sequence mathematics. | `ramsey`, `cooling`, `templates`, `rb_sequences`, `rb_sections` |

## Which layer owns a change?

- **Section composition** belongs to `dsl.sections`. It owns the single `Section` type and the `section`, `add` and `layer` functions. **Section placement** belongs to `compiler.section_schedule` and consumes declared time grids and shared resources.
- **Transport configuration** belongs to `config.transport`. The compiler's `spatial.SpatialState` tracks position along an executed branch without changing the user's qubit object.
- **Generation and verification** have separate owners. The ARTIQ generator emits deployable kernel source. The mock drivers execute it, and verification compares execution with the requested plan. Spectrum's AWG validator compares independently calculated expected samples with finite replay. Its DDS checker reconstructs command intent and checks integrity.
- **Clifford sequence mathematics** lives in `experiments._single_qubit_clifford` and `experiments.rb_sequences`. Experiment templates and Section adapters consume the same seeded sequence and recovery rules.
- **Machine access** starts with an explicit runtime connection. Configurations, builders, compilers, reports and normal imports remain usable offline. The Spectrum SDK is loaded at the adapter boundary.
- **Shared files** include `units.py` for dimensions, `ir.py` for scheduled events and physical plans, `host_plan.py` for shot order, and `_artifact_io.py` for canonical bytes and atomic publication. `lab.py` is the application facade. `spectrum_identity.py` and `spectrum_calibration.py` serve both calibration and execution. `serialization.py` and `execution.py` support exporters and consumers.
- **Package resources** include the report template and viewer JavaScript. Load them through the package resource API so an installed wheel can render a report. Generated ARTIQ code must remain deployable in its separate environment.

Laboratory code should use the [public API](public_api.md). Internal imports target the owner shown above. Keep frozen event fixtures with their existing provenance. Compare meaningful timing and physical data when a source edit changes file locations or hashes.
