# taiyidsl

A Python pulse-level experiment framework for neutral-atom researchers. Describe
logical pulses, timing blocks, parameter sweeps, and repeated shots; compile them
into an inspectable execution plan; then generate an offline timeline and ARTIQ
reference code.

**Status: offline first version.** The reference backend targets ARTIQ 9,
Urukul AD9910, and TTL. Real ARTIQ compilation, RTIO throughput, hardware
synchronization, and optical timing at the atoms are **not verified**. Example
preparation and readout protocols are TTL triggers, not calibrated laboratory
procedures. Nothing in the examples connects to instruments.

[Chinese guide](docs/README.zh-CN.md) · [Contributing](CONTRIBUTING.md) ·
[Architecture and contracts (Chinese)](docs/architecture.md) ·
[Validation record (Chinese)](docs/validation.md)

## Installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
these commands from the project directory:

```bash
uv sync --locked
uv run --locked python examples/tuneup_workflow.py
uv run --locked python examples/state_initialization.py
```

The project pins CPython **3.12.15** in `.python-version` and dependencies in
`uv.lock`. uv manages the interpreter and environment; no machine-specific
interpreter path is needed. The first sync may download Python and dependencies.
ARTIQ is not a required dependency of the offline package.

For UTF-8 output in Windows PowerShell, set this before running examples:

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

## Quick start

```python
from taiyidsl import Qubit, compile_experiment, rabi, reference_setup, us

q0 = Qubit("q0")
setup, mapping = reference_setup([q0])
exp = rabi(q0, [i * us for i in range(41)], shots=100, mode="cyclic")
compiled = compile_experiment(exp, setup, mapping)

compiled.visualize("output/rabi.html")
export = compiled.export_artiq("output/rabi_artiq")
print(export["verification"])  # Offline mock verification only.
```

The HTML includes Plotly locally and needs no CDN. It provides logical and
physical views, scan-point and shot selectors, source information, event tables,
and zooming. Selecting a shot shows its indexing and execution order; it does
not imply a fixed host repetition period.

## Runnable examples

| Example | What it demonstrates | Output |
|---|---|---|
| [`tuneup_workflow.py`](examples/tuneup_workflow.py) | Qubit storage, Rabi fitting, explicit calibration application, Ramsey, and randomized benchmarking | `output/tuneup_workflow/` |
| [`state_initialization.py`](examples/state_initialization.py) | A Yb-171 optical-pumping duration sweep with matched push-out and no-push-out reference conditions | `output/state_initialization/` |

The tune-up example generates synthetic observations, compiled JSON, standalone
HTML, fit results, and both compact and unrolled ARTIQ exports. Each experiment's
`export_comparison.json` records source statistics and executed-event equivalence.
Re-running the example replaces its demonstration outputs, including its sample
calibration store.

The initialization example uses six durations and two readout conditions: 12
scan points share one parameterized shot kernel. Its optical and timing values
are placeholders. Every shot requires freshly prepared and cooled atoms, a
stable trap and bias field, and calibrated optical paths supplied by a laboratory
integration. The example's image protocol only produces a camera trigger;
illumination, camera transfer, and state-discrimination calibration remain external.
If the cooling-to-pumping interval must be deterministic, both sequences must
be scheduled within the same realtime program rather than separated by a host
boundary. No initialization fidelity is inferred from these offline artifacts.

## Experiment DSL

```python
from taiyidsl import (
    Experiment,
    Qubit,
    Square,
    SweepParameter,
    delay,
    measure,
    play,
    prepare,
    reset_frame,
)

q0 = Qubit("q0")
exp = Experiment("ramsey", qubits=[q0])
q = exp.qubits["q0"]  # This experiment's immutable parameter snapshot.
tau = SweepParameter("tau", [0, 4, 8, 12], unit="us")

with exp.average(shots=100, mode="sequential"):
    with exp.sweep(tau):
        prepare(q, protocol="standard")
        with exp.realtime():
            reset_frame(q.frame)
            pulse = Square(
                q.params.raman.pi2_duration,
                amplitude=q.params.raman.amplitude,
            )
            play(q.raman, pulse)
            delay(q.raman, tau)
            play(q.raman, pulse)
            measure(q, protocol="state_readout", handle="population")
```

Use `compile_experiment(exp, *reference_setup([q0]))` to compile this example.
The reference protocols must be replaced before laboratory execution.

