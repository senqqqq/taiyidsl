# TaiyiDSL tutorials

Start with [plain Ramsey and clock Ramsey](ramsey_clock_plain.ipynb). Read the experiment directly as `play`, `delay` and `reset_phase`. Then compare the [operations version](ramsey_clock_walkthrough.ipynb) using the same wiring, calibration, preparation/readout and scan. Both include the experimental source in the notebook.

## Open a tutorial

From a source checkout, run:

```powershell
uv sync --locked --group notebooks
uv run --locked --group notebooks jupyter lab docs/tutorials/ramsey_clock_plain.ipynb
```

Select the project CPython 3.12 kernel and use **Restart Kernel → Run All**. The checkout includes examples, generator inputs and native sources that are outside the installed wheel. All notebooks run offline by default. Example values are uncalibrated. A native TTL fixture illustrates timing. It cannot prepare atoms or return camera frames.

| Notebook | What you can inspect | Local outputs |
|---|---|---|
| [Plain Ramsey and clock Ramsey](ramsey_clock_plain.ipynb) | Build an experiment from device databases and wiring, then inspect pulse timing and DAC samples | `output/plain_ramsey/` |
| [Ramsey with reusable operations](ramsey_clock_walkthrough.ipynb) | Express the same experiments with reusable RF π/2 and clock 2π operations | `output/simple_ramsey/` |
| [Clock Ramsey with transport](ramsey_clock_transport.ipynb) | Move to an interaction zone, align AWG and DDS outputs and inspect the return trajectory | `output/ramsey_clock_transport/` |
| [¹⁷¹Yb global-RF tune-up](yb171_global_rf_tuneup.ipynb) | Design and analyze RF, clock, Rydberg and two-qubit experiments | `output/yb171_tuneup/` |
| [Advanced hybrid control](hybrid_from_scratch.ipynb) | Combine native components, two-qubit RB, disk compilation, joint readout and durable shot records | `output/tutorial_from_scratch/` |
| [中文版：原生 MOT、GM 冷却、态制备与 578 nm 钟谱](native_mot_clock_zh.ipynb) | Integrate atom-control sources and its device database with a Spectrum clock probe in one shot | `output/native_lab_tutorial/` |
| [中文版：单卡 Spectrum 调试 578 nm 钟谱探测脉冲](spectrum_clock_578_zh.ipynb) | Inspect a five-frequency AWG scan and play a center-frequency burst on a standalone card for scope checks | `output/spectrum_clock_578/` |
| [中文版：Kasli TTL 触发单卡 Spectrum](artiq_spectrum_single_ttl_clock_zh.ipynb) | Compile a two-pulse AWG waveform offline; explicitly opt in to one hardware shot with an internal PLL and an EXT0 edge | No default output; hardware sessions write receipts |

Reruns update demonstration outputs. Keep measured data in a separate location. The published notebooks contain source cells with cleared execution outputs.

## Follow one experiment through the control stack

The two introductory notebooks begin with an editable ARTIQ device database, Spectrum card configuration and wiring descriptor. `LabConfig` brings together the setup, qubits and execution mode. `Qubit` owns the role calibrations. Name pulses before building the experiment. Native declarations read source without importing ARTIQ in Jupyter. Experiment functions accept the qubit, pulses, scan and preparation/readout explicitly and return an `Experiment`. Compile outside the function. Use the public `from taiyidsl import ...` interface for experiments.

Both Ramsey versions observe q0 under a shared RF field. Plain Ramsey scans dark time `tau`. Clock Ramsey scans the second RF pulse's `phase`. Both RF pulses use `q0.parameters.rf.frequency * Hz + detuning` with no extra detuning-dependent phase term. The same named `pi2_pulse` stores amplitude and duration. Rebuild the pulse after changing either. If the pulse omits frequency, changing just the qubit frequency requires a new experiment and compile. Old experiments retain their snapshots. The advanced notebook demonstrates revision tracking, applying a calibration proposal and reopening `QubitStore`. Ordinary editable qubits also support a JSON round trip in the introductory notebook.

Clock Ramsey plays RF π/2 (100 µs), clock 2π (200 µs), then RF π/2 (100 µs) with no intervening idle. Card B retains its leading quiet interval on the shared timeline. The example RF carrier is 30 + 1 = 31 kHz. The 200 kHz clock setting is an electrical carrier. Physical rotation angles require calibration.

At 1.25 GS/s samples are 0.8 ns apart. Each segment uses a multiple of 32 samples per channel with minima of 384 samples for one channel and 192 for two. The backend pads the segment without stretching the dark time. Compare cards on the common logical timeline rather than starting each display at its own first nonzero sample. The viewers decode the final DAC values. Variable host/native durations do not become fixed waits.

Both notebooks also check the plain function against `@experiment` in their own kernel. A separate comparison checks the plain and operation versions against each other.

## Transport and tune-up

The transport notebook shows RF π/2 → move into the zone → clock 2π → return to the initial position → RF π/2. Cards A/B use AWG, C uses Spectrum DDS20. All share a trigger group with declared latency compensation. The viewer shows final DAC and DDS frequency/amplitude segments. Positions describe planned motion. `q0.position` retains the initial location. Card C holds a nonzero endpoint until the session closes. Arbitrary DDS gate synthesis and a shared absolute phase ledger across DDS and AWG require separate capabilities.

