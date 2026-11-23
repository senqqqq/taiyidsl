# Qubit parameters, control frames and standard operations

Use `Qubit(uid, signals=..., parameters=..., position=...)` for new experiments. Attributes such as `q.rf`, `q.clock` and `q.rydberg` name logical signals. `q.parameters.rf` holds the calibration for the RF role. The `DeviceSetup` maps signals to laboratory channels and physical outputs and owns card addresses, sample rates and output ranges. The numbers below illustrate the API.

## Define parameters by signal role

```python
from taiyidsl import Qubit, Position, QubitOperations, us, MHz, um

q = Qubit(
    uid="q0",
    signals={"rf": "q0.rf", "clock": "q0.clock", "rydberg": "q0.rydberg"},
    parameters={
        "rf": {
            "frequency": 1 * MHz,
            "pi_duration": 10 * us,
            "pi2_duration": 6 * us,
            "pi_amplitude": 0.3,
            "pi2_amplitude": 0.22,
            "waveform": "square",
            "frame": "spin",
        },
        "clock": {
            "frequency": 80 * MHz,
            "pi_duration": 20 * us,
            "pi2_duration": 12 * us,
            "pi_amplitude": 0.25,
            "pi2_amplitude": 0.18,
            "frame": "clock_control",
            "transition": {"lower": "g_up", "upper": "e_up"},
        },
        "rydberg": {},  # Declare the signal before its gate calibration is available.
        "extensions": {"my_lab": {"trap_label": "A"}},
    },
    position=Position(2 * um, 3 * um, 0, "tweezer_plane"),
)

q.rf  # "q0.rf", the logical signal for play and other DSL instructions.
q.clock  # "q0.clock"
q.parameters.rf.frequency  # Numeric value in Hz.
q.parameters.clock.pi2_duration
q.position  # 3D coordinates in metres with a coordinate-system label.

ops = QubitOperations(drive="rf")
saved = ops.x90(q)  # Outside an experiment, return a standalone Section.
```

Every drive role uses the same parameter structure. Signal names must match `DeviceSetup.signals` but need not follow the spelling `q0.rf`. Labs can name their own roles and access them dynamically through `q.signal(name)` and `q.parameters[name]`.

| Parameter | Meaning |
| --- | --- |
| `frequency` | Logical drive frequency. Explicit transformations in the output binding determine the corresponding hardware frequency. |
| `resonance_frequency` | Optional transition resonance. It does not supply a default drive frequency. |
| `pi_duration`, `pi2_duration` | Separately calibrated total durations, stored in seconds. |
| `pi_amplitude`, `pi2_amplitude` | Separately calibrated normalized amplitudes in [0, 1]. |
| `waveform`, `waveform_parameters` | Shape and parameters. The default shape is `square`. |
| `frame` | Associated control frame name or `None`. |
| `transition` | Optional lower and upper levels and resonance frequency. |

Missing numeric parameters remain `None`. For example, declaring an empty `rydberg` role is valid, and requesting a gate that needs its missing frequency, duration or amplitude raises an error. The library does not derive π/2 calibration by halving π calibration. Zero amplitude is a valid explicit value. When the role and its `transition` both specify `resonance_frequency`, they must agree.

`Position` holds static coordinates. Addressing and transport require explicit operations. Use namespaced, serializable `extensions` for laboratory data, for example `q.parameters.extensions["my_lab"]`.

## Build an operation

```python
from taiyidsl import Experiment, add

exp = Experiment("gate_example", [q])
with exp.average(10), exp.realtime():
    with exp.triggered_sequence(group="qubit_awg"):
        ops.x90(q)  # Already inserted once in the active builder.
        ops.z90(q)  # Virtual Z. Subsequent spin drives inherit the updated frame.
        add(saved)  # Insert a copy of the saved Section.
        ops.clock(q, rotation="pi2", phase=0.3)
```

A `@qubit_operation` body emits DSL and returns `None`. The wrapper collects its node subtree and returns a read-only Section. In an active builder it also inserts the Section once. Calling `add(ops.x90(q))` there would insert twice.