Bare times are in seconds, frequencies in Hz, and phases in radians. Unit objects
include `ns`, `us`, `ms`, `s`, `Hz`, `kHz`, `MHz`, `rad`, and `turn`. Amplitude is
normalized full scale in the interval 0 to 1, not optical power or Rabi frequency.
`SweepParameter.unit` interprets bare sweep values; compiled coordinates use SI
units and preserve both input-unit and stored-unit metadata. Values already
carrying units are not scaled twice.

### Averaging and loops

| Mode | Two shots at scan points A, B, C |
|---|---|
| `cyclic` (default) | A0, B0, C0, A1, B1, C1 |
| `sequential` | A0, A1, B0, B1, C0, C1 |

Both modes retain raw `[point, shot, qubit]` data. Averaging observations is a
separate analysis operation. Nested sweeps form a Cartesian product, with the
innermost axis varying fastest; a cyclic round covers that entire product.

An experiment has one top-level `average` block, followed by a single chain of
nested sweep blocks and the shot program. `repeat(count)` repeats a realtime
block; it does not declare sampling or calculate averages. A Python `for` loop
expands during construction. `exp.case(parameter, value)` selects a static scan
condition during compilation, not a branch on realtime measurement results.

### Timing and phase

- Operations are serial by default. Use `exp.parallel(align="start")` or
  `align="end"`, normally with explicit `exp.serial()` branches, for concurrency.
- A parallel block completes when all branches complete. End alignment requires
  known branch durations. `exact_duration` checks equality rather than adding
  padding.
- `delay(signal, duration)` reserves an idle interval in its branch and does not
  reset the software frame. Other branches do not automatically wait for it.
- `reserve(signal_or_resource, duration)` reserves a resource without output.
  Unknown resources and conflicting exclusive reservations are rejected.
- `shift_phase(q.frame, angle)` and `reset_frame(q.frame)` update a software
  reference frame, independently of DDS hardware accumulator reset. Conflicting
  frame access between parallel branches is rejected.
- A zero-duration pulse retains a logical boundary but produces no physical
  output. A nonzero pulse shorter than the device minimum is rejected.

Compatible adjacent pulses can merge into continuous physical output while their
logical boundaries remain visible. Changing frequency, phase, or amplitude
requires a valid device configuration window. The compiler does not insert a
hidden gap to make a sequence feasible. RB therefore requires an explicit
`inter_gate_delay`.

Times must fit the mapped device grids exactly by default. With explicit
`allow_quantization=True`, durations and delays are rounded to the applicable
common grid; diagnostics record requested values, realized values, and errors.
Quantization does not repair incompatible alignment by adding gaps, and a
nonzero operation cannot be rounded to zero.

The current framework does not support a fixed host shot period, cross-shot
phase continuity, measurement feedback, arbitrary AWG waveforms, or atom
movement. Requests for unsupported behavior are rejected.

## Qubit parameters and calibration

```python
from taiyidsl import (
    Qubit,
    QubitStore,
    analyze_rabi,
    compile_experiment,
    rabi,
    reference_setup,
    synthesize,
    us,
)

store = QubitStore([Qubit("q0")], path="output/qubits.json")
store.save()
q0 = store.update("q0", {"raman.amplitude": 0.4}, persist=True)

exp = rabi(q0, [i * us for i in range(61)], shots=500)
compiled = compile_experiment(exp, *reference_setup([q0]))
results = synthesize(compiled, seed=1, model_parameters={"pi_duration": 12e-6})
proposal = analyze_rabi(results, experiment=exp)

# Fitting creates a proposal. Applying it is a separate, explicit operation.
q1 = store.apply(proposal, persist=True)
```

`Qubit`, its parameter groups, and extension values are immutable. A store update
creates a new revision: use the returned Qubit or retrieve `store["q0"]` again.
Existing variables, experiments, and compiled artifacts retain their snapshots.

Built-in drive fields are `params.raman` and `params.rf`, each with `frequency`,
`amplitude`, `pi_duration`, and `pi2_duration`; readout has
`params.readout.duration`. Namespaced extensions belong in `params.extensions`.
Shared device delays, phase offsets, and output conversions belong in
`DeviceSetup.calibration`, rather than being duplicated into every Qubit.