Yb tune-up covers RF Rabi/Ramsey in two dimensions, refocusing, spin lock, error amplification and RB. Clock and Rydberg sections cover shelving phase, matched holds, light shifts, pair chevrons and return diagnostics. CZ sections cover closure, echo, Bell/parity, input-dependent loss, symmetric stabilizer benchmarking and shaped candidates with held-out validation. Every complete gate includes shelving and return. The notebook distinguishes ideal calculations, synthetic observations and measured data, and explains the space supported by global controls. See [benchmark and waveform references](yb171_benchmarking_references.md) and the [numerical helpers](../../examples/yb171_tuneup_analysis.py).

## Preparation, readout and hardware

Introductory examples register the GreenMOT fixture via `NativeKernelPackage.from_package`. Its initialization calls `core.reset()` and `finalize_method` runs per shot. For independent components, `NativeModule.from_package` and `NativeProgram` support session initialization/finalization and a separate after-shot hook. Keep the lifecycle of the chosen integration. The [native walkthrough](native_components_walkthrough.md) shows both paths, the [component guide](../native_components.md) details contracts, and the [sample laboratory package](../../examples/lab_extension/README.md) includes readout adapters.

For real hardware, supply verified wiring, actual device identities, calibration, an ARTIQ interpreter and the identifier of the running continuous-clock session. `lab.connect()` checks the connection. `session.run()` uploads and executes. The context manager releases owned resources. Confirm multi-card trigger skew, sample synchronization, DMA/step table, re-arm and analog phase/amplitude on instruments. A compiled trigger or camera receipt does not include frames. The direct `HardwareSession.run()` API has no detector-adapter injection point. Match real frames to point/shot/attempt, distinguish zero counts from missing frames and recover persisted data before considering another shot. See [readout adapters](../readout_adapters.md) and [durable execution](../streamed_execution.md).

Advanced hybrid uses an illustrative CZ and synthetic RB results. Replace the gate and calibrate the actual interaction before interpreting physical performance. Disk compilation bounds managed buffers. The experiment, RB words, indexes and Python objects can grow outside that budget. See [streamed execution](../streamed_execution.md). Validate interactive report navigation in a browser, including chunk changes, zoom, long windows and source links. Record browser checks, disconnected [ARTIQ compilation](../artiq_toolchain.md), captured device timing and measured physical results separately.

## Rebuild and verify

| Notebook | Generator |
|---|---|
| Both introductory Ramsey variants | [build_ramsey_tutorial.py](../../scripts/build_ramsey_tutorial.py) |
| Transport | [build_clock_transport_tutorial.py](../../scripts/build_clock_transport_tutorial.py) |
| Yb tune-up | [build_yb171_tuneup_notebook.py](../../scripts/build_yb171_tuneup_notebook.py) |
| Advanced hybrid | [build_hybrid_tutorial.py](../../scripts/build_hybrid_tutorial.py) |
| Native MOT and 578 nm spectroscopy (Chinese) | [build_native_lab_tutorial.py](../../scripts/build_native_lab_tutorial.py) |
| Standalone Spectrum clock probe (Chinese) | [build_spectrum_clock_tutorial.py](../../scripts/build_spectrum_clock_tutorial.py) |

Change the generators and their source inputs together. Transport reads configuration cells from the generated plain notebook. Generate Ramsey first.

```powershell
uv run --locked --group notebooks python scripts/build_ramsey_tutorial.py
uv run --locked --group notebooks python scripts/build_clock_transport_tutorial.py
uv run --locked --group notebooks python scripts/build_yb171_tuneup_notebook.py
uv run --locked --group notebooks python scripts/build_hybrid_tutorial.py
```

The [offline runner](../../scripts/execute_hybrid_tutorial.py) starts a fresh project kernel for each source. It requires local Jupyter loopback communication and does not register a global kernel. Use these commands for individual lessons:

```powershell
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/ramsey_clock_plain.ipynb --output output/plain_ramsey/tutorial --semantics
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/ramsey_clock_walkthrough.ipynb --output output/simple_ramsey/tutorial --semantics
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/ramsey_clock_transport.ipynb --output output/ramsey_clock_transport/execution --semantics
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/yb171_global_rf_tuneup.ipynb --output output/yb171_tuneup/execution --semantics
uv run --locked --group notebooks python scripts/execute_hybrid_tutorial.py --source docs/tutorials/hybrid_from_scratch.ipynb --output output/tutorial_from_scratch/execution
```

Read the executed notebook, static HTML and `notebook_verification.json` with the actual source hash, kernel and outcome. `--semantics` adds a kernel-local verification cell and records `tutorial_semantics.json` outside the published notebook. Compare both introductory styles without rerunning them:

```powershell
uv run --locked python scripts/tutorial_semantics.py output/plain_ramsey/tutorial/tutorial_semantics.json output/simple_ramsey/tutorial/tutorial_semantics.json --pair compiled=compiled --pair clock_compiled=clock_compiled
```

Compare events, scan order, phases, final sample programs and acquisition indices. Source line changes can affect provenance independently of those semantics. The offline tutorial CI executes the four introductory, transport and tune-up notebooks and compares the two Ramsey styles. The advanced hybrid command runs the fifth lesson separately. See the [contributing guide](../../CONTRIBUTING.md).