Standard operations include `x90/xm90/y90/ym90`, `x180/xm180/y180/ym180` and `z90/z180`. Override `amplitude`, `duration`, `frequency`, `phase`, `waveform` or `waveform_parameters` for one call without editing the qubit. `phase` sets the pulse's base phase and the associated frame offset still applies. A clock operation must specify `rotation="pi"` or `"pi2"`. Compose a custom operation for more complex coherent transfer. Compiled metadata records effective pulse settings and the qubit's fingerprint and revision.

For `waveform="flat_top_gaussian"`, set `sigma` and `sample_interval` in `waveform_parameters`, optionally with `cutoff_sigma`. Total π or π/2 duration includes both Gaussian edges and must accommodate them. Pulses must fit the sample grid. The sequence compiler satisfies the 32-sample segment-length constraint across the complete sequence, so each individual gate does not need padding to that length.

## Global fields and independent addressing

If one output is declared `sharing="global"` and several qubits map to it, the operation can explicitly name the calibration source.

```python
ops.global_x90(q0, q1, calibration=q0)
```

This emits one waveform using q0's RF parameters and control phase, and records that the field acts on both q0 and q1. It neither merges their calibrations or frames nor certifies the same physical rotation on q1. Global operations accept the same per-call overrides as `x90`. Select a different drive explicitly, for example `QubitOperations(drive="clock")`.

The descriptor already identifies the covered signals. For an ordinary `play` or gate the targets can be inferred from that wiring.

```python
frequency = q0.parameters.rf.frequency * Hz + detuning
play(q0.rf, pi2_pulse, amplitude=q0.parameters.rf.pi2_amplitude, frequency=frequency)
```

Before scheduling, the compiler identifies the affected registered qubits, records `global_operation` and accounts for the shared output once. The source signal selects the calibration. `LabConfig` adds the relevant registered participants for compilation while preserving the original Experiment and excluding unrelated qubits. An unregistered signal owner is an error. Explicit `targets` are optional checks and must cover exactly the configured participants.

The introductory Ramsey tutorial registers and observes only q0. Although the RF field is global, the compiler can record other affected atoms only when their signals are registered and mapped.

## Control frames

RF and Raman default to the `"spin"` frame associated with `q.frame`. Other roles default to `None`. In the example, the clock refers to `q.phase_frame("clock_control")`. Retrieve the association with `q.drive_frame("clock")`. Each frame has one offset F.

`rz(q, angle)` is a logical nuclear-spin operation. A clock drive gets no implicit quantum Rz interpretation. `shift_phase` updates its selected frame. `reset_frame` clears that frame's entire offset. `reset_phase` also clears inherited modulation offset and re-establishes the carrier reference of the outputs associated with the frame. It cannot interrupt an active pulse. Section entry, reuse, nesting and exit preserve the frame.

Separate control frames do not establish independent physical levels. For a clock operation that coherently transfers a nuclear-spin superposition, define pulse phases, state mapping and phase compensation in the operation. A drive name is insufficient to infer multilevel phase propagation.

## Edit calibration and capture an experiment

```python
q.parameters.rf.pi2_amplitude = 0.24
q.parameters.clock.frequency = 81 * MHz
q.parameters.extensions["lab"] = {"detuning_hz": 500.0}
q.parameters.extensions["lab"]["detuning_hz"] = 750.0
```

Assignment validates the change and updates the qubit's revision and fingerprint. A failed edit preserves the old value. A saved view such as `rf = q.parameters.rf` continues to read the current values. Standard fields check units, ranges and spelling. Extensions accept ordinary JSON data with units agreed by the lab. Their values affect output only when the experiment reads them. UID, signal roles and wiring identity are fixed.

`Experiment(..., [q])`, Sections and compiled products hold independent snapshots. Editing q after construction leaves an existing experiment reproducible. Build it again to capture the new calibration. `LabConfig` checks the registered signal identity, uses the experiment's calibration, and derives frame associations from that experiment.

For simple persistence use `json.dumps(q.to_dict())` and `Qubit.from_dict(...)`. New-format objects loaded this way are editable. `q.snapshot()` explicitly obtains a read-only copy. Legacy `Qubit("q0", QubitParameters(...))` instances retain their read-only behavior.

## Named pulses and experiment factories

