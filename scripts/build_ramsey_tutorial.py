"""Build a Ramsey lesson with editable, fully inline declarations."""

# Tutorial prose and tables are intentionally not wrapped inside string literals.
# ruff: noqa: E501
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "examples/artiq_spectrum/ramsey_lab"
cells = []


def md(source):
    cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))


def code(source):
    cells.append(nbformat.v4.new_code_cell(textwrap.dedent(source).strip()))


md(r"""
# Ramsey and clock Ramsey from wiring to output samples

**Operation-based lesson. First-time users can start with the [plain DSL version](ramsey_clock_plain.ipynb).** Follow the same setup through two experiments.

| Experiment | Gate sequence | Sweep |
|---|---|---|
| Ramsey | RF π/2, dark interval, RF π/2 at zero additional phase | Dark time 1, 10 or 100 µs |
| Clock Ramsey | RF for 100 µs, clock for 200 µs, RF for 100 µs | Second RF phase 0°, 45°, 90°, 135° or 180° |

The clock carrier is 200 kHz. The example π and 2π durations are 100 and 200 µs. Both experiments drive RF at 31 kHz, the 30 kHz setting plus 1 kHz detuning.

Work through the devices, wiring, qubit settings, native ARTIQ stages, experiment definition and waveform inspection in order. All inputs are editable. Run All performs offline compilation. The GreenMOT TTL fixture illustrates timing, and the camera stage emits a trigger. Atom preparation, optical calibration and image retrieval require laboratory implementations.

From the repository root run `uv sync --locked --group notebooks`, then `uv run --locked --group notebooks jupyter lab`. Select the project CPython 3.12 kernel. Real ARTIQ compilation and execution use a separate toolchain.
""")
code("""
from pathlib import Path
from IPython.display import Markdown, display

from taiyidsl import (
    DeviceSetup, Experiment, LabConfig, NativeKernelPackage, Qubit,
    QubitOperations, Square, SweepParameter, Hz, deg, delay, kHz, ms, ns, play,
    qubit_operation, reset_phase, us, experiment, average, sweep, realtime, triggered_sequence,
)

ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src/taiyidsl").is_dir())
OUTPUT = ROOT / "output/simple_ramsey"
OUTPUT.mkdir(parents=True, exist_ok=True)

""")
md(r"""
## 1. Device databases and connection addresses

`device_db` is the ARTIQ device database. The small dictionary below provides a complete offline example. Retain the existing laboratory database for the MOT. The example core host `192.0.2.10` is a documentation address. A TTL channel is a gateware RTIO channel number. MOT/camera and the two AWG triggers use distinct lines. Supplying an address merely describes the connection. Hardware access requires explicit connection with emulation disabled.
""")
code((LAB / "device_db.py").read_text(encoding="utf-8"))
md(r"""
### Reusing a laboratory MOT device database

Keep the original device database, including its core, DDS, Zotino, TTL and controller entries.

1. Identify two wired TTLs unused by MOT or readout and check them against the existing device database.
2. Reference their real names in the descriptor, for example `{"A": "ttl8", "B": "ttl9"}`.
3. If desired, add `"awg_trigger_a": "ttl8"` and `"awg_trigger_b": "ttl9"` as aliases to these lines.
4. Add a channel entry only when the installed hardware and gateware provide that channel.

```python
setup = DeviceSetup.from_descriptor(
    descriptor,
    device_db="C:/my_lab/device_db.py",  # Existing MOT device database
    spec_db="C:/my_lab/spec_db.py",
)
```

The loader reads declarative assignments. If the laboratory database builds entries using loops, imports or functions, evaluate it in the trusted configuration environment and pass the resulting dictionary. The exported DSL program and native components ultimately share the same database and managers. [ARTIQ device databases and aliases](https://m-labs.hk/artiq/manual/environment.html#the-device-database)
""")
md(r"""
`spec_db` is the TaiyiDSL configuration consumed by the Spectrum `spcm` adapter. A enables one output and B enables two. Their wiring UIDs are A and B, while the serial numbers identify physical cards. The local PCIe driver addresses are `/dev/spcm0` and `/dev/spcm1`. Replace `SIM_A` and `SIM_B` before using real cards. A configured remote service can expose an SDK resource such as `TCPIP::192.168.1.10::inst0::INSTR`. The analog output range `output_range_mv` and normalized gate amplitude `pi_amplitude` set different quantities. [Spectrum connection identifiers](https://github.com/SpectrumInstrumentation/spcm/blob/master/src/examples/README.md)
""")
code((LAB / "spec_db.py").read_text(encoding="utf-8"))
md(r"""
## 2. Wiring the logical signals to physical outputs

- `instruments` references the two databases by UID.
- `channels` names laboratory lines. RF uses A output 0. Clock and Rydberg use B. Both AWG trigger lines are declared here.
- `sync_groups` assigns each trigger channel to a card. Direct device names are also supported.
- `signals` maps qubit roles to laboratory channels. Nested `q0: {rf: ...}` expands to `q0.rf`. Shared camera signals can be named at the top level. Flat mappings remain valid.

In the RF-only experiment card B plays zeros in sync with A. Clock Ramsey also uses B's clock output. AWG trigger TTLs belong to the wiring configuration.
""")
wiring_source = (
    (LAB / "wiring.py")
    .read_text(encoding="utf-8")
    .replace(
        '"nuclear_rf": {"instrument": "A", "channel": 0},',
        '"nuclear_rf": {"instrument": "A", "channel": 0, "sharing": "global"},',
    )
)
code(wiring_source)
code("""
setup = DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
display(setup.connections())
display(setup.trigger_connections())
""")
md(r"""
### Inspect the resolved DeviceSetup

`DeviceSetup` holds the device and group declarations, descriptor sources and resolved mappings. `describe()` returns a detached snapshot of the core host, card addresses and serial numbers, channels, groups and signals. Editing the returned dictionary leaves the setup unchanged. Each address is defined in its database and referenced by UID in the descriptor.
""")
code("""
display(setup.describe())
""")
md(r"""
### One observed qubit in a global RF field

This experiment registers q0. The field declaration `sharing="global"` describes its actual coverage even when there is only one observed qubit. If other qubits are registered on the same field, the compiler derives affected targets from the wiring.
""")
md(r"""
Once the wiring is stable, save the two databases as separate Python files and pass their paths. The loader reads assignments and numeric expressions statically. For dynamically generated databases, pass the evaluated dictionary explicitly. Setup derives the `SignalMap` and Spectrum groups offline.
""")
code("""
config_dir = OUTPUT / "lab_config"
config_dir.mkdir(exist_ok=True)
for name, value in (("device_db", device_db), ("spec_db", spec_db)):
    (config_dir / f"{name}.py").write_text(name + " = " + pprint.pformat(value, sort_dicts=False) + "\\n", encoding="utf-8")
setup = DeviceSetup.from_descriptor(
    descriptor,
    device_db=config_dir / "device_db.py",
    spec_db=config_dir / "spec_db.py",
)
""")
md(r"""
## 3. LabConfig, qubit roles and calibration

`LabConfig(setup=setup, qubits=[q0], emulation=True)` brings the resolved wiring and registered qubit together. Later, `lab.compile(exp)` uses this configuration offline.

### Signals and parameters on a qubit

In `signals={"rf": "q0.rf"}`, the key names the qubit's RF role and the value names its wired logical signal. Here the path is `rf role → q0.rf → nuclear_rf → A/output 0`. If a logical name changes, update the descriptor. Changing the qubit UID leaves explicitly supplied signal names intact.

`q0.rf` selects the logical signal. `q0.parameters.rf` supplies calibration values. Each drive has the same structure. Its waveform frequency may differ from the physical resonance frequency after an AOM or mixer. π and π/2 amplitudes and durations are specified independently.

For clock, the example carrier is 200 kHz, π lasts 100 µs, π/2 lasts 50 µs and the normalized amplitude is 0.2. The 200 µs square pulse illustrates a 2π rotation at unchanged Rabi rate. Actual pulse areas require calibration. Clock control has its own frame. No Rydberg gate parameters are supplied here.
""")
qubit_source = (LAB / "qubits.py").read_text(encoding="utf-8")
# Imports are shown once in the opening cell; keep the complete editable object.
code(qubit_source[qubit_source.index("q0 = Qubit(") :])
code("""
print("RF signal:", q0.rf)
print("RF π/2 amplitude:", q0.parameters.rf.pi2_amplitude)
print("RF π/2 duration [s]:", q0.parameters.rf.pi2_duration)
print("Clock signal:", q0.clock)
lab = LabConfig(setup=setup, qubits=[q0], emulation=True)
""")
md(r"""
### 3.1 Edit qubit parameters

Assign a new calibration directly, for example `q0.parameters.rf.pi2_amplitude = 0.22`. Assignment validates units, ranges and standard field names. The demonstration restores the RF amplitude to 0.2 before building the experiments.
""")
code("""
original_amplitude = q0.parameters.rf.pi2_amplitude
q0.parameters.rf.pi2_amplitude = 0.22
print("Updated π/2 amplitude:", q0.parameters.rf.pi2_amplitude)

# Use a dictionary for laboratory-specific parameter extensions.
q0.parameters.extensions["lab"] = {"clock_detuning_hz": 500.0}
q0.parameters.extensions["lab"]["clock_detuning_hz"] = 750.0
print("Added parameters:", q0.parameters.extensions["lab"])

# This extension is illustrative. The clock uses its own configured frequency.
q0.parameters.rf.pi2_amplitude = original_amplitude
assert q0.parameters.rf.pi2_amplitude == 0.2
""")
md(r"""
Update a standard field with an assignment such as `q0.parameters.clock.pi_duration = 100 * us`. Store laboratory-specific values as JSON data with explicit units under `extensions["lab"]`. A new drive role also needs a signal and descriptor mapping.

An experiment captures a copy of the parameters at construction. After editing an amplitude or duration saved in a pulse, rebuild that pulse and the experiment. If an RF frequency used by the experiment function changes, call the function again. The existing lab wiring remains valid.

Assignments update revision and fingerprint. Use `QubitStore` for versioned history or for applying fitted calibration proposals. The following JSON round trip saves and reloads an editable qubit without a store.
""")
code("""
qubit_file = OUTPUT / "qubit_parameters.json"
qubit_file.write_text(json.dumps(q0.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
restored_q0 = Qubit.from_dict(json.loads(qubit_file.read_text(encoding="utf-8")))
assert restored_q0.to_dict() == q0.to_dict()
""")
md(r"""
### 3.2 From qubit calibration to pulse, experiment and compilation

Both RF rotations are π/2 pulses. The experiment author explicitly selects `q0.parameters.rf.pi2_amplitude` and `pi2_duration` when constructing the named pulse.

```python
# Capture the calibrated values when constructing the pulse.
pi2_pulse = Square(
    duration=q0.parameters.rf.pi2_duration,
    amplitude=q0.parameters.rf.pi2_amplitude,
)
# Compute RF frequency in the experiment and override phase on the second play.
frequency = q0.parameters.rf.frequency * Hz + detuning
play(q0.rf, pi2_pulse, frequency=frequency)
play(q0.rf, pi2_pulse, frequency=frequency, phase=phase)
```

| Setting | Source | When it takes effect |
|---|---|---|
| Shape, duration and amplitude | Named RF or clock pulse | Pulse construction |
| RF drive frequency | Qubit RF setting plus explicit detuning | Experiment construction |
| Clock drive frequency | Qubit snapshot captured by the experiment | Resolved at compile time |
| Detuning, dark time and analysis phase | Experiment arguments | Scalars at construction, a sweep parameter per compiled point |
| Card, channel, trigger and sample grid | DeviceSetup / LabConfig | Compilation |

```text
Editable qubit → named pulse captures duration/amplitude; experiment reads frequency and adds detuning
Named pulse + function arguments
      ↓ ramsey(...) / clock_ramsey(...)
Experiment snapshots parameters; RF play records pulse duration/amplitude and frequency/phase overrides
      ↓ lab.compile(exp)
Bind sweep value → schedule and resolve phase → logical IR → card/channel → final DAC samples
```

Both RF calls share `pi2_pulse`. The second call alone overrides its additional phase. For any field, a `play` override takes precedence over the pulse's saved value, followed by the qubit default when the pulse field is `None`. An override leaves the original pulse and qubit intact. Rebuild the pulse after changing its saved shape, amplitude or duration. Rebuild the experiment and recompile after changing calibration. A different envelope also needs a corresponding physical calibration.
""")
md(r"""
### Assemble the laboratory configuration

```text
LabConfig
├─ backend：artiq_spectrum
├─ emulation：True
├─ setup: DeviceSetup (device facts, wiring, sync groups and qubit frame bindings)
├─ qubits: registered qubits and wiring; the experiment captures calibration for each run
├─ signal_map: derived from the descriptor
└─ artiq_python: separate ARTIQ interpreter for hardware execution
```

`lab.describe()` includes the wiring, connection sources, registered qubits and emulation setting. Earlier `lab.to_dict()` output describes the legacy compile configuration. No connection is opened when inspecting either snapshot.
""")
code("""
lab_view = lab.describe()
display(lab_view)
""")
md(r"""
This lesson uses q0 as the calibration source and RF frame reference. Experiment construction snapshots its current settings.
""")
md(r"""
## 4. Register native ARTIQ methods

The example reuses `GreenMOTFixture`. Registration reads and pins source with its static dependencies. `.kernel(...)` creates a DSL proxy for a future call in the exported kernel.

`prepare_MOT()` combines a variable-duration prefix and a fixed preparation tail. The tail's actual return cursor must match its declaration. The legacy package's `finalize_method` runs at the end of every shot.
""")
code("""
package = NativeKernelPackage.from_package(
    root=ROOT / "examples/artiq_spectrum/greenmot_native",
    entry="greenmot_fixture:GreenMOTFixture",
    resources=("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"),
    initialize_method="initialize_native",
    finalize_method="finalize_native",
)
prepare_prefix = package.kernel("prepare_MOT", timing="prefix")
prepare_tail = package.kernel(
    "finish_preparation", timing="timed", duration=15 * ms + 16 * ns,
    end_boundary="Fixture TTL falling edge; no analog preparation model",
)
readout = package.kernel("execute_readout", timing="suffix", duration=100.48 * ms)

def prepare_MOT():
    prepare_prefix()
    prepare_tail()
""")
md(r"""
The experiment calls `prepare_MOT()` and `readout()` directly, so preparation, control and readout remain visible. This fixture calls `core.reset()` during initialization and performs per-shot cleanup. New independent cooling or camera components can use `NativeModule.from_package` and `NativeProgram`, whose initialize/finalize hooks run once per session and after_shot once per shot. Saving a module as `my_lab/native.py` makes `from my_lab import native` and `native.prepare_MOT()` available. [Native components](../native_components.md) · [Example laboratory package](../../examples/lab_extension/README.md)

To use the real GreenMOT source, pass its path to `GreenMOTAdapter` and supply its preparation and readout callbacks. It checks that splitting the original code preserves operation order, with a variable prefix, a 15 ms + 16 ns fixed tail and per-shot cleanup. The digital tail ends at the Zotino LDAC edge. Measure analog settling to choose `preparation_to_gate`. Declare resources and methods explicitly for other classes. Place native calls at the preparation/readout boundaries of realtime. Strict timed/suffix stages prohibit cursor reset, break or unbounded waits.
""")
md(r"""
### Native camera trigger and declared measurement

Both versions keep `prepare_MOT` and `readout` as native DSL proxies. `measure(q0)` and `acquire(...)` require a readout protocol, signal mapping and result adapter. This example emits the camera trigger once. A laboratory result adapter must retrieve the image.

## 5. Reusable operations and Section

Give each pulse a name before building the experiment. Both RF gates reuse the same π/2 pulse carrying its calibrated duration and amplitude. The function sets the drive frequency and applies an extra phase to the second RF call.
""")
code("""
# After editing amplitude or duration, rebuild the pulse and experiment.
pi2_pulse = Square(
    duration=q0.parameters.rf.pi2_duration,
    amplitude=q0.parameters.rf.pi2_amplitude,
)
clock_pulse = Square(
    duration=2 * q0.parameters.clock.pi_duration,
    amplitude=q0.parameters.clock.pi_amplitude,
)
print("RF π/2 pulse:", pi2_pulse)
print("Clock 2π pulse:", clock_pulse)
""")
md(r"""
### What to rerun after changing calibration

Assign to the existing object, for example `q0.parameters.rf.pi2_amplitude = 0.25`. Recreating q0 would restore its original values.

| Change | Cells to rerun, in order |
|---|---|
| RF π/2 or clock π amplitude/duration | Pulse construction, experiment call, compile, report and plots |
| RF or clock frequency | Experiment call, compile, report and plots |
| Detuning, dark time, phase or shots | Edited experiment call and the subsequent compile/report cells |

Section 6 constructs `exp` and `compiled`. Section 6.1 constructs the decorator form and compares it with the plain function. Section 7 exports the report and plots. Section 8 constructs, compiles and reports clock Ramsey. Refresh both compiled objects before comparing them.

Changing parameters on the same q0 requires no new function, gate definition or lab. Replacing q0 itself requires rerunning the decorator definition to bind the new object. Recompiling an old experiment continues to use its captured parameters and pulses.
""")
md(r"""
The `global_rf_pi2` operation takes q0, a named pulse and the drive frequency computed by the experiment. Each call plays RF once, and the second can specify a phase. The compiler derives affected targets from the wiring. The decorator constructs a Section and inserts it once if a builder is active. The `ramsey` function in the next section returns the complete Experiment.
""")
code("""
@qubit_operation
def global_rf_pi2(q0, pulse, *, frequency, phase=0 * deg):
    play(q0.rf, pulse, frequency=frequency, phase=phase)

gate = global_rf_pi2(q0, pi2_pulse, frequency=q0.parameters.rf.frequency * Hz)
print("Gate operation returns:", type(gate).__name__)
# Outside a context use exp.add(gate). This experiment calls global_rf_pi2 directly.
""")
md(r"""
Instructions run in sequence by default. For independent outputs use `section(..., schedule="parallel", align="start"/"end", qubits=(...))`. Each direct child is a branch and a compound branch retains its internal ordering. A shared output remains a shared resource. `layer()` wraps the parallel form. `barrier()` sets a synchronization point in ASAP schedules. Sections and hardware segments retain the current frame. This experiment explicitly calls `reset_phase` at the gate sequence entrance.
""")
md(r"""
## 6. Build the full Ramsey experiment

Read through preparation, sweep, gate sequence and readout. The dark time `tau` runs from the end of the first pulse to the start of the second. Both RF pulses use `q0.parameters.rf.frequency * Hz + detuning`, here 30 kHz + 1 kHz = 31 kHz. The compiler uses that final frequency to compute carrier phase and DAC samples. The reference advances during the dark time even when DAC output is zero. Apply `second_phase` only on the second pulse. Applying an extra `2π·detuning·tau` here would count the same detuning twice. This scan uses 1, 10 and 100 µs and zero additional phase.
""")
code("""
def ramsey(q0, detuning, tau, pi2_pulse, phase=0 * deg, *,
           prepare, readout, shots=2, preparation_to_gate=10 * us,
           group="qubit_awg"):
    # Both RF pulses share this final drive frequency.
    frequency = q0.parameters.rf.frequency * Hz + detuning
    # Only tau is swept. Detuning and phase are scalar.
    exp = Experiment("greenmot_ramsey", [q0], metadata={"hardware_verified": False})
    with exp.average(shots):
        with exp.sweep(tau):
            with exp.realtime():
                prepare()
                delay(preparation_to_gate)
                with exp.triggered_sequence(group=group):
                    reset_phase(q0.phase_frame("spin"))
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency)
                    delay(q0.rf, tau)
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)
                readout()
    return exp
""")
md(r"""
Only `tau` is a SweepParameter. Detuning and phase are scalars. Preparation, readout, shots and group are explicit function arguments. The call returns an Experiment for subsequent offline compilation.
""")
code("""
delays = SweepParameter("tau", [1, 10, 100], "us")
exp = ramsey(
    q0=q0, detuning=1 * kHz, tau=delays, pi2_pulse=pi2_pulse,
    phase=0 * deg, prepare=prepare_MOT, readout=readout, shots=2,
)
""")
md(r"""
After editing amplitude or duration, rebuild `pi2_pulse` and call `ramsey` again. With a frequency or detuning change, only a new experiment and compilation are needed. Each playback captures its phase independently.
""")
code("""
compiled = lab.compile(exp)
print("Scan points:", len(compiled.points), "shots/point:", compiled.shots)
display(compiled.axes)
rf_events = [e for e in compiled.points[0].logical_events if e.kind == "play"]
display([{"pulse": i + 1, "base_frequency_Hz": q0.parameters.rf.frequency,
        "detuning_Hz": e.params["frequency"] - q0.parameters.rf.frequency,
        "compiled_frequency_Hz": e.params["frequency"]}
       for i, e in enumerate(rf_events)])
assert rf_events[0].params["frequency"] == rf_events[1].params["frequency"]
""")
md(r"""
### 6.1 Build the same Ramsey with @experiment

The plain function constructs and returns an experiment explicitly. The decorator creates a new experiment on each invocation and records the DSL calls in its body. `@qubit_operation` produces a Section and `@experiment` produces an Experiment.

`@experiment(qubits=[q0])` binds the q0 object and snapshots its values at invocation. The free `average`, `sweep`, `realtime` and `triggered_sequence` contexts use that active experiment. To replace q0 with another object, bind the decorator again or pass the new qubit to the ordinary function.
""")
code("""
@experiment(qubits=[q0], name="greenmot_ramsey", metadata={"hardware_verified": False})
def ramsey_decorated(detuning, tau, pi2_pulse, phase=0 * deg, *,
                     prepare, readout, shots=2, preparation_to_gate=10 * us,
                     group="qubit_awg"):
    frequency = q0.parameters.rf.frequency * Hz + detuning
    with average(shots):
        with sweep(tau):
            with realtime():
                prepare()
                delay(preparation_to_gate)
                with triggered_sequence(group=group):
                    reset_phase(q0.phase_frame("spin"))
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency)
                    delay(q0.rf, tau)
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)
                readout()

exp_decorated = ramsey_decorated(
    detuning=1 * kHz, tau=delays, pi2_pulse=pi2_pulse, phase=0 * deg,
    prepare=prepare_MOT, readout=readout, shots=2,
)
compiled_decorated = lab.compile(exp_decorated)
print("Decorator returns:", type(exp_decorated).__name__)
print("Qubits in the compiled snapshot:", list(compiled_decorated.qubits))
""")
md(r"""
Compare each point's timing, π/2 settings, DAC segment hashes and playback step table. Source locations may differ between authoring styles.
""")
code("""
def waveform_signature(result):
    signature = []
    for point in result.points:
        timing = [(e.kind, e.signal, e.start_mu, e.duration_mu) for e in point.logical_events]
        rf = [(e.params["amplitude"], e.params["frequency"], e.params["phase"])
              for e in point.logical_events if e.kind == "play"]
        programs = []
        for event in point.physical_events:
            if event.kind == "spectrum_program":
                for card, item in event.params["cards"].items():
                    program = item["program"]
                    programs.append((card, [(s["sample_count"], s["sha256"]) for s in program["segments"]], program["steps"]))
        signature.append((timing, rf, programs))
    return signature

assert waveform_signature(compiled_decorated) == waveform_signature(compiled)
assert set(exp.qubits) == {"q0"}  # Compilation preserves the input experiment.
assert set(compiled.qubits) == {"q0"}
print("Function and @experiment match in timing, phase and DAC samples.")
""")
md(r"""
## 7. Inspect timing, phase and final DAC samples

The report reads the compiled artifact and decodes the final packed DAC samples. Native ARTIQ calls are shown with duration and source. Their analog outputs and the atom response require separate measurement.
""")
code("""
report = compiled.export_report(OUTPUT / "notebook_report")
display(Markdown(f"[Open the compiled experiment report]({report['index'].resolve().as_uri()})"))
print("Report path:", report["index"].resolve())
""")
md(r"""
### Segment length and the 32-sample grid

At 1.25 GS/s the sample interval is 0.8 ns. Segment lengths per channel must be multiples of 32. The minimum is 384 samples with one enabled channel and 192 with two. The compiler segments the complete gate timeline and pads the trailing zero output. The Ramsey dark time remains as requested. The report distinguishes the end of the logical gate and the end of physical playback. An independent camera resource can begin readout while a card finishes, but uploads and re-triggers must wait for readiness.
""")
code("""
point_index = 2  # tau=100 us. The HTML viewer also exposes the 1 and 10 us points.
point = compiled.points[point_index]
group_event = next(e for e in point.physical_events if e.kind == "spectrum_program")
rows = []
for card, item in group_event.params["cards"].items():
    program = item["program"]
    rows.append({
        "card": card,
        "active_channels": item["contract"]["active_channels"],
        "segment_lengths": [s["sample_count"] for s in program["segments"]],
        "tail_samples": program["tail_samples"],
        "tail_ns": program["tail_samples"] * 0.8,
    })
    assert all(s["sample_count"] % 32 == 0 for s in program["segments"])
display(rows)
""")
md(r"""
### A common time axis for all cards

Inspect one complete shot at the 100 µs dark-time point, including both π/2 pulses and final padding. Time zero is the sequence entrance. Each card's trigger-compensation prefix is included. B stays at zero throughout this RF-only experiment. Preserve idle intervals on all cards even when the segment boundaries differ. Measured analog alignment also requires a shared reference and calibrated per-channel delays and phases.

The cell embeds the plot and writes `output/simple_ramsey/ramsey_dac.png`. The interactive waveform viewer joins multiple point/shot windows in execution order. Click a legend item to toggle its channel and zoom to load exact samples. Overview min/max bins are computed from the final DAC data.
""")
code("""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from IPython.display import Image

from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window

def plot_dac_sequence(compilation, point_index, image_path, title):
    # Decode both cards against the gate entry, including all idle intervals.
    point = compilation.points[point_index]
    event = next(e for e in point.physical_events if e.kind == "spectrum_program")
    region = next(e for e in point.logical_events if e.kind == "spectrum_region")
    tick = compilation.setup.tick_seconds
    gate_start = region.start_mu * tick
    fig, ax = plt.subplots(figsize=(11, 3.8))
    for card, item in event.params["cards"].items():
        contract, program = item["contract"], item["program"]
        rate = contract["sample_rate_hz"]
        sample_zero = event.params["trigger_relative_mu"] * event.params["trigger_time_unit_s"] + contract["trigger_latency_samples"] / rate
        count = program["physical_duration_samples"]
        # These short examples have at most ~500k samples/card; no downsampling.
        dac = reconstruct_window(program, 0, count)
        t_us = (sample_zero + np.arange(count) / rate - gate_start) * 1e6
        for column, channel in enumerate(contract["active_channels"]):
            ax.plot(t_us, dac[:, column] / 32767, label=f"{card}: output {channel}", linewidth=0.9)
    pulses = [e for e in point.logical_events if e.kind == "play"]
    for index, pulse in enumerate(pulses):
        start_us = (pulse.start_mu * tick - gate_start) * 1e6
        end_us = ((pulse.start_mu + pulse.duration_mu) * tick - gate_start) * 1e6
        ax.axvspan(start_us, end_us, color="C0", alpha=0.05)
        ax.text((start_us + end_us) / 2, 0.23, f"pulse {index + 1}", ha="center")
    ax.set(xlabel="Time from gate sequence start [us]", ylabel="Normalized DAC", title=title + " — compiled samples, not measured", ylim=(-0.25, 0.29))
    ax.legend(loc="lower center", ncol=3)
    fig.tight_layout()
    fig.savefig(image_path, dpi=130)
    plt.close(fig)
    display(Image(filename=str(image_path)))

plot_dac_sequence(compiled, point_index, OUTPUT / "ramsey_dac.png", "Ramsey: RF 100 us / dark 100 us / RF 100 us")
plot_dac_sequence(clock_compiled, 0, OUTPUT / "clock_ramsey_dac.png", "Clock Ramsey: RF 100 us / clock 200 us / RF 100 us; second phase 0 deg")
""")
md(r"""
### How the Spectrum settings reach the SDK

```text
spec_db (address, serial number, sample rate, reference clock, channels, range)
    → DeviceSetup.from_descriptor: resolve SpectrumCard settings and address
    → LabConfig.compile: generate waveforms, segments, steps and per-card contracts offline
    → lab.connect: ARTIQ worker opens devices and verifies identity and clock
    → session.run: preflight all cards → configure outputs/triggers → upload → all ready → synchronous ARTIQ trigger
```

`Worker.connect()` in `src/taiyidsl/runtime/worker.py` uses the `SpcmAdapter` in `src/taiyidsl/runtime/spectrum/awg.py`. The adapter drives the official `spcm.Card/Clock/Channels/Trigger/Sequence` API. Its configure step sets enabled outputs, ranges and triggers, and upload writes segments and steps.

Normal connection and execution verify the current clock state. Use `configure_for_calibration()` during initial setup or a clock change, then measure and record time/phase calibration for that state. Editing the sample rate or reference in a configuration file requires rechecking the calibration. This lesson uses the offline path.
""")
md(r"""
## 8. Clock Ramsey with the clock pulse between RF gates

Reuse the same devices, descriptor, qubit, preparation and readout. On card A, RF π/2 occupies 0–100 µs. On card B, clock 2π occupies 100–300 µs. Card A plays RF π/2 again at 300–400 µs. The 400 µs sequence has no extra delay, and B retains its first 100 µs of idle time.

The clock pulse uses the amplitude of the example 100 µs π pulse for a 200 µs 2π pulse. Its 200 kHz carrier and the rotation angle are separate physical quantities. For other envelopes, supply a laboratory-calibrated gate. The operation returns a Section and inserts it once in an active context.
""")
code("""
@qubit_operation
def clock_2pi(q, pulse):
    play(q.clock, pulse)


def clock_ramsey(q0, detuning, clock_pulse, pi2_pulse, phase, *,
                 prepare, readout, shots=2, preparation_to_gate=10 * us,
                 group="qubit_awg"):
    frequency = q0.parameters.rf.frequency * Hz + detuning
    # Sweep the second RF phase. The RF gap equals the clock pulse duration.
    exp = Experiment("clock_ramsey", [q0], metadata={
        "hardware_verified": False,
        "clock_2pi_assumption": "constant resonant Rabi rate; demo only",
    })
    with exp.average(shots):
        with exp.sweep(phase):
            with exp.realtime():
                prepare()
                delay(preparation_to_gate)
                with exp.triggered_sequence(group=group):
                    reset_phase(q0.phase_frame("spin"))
                    reset_phase(q0.phase_frame("clock_control"))
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency)
                    clock_2pi(q0, clock_pulse)
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)
                readout()
    return exp
""")

