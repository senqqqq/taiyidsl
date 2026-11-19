# Lab configuration, optical settings and experiment construction

The initialization and Raman sideband cooling (RSC) examples share the same configuration and snapshot interfaces.

## Three inputs to a compiled experiment

| Object | What it holds | Where it is used |
|---|---|---|
| `LabConfig` | Devices, shared resources, logical signal mappings, trigger sequences, device calibration and backend | Binds the experiment to hardware during compilation |
| `OpticsConfig` | Shared optical working points, default pulse lengths and wait times | Validated parameter table loaded from a dictionary or JSON |
| `Experiment` | Qubit and parameter snapshots, scan, averages and timing | Composes operations written as Python functions |

A logical signal connects the experiment to a lab. For example, the settings specify the frequency and amplitude of `optics.pump_556` and the lab assigns that signal to a DDS. Urukul channels belong in the lab. Execution order belongs in the experiment.

```python
from taiyidsl import LabConfig, OpticsConfig, Qubit
from examples.state_initialization import build_initialization_scan

q0 = Qubit("q0")
lab = LabConfig.load("examples/config/labs/initialization.json", qubit_id=q0.uid)
optics = OpticsConfig.load("examples/config/optics/initialization.json")
exp = build_initialization_scan(q0, optics, shots=100, averaging="cyclic")
compiled = lab.compile(exp)
```

A lab can compile several experiments if its mapping and devices meet their requirements. The two offline examples have different wiring. Initialization maps push-out to `urukul0_ch1` and RSC uses `urukul1_ch1`. Confirm both mappings before using one physical apparatus for these experiments.

## Optical working points

A pump-only configuration is sufficient for an experiment that requires just the pump:

```python
optics = OpticsConfig.from_dict(
    {
        "schema_version": 2,
        "revision": "illustrative-v1",
        "pump": {
            "signal": "optics.pump_556",
            "frequency_hz": 80_000_000,
            "amplitude": 0.1,
            "duration_s": 20e-6,
            "setup_wait_s": 20e-6,
            "settle_s": 20e-6,
        },
    }
)
data = optics.to_dict()  # independent plain dictionary, serializable with json.dumps(data)
```

Schema 2 uses shallow groups named `pump`, `pushout`, `beams`, `raman`, `imaging` and `camera`. Supply the groups your experiment needs. The top level also holds the version and optional `conditions`. Frequencies, durations and phases use Hz, s and rad. `amplitude` is a fraction of RF output from 0 to 1.

Use `extensions` for small amounts of laboratory-specific data. Values in `OpticsConfig` must round-trip through JSON. Store durations as SI numbers in `_s` fields. Python operation calls can instead use quantities such as `50 * us`. `Experiment(parameters=dict)` also accepts quantities and validates and copies them into its snapshot.

`pump` and `pushout` directly specify the signal, frequency, amplitude, default duration and wait windows. `beams` holds named operating points. Each `raman["x"]` pair refers to its named beams through `a` and `b`. Common Raman beams and pump parameters each have a single definition.

RF frequency must be calibrated against the optical frequency or sideband detuning at the atom. The version identifies the settings. `conditions` documents prerequisites such as field and lock status, which the laboratory must establish.

## Override a parameter for one call

```python
from taiyidsl import Experiment, us
from taiyidsl.cooling import optical_pump

exp = Experiment("override_demo", qubits=[q0], parameters=optics)
p = exp.parameters

with exp.average(shots=1):
    with exp.realtime():
        optical_pump(exp, p["pump"], amplitude=0.12)
        optical_pump(exp, p["pump"], duration=50 * us, amplitude=0.08)
        optical_pump(exp, p["pump"])  # reuse the snapshot defaults
```

The first call overrides amplitude. The second overrides duration and amplitude. Frequency, phase and waits come from the working point. The third call reuses the snapshot defaults. Explicit `amplitude=0` sets a zero amplitude. `duration=0 * us` produces a zero-pump control without a switching glitch.

`optical_pump` accepts `duration`, `amplitude`, `frequency`, `phase`, `setup_wait` and `settle`. Quantities and sweeps must have the right dimensions. Invalid values and misspelled keywords raise errors. The operation IR records the effective parameters. Compiled pulses expose their point-specific values and the base snapshot retains the defaults.

DDS changes may require a preparation window. The lab and adapter supply the configuration budget. Schedule sufficient time explicitly, or compilation fails.

To change the defaults for several parameter groups throughout one experiment, merge ordinary dictionaries before construction:

```python
exp = build_cooling_scan(
    q0,
    parameters={
        **optics,
        "pump": {**optics["pump"], "duration_s": 50e-6},
        "pushout": {**optics["pushout"], "amplitude": 0.25},
    },
)
```

Every operation that reads those groups in the new experiment uses the new values. The original optics remain independent. To change just one invocation, pass the override to that operation.

## Describe the sequence in Python

The RSC call specifies axis order, cycles per axis and round count:

```python
sideband_cool(
    exp,
    p,
    axes=("x", "y", "z"),
    cycles_per_axis=2,
    rounds=3,
)
```

The parameter table holds per-axis frequencies, amplitudes and pulse widths. Stage order, cycles, rounds and readout choice live in code. Operations expand to `serial`, `repeat`, `parallel`, `play` and `delay`, and a Python function can compose more elaborate schedules.

| Readout function | Sequence |
|---|---|
| `trigger_readout(exp, q, p, pushout=...)` | Push-out or equal-duration idle, camera trigger, then wait. Used for initialization. |
| `illuminated_readout(exp, q, p, illumination=(...), pushout=...)` | Push-out or equal-duration idle, simultaneous illumination and camera trigger, then wait. Used for RSC. |

`illumination` selects named beams. The experiment explicitly chooses a readout operation. The laboratory integration handles image transfer, loading status and calibrated spin discrimination.

## Snapshots and validation

Both `Experiment(parameters=optics)` and `Experiment(parameters=ordinary_dict)` create independent, read-only snapshots. The core experiment accepts a general mapping. Later edits to the source dictionary or a new `OpticsConfig` version apply to subsequent experiments. The optical builders normalize ordinary dictionaries to `OpticsConfig`. The experiment copy preserves that type and its immutable contents. `OpticsConfig.coerce()` can reuse an already validated immutable configuration.

A whole parameter table supplied to `sideband_cool` or a readout operation must include `schema_version=2` and valid groups. Update any unversioned partial table used at those entry points. `optical_pump` and `raman_pulse` also accept individual pulse or beam dictionaries and check their structure.

| Stage | Checks |
|---|---|
| Configuration loader | Fields, static ranges and named references |
| DSL constructors | `Square` and `delay` check finite values, units and scan values. `repeat` checks its repetition count. |
| Composite operations | Axis selection, readout choice and other composition rules. Internal timing helpers reuse parsed settings. |
| Compiler | Time grid, shared resources, configuration windows and acquisition indexing |

`metadata` holds descriptions and protocol labels. Use the parameter snapshot for working points. In a round scan, `with exp.sweep(count) as cnt` and `sideband_cool(..., rounds=cnt)` retain the reference until each point is bound. Readout branches use `case`. [Raman sideband cooling](Yb171_raman_sideband_cooling.md)

`Qubit.params` holds local gate calibrations, `OpticsConfig` supplies shared beam values and the lab holds device-path delays and output conversions. No implicit precedence rule connects these three stores.

## Migrate an older configuration

Convert old `RecipeSet` and `PulseRecipe` containers and the required `cooling/readout` aggregate into the appropriate schema 2 groups. Merely changing the version tag leaves the old structure invalid. The reference settings are [initialization.json](../examples/config/optics/initialization.json) and [rsc.json](../examples/config/optics/rsc.json).

Both builders accept `qubit, parameters, *, ... shots, averaging`. Pass an `OpticsConfig` or a mapping with equivalent groups. The RSC builder additionally takes the axis order and repetition counts. `LabConfig` JSON loading, `TriggerSequence`/`TriggerStep`, compilation and export keep their existing interfaces. `Protocol`/`ProtocolStep` are compatibility names for trigger specifications, independent of the retired recipe containers.

## Device initialization and validation

The exporter selects devices from actual physical events across all scan points. Only the selected devices receive initialization or shutdown commands. When a used DDS shares a bus or update group with unused channels, prepare it externally and declare `prepared_shared_resources`. A real lab profile must list every channel that could be affected by shared operations. A core reset requires exclusive access to the entire RTIO domain. Establish electrical isolation and concurrent experiment support separately.

The frozen point-by-point fixtures retain the earlier behavior. The original 30-point fixture supports comparison across two configuration changes. `output/optics_refactor_equivalence.json` records comparisons under the two averaging modes when generated. Each example's `verification.json` independently compares mock events from parameterized and expanded exports.

Real ARTIQ compilation, physical effects of board initialization, RTIO throughput, optical synchronization and cooling performance require independent verification.