A `CalibrationProposal` records source revision and fingerprint, target fields,
units, fit quality, and experiment provenance. Stale sources, invalid units or
values, and failed persistence do not update the store. JSON writes use atomic
replacement and retain update history. Automatic application requires explicit
`store.apply_auto(...)` with a `CalibrationPolicy`; it uses the same validation
and persistence path as manual application.

Rabi analysis only proposes drive updates for a matching square-pulse duration
scan and source profile. Ramsey returns the magnitude of the detuning and the
decay time; it does not infer the sign of a frequency correction. RB retains the
seed, Clifford identities, recovery, and actual pulse sequence. Ideal recovery
is checked using exact integer SO(3) operations. Synthetic fit results verify
the data flow, not physical calibration accuracy.

## Device mapping and ARTIQ export

`SignalMap` maps logical signals to device names, transformed `ControlBinding`
objects, or multi-output `ControlRecipe` objects. A recipe can expand one logical
Raman control into several physical outputs, but is not automatically a
calibrated two-photon transition model.

`DeviceSetup` declares clocks, output and configuration grids, DDS configuration
budgets, shared buses, and update groups. The reference backend conservatively
rejects an update while another DDS in its update group is producing output,
and schedules blocking configuration calls in a globally serial order. These
are backend constraints, not universal ARTIQ hardware limits.

Reference values include a 4-mu DDS output grid, an 8-mu configuration grid,
a 1000-mu configuration budget, and `tick_seconds=1e-9`. Each shot uses an
independent tracking-phase reference. These assumptions require device and
optical-path validation before hardware use.

```python
from taiyidsl.artiq import verify_equivalence

compact = compiled.export_artiq("output/artiq")  # mode="auto" by default.
expanded = compiled.export_artiq("output/artiq_unrolled", mode="unrolled")
comparison = verify_equivalence(compiled)
```

The default export uses **host averaging/sweep loops, parameter tables, and
parameterized kernels shared by compatible event structures**. It applies to
custom experiments as well as Rabi and Ramsey templates. A shorter event sequence
can share a longer sequence using optional event slots, including zero-duration
Rabi and zero-delay Ramsey points. Incompatible structures form separate kernel
groups. The debug mode generates a separate `point_N` kernel for each point.

Both modes use the same compiled timing plan. They do not move averaging or
sweeps onto the core. Static `repeat` blocks within a shot are still expanded by
the compiler. Shorter source does not guarantee a single real ARTIQ compilation
or improved hardware throughput.

Generated code includes device initialization, target-clock checks, and DDS
configuration-budget guards. Before execution, a laboratory integration must
provide `result_adapter.before_shot(point, shot)`, `after_shot(point, shot)`, and
`finish()` for preparation and data transfer. The exporter does not implement a
detector interface.

`verify_export` executes trusted generated Python with independent mock drivers
and compares reconstructed events with the compiled plan. `verify_equivalence`
executes both representations and compares event order, times, DDS words, phase
references, completion times, and host sampling order. Neither runs the real
ARTIQ compiler. These functions execute Python and are not sandboxes for
untrusted source.

Compiled artifacts retain IR, snapshots, hashes, source information, diagnostics,
and both logical and physical event tables. Integrity checks reject subsequent
artifact mutation; rebuild from the experiment rather than modifying a compiled
object in place.

## Results and development

`ResultSet` stores data by measurement handle, including numerical observations,
statuses, labels, asset references, and execution order. Missing, failed, lost,
or unexecuted observations are distinct from valid zero-valued measurements.
Aggregation uses valid numerical samples and records valid counts and explicit
postselection. JSON and NPZ loading does not enable pickle.

`synthesize` generates seeded test observations. It is not a full atomic-dynamics
simulation of compiled waveforms. The same coordinate and shot give the same
synthetic observations in either averaging mode.

Run the local checks from the repository root:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest --basetemp=.test-tmp-pytest
uv build --no-sources
```

Node.js 22 is used for the standalone HTML JavaScript syntax check. Without Node,
that test is skipped locally; CI installs it explicitly. The CI workflow covers
Windows and Linux with the pinned Python version. Generated output, caches,
virtual environments, and build artifacts are excluded from source control and
source linting.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the standards, review workflow, and
guidance on adding tests and device adapters. Test and export verification must
remain separate from real ARTIQ compilation and hardware acceptance.
