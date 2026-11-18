# taiyidsl

A Python pulse-level experiment framework for neutral-atom researchers. Describe
pulses, timing, scans and repeated shots; compile an inspectable plan; then view
its waveforms or explicitly run it through a configured laboratory connection.

**Status: laboratory collaboration preview, offline first.** The available
backends cover ARTIQ 9 / AD9910 / TTL and hybrid ARTIQ + Spectrum AWG/DDS.
Offline verification, real ARTIQ compilation and instrument acceptance are
separate evidence. Selected historical exports have real compile-only evidence;
RTIO capacity, device synchronization, optical timing and camera acquisition
still require laboratory validation. Example parameters are not calibrations.

[Chinese guide](docs/README.zh-CN.md) ·
[Tutorial setup](docs/tutorials/README.md) ·
[Developer guide](docs/development.md) ·
[Contributing](CONTRIBUTING.md) ·
[Public API](docs/public_api.md)

The shared development repository is [TaiyiQ/pulse-dsl](https://github.com/TaiyiQ/pulse-dsl).
[senqqqq/taiyidsl](https://github.com/senqqqq/taiyidsl) is a synchronized personal copy.
Open collaboration issues and pull requests in the shared repository.

## Start with an experiment

Start with the [plain Ramsey and clock Ramsey notebook](docs/tutorials/ramsey_clock_plain.ipynb).
It exposes the configuration and experimental sequence directly. The matching
[qubit-operation version](docs/tutorials/ramsey_clock_walkthrough.ipynb) shows
how to reuse a short operation without changing the experiment's structure.
The lessons follow one route:

| Step | What the researcher writes or inspects |
| --- | --- |
| Device databases | ARTIQ devices and Spectrum cards, addresses and clocks |
| Wiring descriptor | Physical channels, logical signals and shared outputs |
| `LabConfig` | The setup, registered qubits and offline/hardware mode |
| `Qubit` | Editable calibration parameters and named pulse definitions |
| Native ARTIQ | Existing preparation and readout routines with explicit boundaries |
| Experiment definition | A plain function, explicit scan arguments and visible pulse timing |
| Compile and visualize | `lab.compile(exp)`, pulse sheets, final waveforms and exported code |
| Connect to hardware | A separate, explicit session using the laboratory's configuration |

Use the public `from taiyidsl import ...` entry points. The internal
`dsl/`, `config/`, `compiler/` and `runtime/` directories organize library
maintenance; experiment authors do not need to import their private helpers.
Define pulses before the experiment body, pass physical quantities explicitly,
and keep compilation and connection outside the experiment function.

After Ramsey, choose the extension relevant to your work:

| Tutorial | Focus |
| --- | --- |
| [171Yb global-RF tune-up](docs/tutorials/yb171_global_rf_tuneup.ipynb) | 2D Rabi/Ramsey, coherence and loss controls, full-cycle CZ benchmarking and waveform candidates; [paper-to-protocol guide](docs/tutorials/yb171_benchmarking_references.md) |
| [Clock Ramsey with transport](docs/tutorials/ramsey_clock_transport.ipynb) | Explicit IZ moves, AWG/DDS synchronization and planned atom positions |
| [Advanced hybrid reference](docs/tutorials/hybrid_from_scratch.ipynb) | Multiple native components, RB, chunking and joint-shot results |

All default notebook cells remain offline. The hardware section is an explicit
handoff to a configured laboratory, not part of the notebook validation run.
The GreenMOT fixture emits demonstration TTLs; it does not prepare atoms or
retrieve camera frames.

## Install and open a tutorial

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
from a source checkout:

```bash
uv sync --locked --group notebooks
uv run --locked --group notebooks jupyter lab docs/tutorials/ramsey_clock_plain.ipynb
```

The project pins CPython **3.12.15** in `.python-version` and dependencies in
`uv.lock`. uv manages the interpreter and environment; the first sync may
download them. For library development without notebooks, use `uv sync --locked`.
Tutorials use the repository's example sources and native fixtures, so run them
from the checkout. Installing the wheel alone does not install those lessons.

For UTF-8 console output in Windows PowerShell:

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

ARTIQ is not required for offline authoring and compilation. Real execution
needs the [separate ARTIQ toolchain](docs/artiq_toolchain.md), native drivers
and laboratory configuration. `uv sync --locked --extra spectrum` installs
the optional Spectrum Python SDK; the lock selects `spcm 1.13.4`, the checked
version. A permitted dependency range is not evidence for every SDK version.
See [installation and repository contents](docs/repository_contents.md).

## Extending a laboratory experiment

Keep real wiring, calibration, camera configuration and laboratory-specific
ARTIQ classes in your own package. Start with the
[standalone lab extension example](examples/lab_extension/README.md).
Reusable language, compiler and data capabilities belong in TaiyiDSL.

New independent MOT, cooling or preparation components use
`NativeModule.from_package(...)` and `NativeProgram`. The Ramsey tutorials
retain `NativeKernelPackage.from_package(...)` for the existing fixture:
its initialization resets the core and its finalizer runs after each shot.
Component finalization runs once per session. These lifecycle contracts cannot
be exchanged by renaming a class; see [native components](docs/native_components.md).

Camera triggering, frame retrieval and data submission are separate actions.
`HardwareSession.run()` currently has no detector-adapter injection interface;
a returned execution receipt is not a measurement. See the
[readout adapter contracts](docs/readout_adapters.md) before adding a camera.

## Reference guides

Read these when the experiment needs the corresponding capability:

| Topic | Guides |
| --- | --- |
| Authoring and reuse | [Experiment functions and timing](docs/experiment_authoring.md), [Sections and operations](docs/sections_and_operations.md), [Qubit parameters](docs/qubit_parameters_and_operations.md) |
| Devices and native code | [Descriptor and connection](docs/device_setup_descriptor.md), [Native integration](docs/native_artiq_integration.md), [ARTIQ toolchain](docs/artiq_toolchain.md) |
| Hybrid control | [Spectrum](docs/spectrum_hybrid.md), [Phase modes](docs/spectrum_phase_modes.md), [DDS transport](docs/transport_dds.md) |
| ARTIQ timing | [Square pulses](docs/square_timing.md), [RAM pulses](docs/pulses_and_ram.md), [RAM boundaries](docs/ram_boundary_timing.md), [Virtual Z and short gates](docs/phase_switching_and_virtual_z.md) |
| Large experiments | [Chunked execution](docs/chunked_execution.md), [Disk compilation and recovery](docs/streamed_execution.md), [Randomized benchmarking](docs/randomized_benchmarking.md) |
| Inspection and data | [Compiled viewers](docs/compiled_viewers.md), [Results and imaging](docs/results_and_imaging.md), [Capture comparison](docs/capture_comparison.md) |
| Optical preparation | [Laboratory profiles](docs/lab_recipes_experiments.md), [Yb preparation reference](docs/yb171_full_preparation.md) |
| Maintenance | [Source layout](docs/source_layout.md), [Architecture](docs/architecture.md), [Backend contracts](docs/backends.md), [Design decisions](docs/design_decisions.md), [Validation scope](docs/validation.md) |

The HTML reports include their viewing dependencies locally. Waveform displays
use compiled samples; DDS transport curves are command predictions. Neither
is a physical measurement or a model of the atoms.

## Contributing and checking changes

| If you want to... | Start here |
| --- | --- |
| Write an experiment or a lab extension | [Tutorials](docs/tutorials/README.md) and [examples](examples/) |
| Change the library | [Source packages](src/taiyidsl/) and their [responsibilities](docs/source_layout.md) |
| Regenerate a notebook, inspect a driver export, or prepare a release | [Repository tools by task](scripts/README.md) |
| Check a behavior change | The corresponding [tests](tests/) and the [test selection guide](CONTRIBUTING.md#during-development) |

Read [CONTRIBUTING](CONTRIBUTING.md) and the [developer guide](docs/development.md)
for code ownership, extension contracts and test selection. Use focused tests
for the behavior changed, plus Ruff on edited Python files. A prose-only edit
needs content and link review, not a package build.

When changing a tutorial, update its generator and notebook together, then
execute affected notebooks in fresh kernels. A cross-stage integration
checkpoint also compares the two Ramsey authoring versions and runs the
offline suite; it is not required after every local edit. Existing CI retains
Windows/Linux checks. Report what was actually run, and keep hardware
validation separate.

Source notebooks are published without execution outputs. Generated reports,
useful executed copies, toolchains and data stay outside source history;
see [repository contents](docs/repository_contents.md). No open-source license
or public distribution policy has been selected for this collaboration preview.