```python
pi2_pulse = Square(
    duration=q.parameters.rf.pi2_duration,
    amplitude=q.parameters.rf.pi2_amplitude,
)
frequency = q.parameters.rf.frequency * Hz + detuning
# One play inside the experiment. The other reuses pi2_pulse with its own phase.
play(q.rf, pi2_pulse, frequency=frequency, phase=phase)
```

In a Ramsey factory the two RF plays share `pi2_pulse`. Construct it from the π/2 amplitude and duration. Compute the actual RF frequency once as `q.parameters.rf.frequency * Hz + detuning`. Both calls use it, with a phase override for the second pulse. Passing `detuning` again to `play` would apply it twice. There is no need for another generic `rf.amplitude` or two copies of the pulse. The clock pulse can use the qubit's frequency, and the first pulse defaults to zero phase. The lower-level scalar `detuning` argument remains supported.

After editing amplitude, duration or shape, rebuild the named pulse, rebuild the experiment, compile it and regenerate its report. A frequency-only edit requires rebuilding and compiling the experiment, but the named envelope can be reused. Ordinary functions and `@experiment` capture values in the same way. Recompiling an existing experiment uses its captured values.

The [Ramsey tutorials](tutorials/README.md) compile outside the experiment functions. `ramsey(q0, detuning, tau, pi2_pulse, phase, ...)` sweeps only `tau`. `clock_ramsey(q0, detuning, clock_pulse, pi2_pulse, phase, ...)` sweeps only the second-pulse `phase` and takes the separation from `clock_pulse` duration. Other control arguments are scalar. The factory declares which axis to scan. Older pulse amplitude/frequency defaults still work, and standard `QubitOperations` can read the calibration for a named gate. Custom operations can take a named pulse directly.

## Keep a calibration history

Use `QubitStore` when you need versioned records, proposals and atomic persistence. The store holds read-only records. `store.update` returns a new version and preserves the earlier record and the working qubit supplied to the store. Save explicitly with `store.save(path)`, or use `update(..., persist=True)` after configuring a path. A proposal checks its base revision and fingerprint.

Update paths may be `rf.pi2_amplitude` or `parameters.rf.pi2_amplitude`. Extensions may introduce new dictionary namespaces. Unknown drive fields are rejected. To edit a stored record independently, use `Qubit.from_dict(store["q0"].to_dict())`.

The current qubit format is schema 3 and includes signals, parameters and position. Editability is an in-memory property and has no effect on file format or historical fingerprints. The store reads schemas 1, 2 and 3. Loading preserves old files and maintains revision checks for existing proposals.

## Read older configurations

`Qubit("q0", QubitParameters(...))` retains its original defaults and serialized form. Fields such as `params.rf`, `params.raman`, `params.drives`, `drive_frames` and `transitions` remain part of that data model.

| Existing spelling | New experiment spelling |
| --- | --- |
| `q.params.rf` | `q.parameters.rf` |
| `q.params.drives["clock"]` | `q.parameters.clock` |
| `q.drive("clock")` | `q.parameters.clock` |
| `q.signal("clock")` | `q.clock` (the method remains useful for dynamic lookup) |
| `q.params.position` | `q.position` |
| `drives.clock.pi_duration` update path | `clock.pi_duration` on new objects |

Older qubits also expose a read-only `q.parameters` view without changing their serialized format. `q.drive` reads parameters. `q.rf`, `q.clock` and analogous attributes identify signals. For laboratory fields called pi_length and pi_ampl use `pi_duration` and `pi_amplitude`.

## Two-qubit operations

Basic Ramsey experiments need no CZ implementation. To define one, supply `QubitOperations(cz_impl=my_cz)` with a `@qubit_operation` or ordinary DSL helper. `ops.cz(q0, q1)` returns a Section with both targets. Calling it without an implementation raises an error. The library provides no default physical CZ waveform.

`PairCalibration` records ordered endpoint roles, implementation name, parameters, local phase compensation, provenance and revision. A `calibrated` record must pin both qubits' revisions and fingerprints. Stale snapshots and reversed endpoints are rejected in `pair_calibrations`. The `cz_impl` owns local compensation. The wrapper records it without applying it a second time. `revised(expected_revision=..., parameters=...)` returns a new, unverified version. Save/load supports atomic file publication. A `status="demonstration"` record denotes an example calibration.