md(r"""
Here `clock_pulse` defines the gap between RF pulses. Only the additional phase of the second RF pulse is swept, at 0°, 45°, 90°, 135° and 180°. RF remains at 31 kHz. At zero extra phase its carrier continues along the common time axis. The spin and clock_control frames are explicitly reset on entry and evolve independently thereafter. A physical transfer of coherence between levels may require separately calibrated compensation.
""")
code("""
second_phases = SweepParameter("second_phase", [0, 45, 90, 135, 180], "deg")
clock_exp = clock_ramsey(
    q0=q0, detuning=1 * kHz, clock_pulse=clock_pulse, pi2_pulse=pi2_pulse,
    phase=second_phases, prepare=prepare_MOT, readout=readout, shots=2,
)

clock_compiled = lab.compile(clock_exp)
clock_report = clock_compiled.export_report(OUTPUT / "clock_ramsey_report")
display(Markdown(f"[Open the clock Ramsey report]({clock_report['index'].resolve().as_uri()})"))
print("Phase scan points:", len(clock_compiled.points), "shots/point:", clock_compiled.shots)
display(clock_compiled.axes)

""")
md(r"""
A/output 0 carries RF, B/output 0 carries clock and B/output 1 stays at zero. Different segment lengths and trailing padding preserve the handoffs at 100 and 300 µs.

### Read the interactive reports

The pulse sheet lays out shots in execution order and uses one lane per signal. Hover for a summary and click a block for its parameters and source. The output simulator overlays all enabled cards and channels. Toggle channels in the legend, drag to zoom, reset to the overview, and use the advanced controls for point, unit and window selection.

Both experiments average cyclically. Shot 0 visits all points before shot 1, giving six Ramsey executions and ten clock-Ramsey executions. On the common axis, each Ramsey window has two 100 µs RF pulses and a 1, 10 or 100 µs gap. In each clock-Ramsey window, RF occupies 0–100 and 300–400 µs and clock occupies 100–300 µs. The five points vary only the second RF phase. Zoom around a transition to inspect the exact samples.

The viewer concatenates known output windows. Unknown host waits, uploads and native prefixes appear as boundaries. The pulse sheet shows control windows and the output simulator shows AWG playback windows. Native signals require separate instrumentation. Each shot keeps its own calculated phase. Open the local HTML directly in the browser to check interaction.
""")
md(r"""
## 9. Connect real hardware explicitly

| Setting | Source | Required laboratory input |
|---|---|---|
| ARTIQ address and TTL/DDS/SPI topology | device_db | Current gateware-matched database |
| Card identity, channels, clocks and ranges | spec_db | Driver addresses, actual serial numbers and measured operating settings |
| Wiring and sync group | descriptor | Physical trigger-to-card and logical-signal-to-output connections |
| Gate calibration | Qubit parameters | Measured π and π/2 amplitude, duration, frequency, shape and frame |
| Time and phase alignment | Sync-group calibration | Channel delay, gain, phase and associated frequencies/hardware revisions |
| Preparation and camera | Native declarations and ARTIQ package | Verified fixed tail, per-shot cleanup and frame retrieval |

After configuring the lab, recompile and inspect the report. Compilation stays offline with `emulation=False`. The following hardware example is documentation and is excluded from Run All.

```python
# Provide laboratory wiring, calibration and native callbacks before creating pulses and the experiment.
q0 = calibrated_q0
pi2_pulse = Square(
    duration=q0.parameters.rf.pi2_duration,
    amplitude=q0.parameters.rf.pi2_amplitude,
)
lab = LabConfig(
    setup=real_setup, qubits=[q0], emulation=False, artiq_python=artiq_python,
)
exp = ramsey(q0, 1 * kHz, delays, pi2_pulse, phase=0 * deg,
             prepare=real_prepare_MOT, readout=real_readout, shots=100)
compiled = lab.compile(exp)
with lab.connect(clock_session_ids=measured_clock_session_ids) as session:
    receipt = session.run(compiled)
```

`artiq_python` selects the independent ARTIQ interpreter. The laboratory owns the clock-session ID. It is valid while the reference remains locked and wiring unchanged. Connect checks card identity and clocks. Run uploads, initializes and executes. The execution receipt records the hardware action. Image transfer requires the laboratory's result adapter. [ARTIQ toolchain](../artiq_toolchain.md)
""")
md(r"""
## 10. Save the experiment as a laboratory package

Once the wiring is established, keep it in separate modules while leaving the experiment function readable.

```text
my_lab/
    device_db.py     # ARTIQ device declarations
    spec_db.py       # Spectrum card settings
    wiring.py        # Wiring descriptor shown in this notebook
    qubits.py        # Editable qubit parameters
    native.py        # Native class declarations
ramsey.py            # Parameterized function returning Experiment
```

Save `ramsey` and `clock_ramsey` in the laboratory experiment module. The examples in `examples/artiq_spectrum/` use declarations from `ramsey_lab/` and package their own convenience interfaces. Its `experiments.py` contains the `clock_2pi` operation and the reusable `clock_ramsey_experiment` builder. From the repository root, run `uv run --locked python -m examples.artiq_spectrum.simple_ramsey` or `uv run --locked python -m examples.artiq_spectrum.clock_ramsey` to compile and export offline.

When experiments grow, automatic chunking controls bank residency, and `compile_stream` bounds the managed compilation workspace. The [advanced hybrid lesson](hybrid_from_scratch.ipynb) covers both. Real ARTIQ compilation and physical output measurements provide additional evidence beyond this software exercise.
""")


def introductory_route(lesson):
    """Keep configuration and experiment cells first; inspection details are optional."""
    main = list(lesson)
    appendices = []

    def extract(first, following):
        start = next(i for i, cell in enumerate(main) if cell.source.startswith(first))
        stop = next(
            i for i, cell in enumerate(main) if cell.source.startswith(following)
        )
        appendices.extend(main[start:stop])
        del main[start:stop]

    extract("### Inspect the resolved DeviceSetup", "### One observed qubit")
    extract("Once the wiring is stable", "## 3. LabConfig")
    extract("### 6.1 Build the same Ramsey", "## 7. Inspect timing")
    extract("### 3.1 Edit qubit parameters", "## 4. Register native")
    extract("### Segment length", "## 8. Clock Ramsey")
    intro = nbformat.v4.new_markdown_cell(
        "## Appendix: configuration, equivalence checks and static DAC plots\n\n"
        "The main lesson covers construction, compilation, reports and hardware configuration. "
        "These optional cells inspect the configuration and raw results outside the experiment function."
    )
    imports = nbformat.v4.new_code_cell("import json\nimport pprint")
    return main + [intro, imports] + appendices


def plain_variant(original):
    """Keep the same lesson and experiment signatures, expand only gate wrappers."""
    result = deepcopy(original)
    for cell in result:
        source = cell.source.replace("output/simple_ramsey", "output/plain_ramsey")
        if cell.cell_type == "code":
            source = source.replace("QubitOperations, ", "").replace(
                "qubit_operation, ", ""
            )
            if "public_gate = QubitOperations" in source:
                source = source[: source.index("public_gate =")]
                source += 'print("The global wiring illustration is separate from the single-qubit setup used here.")'
            elif source.startswith("@qubit_operation\ndef global_rf_pi2"):
                source = 'print("The plain experiment plays the named pulse directly.")'
            elif source.startswith("@qubit_operation\ndef clock_2pi"):
                source = source[source.index("def clock_ramsey(") :]
            source = (
                source.replace(
                    "global_rf_pi2(q0, pi2_pulse, frequency=frequency)",
                    "play(q0.rf, pi2_pulse, frequency=frequency)",
                )
                .replace(
                    "global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)",
                    "play(q0.rf, pi2_pulse, frequency=frequency, phase=phase)",
                )
                .replace(
                    "clock_2pi(q0, clock_pulse)",
                    "play(q0.clock, clock_pulse)",
                )
            )
        else:
            source = source.replace(
                "## 5. Reusable operations and Section",
                "## 5. Name the pulse and pass it to the experiment",
            )
            if source.startswith("The `global_rf_pi2` operation"):
                source = "The plain Ramsey functions play the same named pulse directly. Each playback uses the stored amplitude and duration, the final drive frequency and the specified analysis phase. Each call builds a new Experiment."
            source = source.replace(
                "Use `ops.global_x90(q0, q1, calibration=q0)`:",
                "The operation-based version can use `ops.global_x90(q0, q1, calibration=q0)`:",
            )
            source = source.replace(
                "The operation returns a Section and inserts it once in an active context.",
                "The following function plays the pulse directly.",
            )
        cell.source = source
    result[0].source = (
        result[0]
        .source.replace(
            "# Ramsey and clock Ramsey", "# Plain DSL · Ramsey and clock Ramsey", 1
        )
        .replace("`ramsey_clock_walkthrough.ipynb`", "`ramsey_clock_plain.ipynb`")
        .replace("Operation-based lesson", "Plain DSL lesson")
        .replace(
            "First-time users can start with the [plain DSL version](ramsey_clock_plain.ipynb)",
            "See the [operation-based version](ramsey_clock_walkthrough.ipynb) for reusable gates",
        )
    )
    result[
        0
    ].source += "\n\nThe [operation-based lesson](ramsey_clock_walkthrough.ipynb) uses the same devices, calibration, native hooks and plots. This plain version writes to output/plain_ramsey."
    return result


for stem, lesson in (
    ("ramsey_clock_walkthrough", introductory_route(cells)),
    ("ramsey_clock_plain", introductory_route(plain_variant(cells))),
):
    for index, cell in enumerate(lesson):
        cell.id = f"ramsey-lesson-{index:03d}"
    notebook = nbformat.v4.new_notebook(cells=lesson)
    notebook.metadata.kernelspec = {
        "display_name": "Python 3 (project)",
        "language": "python",
        "name": "python3",
    }
    notebook.metadata.language_info = {"name": "python", "version": "3.12"}
    nbformat.validate(notebook)
    destination = ROOT / f"docs/tutorials/{stem}.ipynb"
    nbformat.write(notebook, destination)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "I,F401",
            "--fix",
            str(destination),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", str(destination)], check=True
    )
    print(destination)
