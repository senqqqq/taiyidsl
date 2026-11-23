# Pulsed Raman sideband cooling of ¹⁷¹Yb

The offline example builds a three-axis Raman sideband cooling (RSC) sequence, optionally probes a sideband, and exports the shot for ARTIQ. Start with the [nuclear-spin initialization guide](Yb171_state_initialization.md).

## What runs in one shot

Each shot pumps the nuclear spin, applies a sequence of red-sideband Raman pulses by axis, and uses dissipative optical pumping to reset the spin after every pulse. An optional two-beam probe precedes state-selective push-out or its survival control. Imaging light, cooling light and a camera trigger share one exposure window. The host then retrieves the measurement. The deterministic part expands into ordinary `serial`, `parallel`, `repeat`, `play`, `delay` and `measure` nodes in a single real-time shot.

The laboratory must supply loaded, precooled atoms and establish the trap, bias field, laser locks, polarizations and beam paths before each shot. A host `before_shot` hook can perform that preparation. The example starts after MOT loading, field ramps and trap-depth ramps. If the delay between preparation and RSC must be deterministic, include the relevant stages in the real-time shot using adapters with defined timing.

## Cooling cycle and frequency convention

Let \(|0\rangle=|{}^1S_0,m_F=+1/2\rangle\) and \(|1\rangle=|{}^1S_0,m_F=-1/2\rangle\). The ideal cycle on each axis is

\[
|0,n\rangle\xrightarrow{\text{Raman red sideband}}|1,n-1\rangle
\xrightarrow{\text{optical pumping}}|0,n-1\rangle.
\]

The second arrow approximates the reset. Recoil from spontaneous scattering can change the motional quantum number. At \(n=0\) there is no lower first-order red sideband. Three-dimensional cooling requires a nonzero projection of the effective Raman wavevector difference on each addressed motional axis. Pump detuning affects loss and heating in ¹⁷¹Yb. Determine the RSC pump working point under its own experimental conditions. [Jenkins et al., PRX 12, 021027, Sec. VII and Fig. 5](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

For absorption from beam A and stimulated emission into B during \(|0\rangle\to|1\rangle\), the ideal resonance is

\[
\omega_A-\omega_B=(E_1-E_0)/\hbar-\omega_{\mathrm{trap},i}.
\]

This is the **optical angular frequency difference at the atom**. Light shifts, field and trap conditions affect the observed resonance. The control signal's `frequency_hz` is an RF frequency in hertz. Laser carrier, EOM, AOM diffraction order and single- or double-pass geometry determine how an RF change appears at the atom, including the sign of the change when sweeping beam B. The `raman` table groups the settings by x, y and z. All example values are offline placeholders. Establish the resonance for each axis experimentally.

## Shared optical settings

| Input | File | Responsibility |
|---|---|---|
| Devices, resources, wiring and triggers | [rsc lab configuration](../examples/config/labs/rsc.json) | Loaded by `LabConfig.load()` |
| Shared optical working points | [rsc optical settings](../examples/config/optics/rsc.json) | Loaded by `OpticsConfig.load()` |
| Validation and dictionary serialization | [optics.py](../src/taiyidsl/config/optics.py) | Schema 2 parameter table |
| Reusable operations | [cooling.py](../src/taiyidsl/experiments/cooling.py) | Expand Python calls into DSL operations |
| Experiment builders | [raman_sideband_cooling.py](../examples/raman_sideband_cooling.py) | Axis order, repetition, readout, scans and averages |

```python
from taiyidsl import LabConfig, OpticsConfig

lab = LabConfig.load("examples/config/labs/rsc.json", qubit_id="q0")
parameters = OpticsConfig.load("examples/config/optics/rsc.json")
data = parameters.to_dict()
parameters = OpticsConfig.from_dict(data)
```

`pump` and `pushout` hold pulse settings directly. `beams` gives each beam's logical signal, frequency and amplitude. `raman["x"]` references two named beams, `a` and `b`, and gives their default pulse duration. `imaging` and `camera` supply exposure and wait times. Each common beam and pump setting has a single definition. The experiment supplies the stage order and repetitions.

The qubit owns local parameters, the lab owns topology and device/path calibration, and `OpticsConfig` owns shared settings and timing windows. `Experiment(parameters=...)` copies an optical configuration or plain mapping into a read-only snapshot. Builders use `exp.parameters`. The `conditions` field documents prerequisites. The laboratory establishes the field and checks laser lock.

Use Hz, s and rad, with normalized amplitudes from 0 to 1. Invalid values, unknown keys and unsupported schema versions raise errors. Updated settings apply to new experiments. Migrate the contents of old `RecipeSet` and aggregate `OpticsConfig` inputs to schema 2 before changing their version tag. [Optical settings and experiment construction](lab_recipes_experiments.md)

## Reusable operations and per-call overrides

| Operation | Generated sequence |
|---|---|
| `optical_pump(exp, p["pump"], ...)` | Setup window, pump pulse, settling window |
| `sideband_cool(exp, p, axes=..., cycles_per_axis=..., rounds=...)` | Initial pump, then two-beam Raman transfer and pump reset in the requested order |
| `illuminated_readout(exp, q, p, illumination=..., pushout=...)` | Push-out or equal-duration idle, wait, simultaneous illumination and camera trigger, wait |

`raman_pulse()` aligns the starts and ends of both beam pulses for cooling or probing. The initialization example uses `trigger_readout()` for a trigger-only acquisition. Each operation produces a serial IR block with `operation_name` and `effective_parameters`. The experiment snapshot holds the defaults, the operation IR records overrides and each compiled pulse records the values bound at that point. `TriggerSequence` and `TriggerStep` describe the triggers independently.

```python
from taiyidsl import us
from taiyidsl.cooling import optical_pump

optical_pump(exp, p["pump"], amplitude=0.12)
optical_pump(exp, p["pump"], duration=50 * us, amplitude=0.08)
```

Only the supplied values change for that call. An explicit `amplitude=0` takes effect. DDS reconfiguration must fit the declared time windows.

## A complete real-time sequence

Run this example from the project root.

```python
from taiyidsl import Experiment, LabConfig, OpticsConfig, Qubit
from taiyidsl.cooling import illuminated_readout, sideband_cool

q0 = Qubit("q0")
lab = LabConfig.load("examples/config/labs/rsc.json", qubit_id=q0.uid)
parameters = OpticsConfig.load("examples/config/optics/rsc.json")
exp = Experiment("rsc_single_sequence", qubits=[q0], parameters=parameters)
q = exp.qubits["q0"]
p = exp.parameters

with exp.average(shots=100, mode="cyclic"):
    with exp.realtime():
        sideband_cool(exp, p, axes=("x", "y", "z"), cycles_per_axis=2, rounds=2)
        illuminated_readout(
            exp, q, p, illumination=("image", "image_cooling"), pushout=True
        )

compiled = lab.compile(exp)
compiled.save("output/rsc_single_sequence/compiled.json")
compiled.visualize("output/rsc_single_sequence/timeline.html")
compiled.export_artiq("output/rsc_single_sequence/artiq")
```

The helper validates the inputs and handles zero rounds. Its loop is equivalent to the following structure.

```python
optical_pump(exp, p["pump"])
with exp.repeat(rounds):
    for axis in axes:
        setting = p["raman"][axis]
        pair = {role: p["beams"][setting[role]] for role in ("a", "b")}
        with exp.repeat(cycles_per_axis):
            raman_pulse(
                exp,
                pair,
                duration_s=setting["duration_s"],
                setup_wait_s=setting["setup_wait_s"],
            )
            optical_pump(exp, p["pump"])
```

`rounds=0` retains the initial pump and skips the cooling cycles. The default order is x, y, z with two cycles per axis. For coarse and fine cooling, compose multiple working points and loop counts in Python. Each Raman pair runs in `parallel(align="start")`. Pumping starts after both beams finish. `setup_wait_s` and `settle_s` give the compiler explicit windows for configuration and settling.

## Hardware and optical timing

The reference lab maps eight DDS channels on two four-channel Urukuls to the pump, common Raman beam, x/y/z Raman branches, push-out, 399 nm imaging and 556 nm imaging cooling. A TTL triggers the camera. Other wiring can implement the same logical signals.

Programming DDS registers takes time. Configuration operations sharing an SPI bus must run serially. The compiler may preconfigure other channels in valid windows. A channel already playing a pulse must have sufficient time before a new, incompatible setting takes effect. The example supplies these windows explicitly. Incompatible back-to-back pulses on one channel raise an error.

Aligned digital switching edges establish timing at the devices. Verify optical path delays, DDS synchronization, RF and optical difference-frequency stability, and polarization geometry independently to determine the alignment at the atoms.

## Cooling-round scan

`build_cooling_scan` uses three round counts, `[0, 1, 3]`, and two readout modes, `[0, 1]`. Mode 0 is the survival reference without push-out. Mode 1 removes the target spin state. The default is 20 shots per point with cyclic traversal over six logical points.

```python
count = SweepParameter("rounds", (0, 1, 3), unit="dimensionless")
readout = SweepParameter("readout_mode", (0, 1), unit="dimensionless")
with exp.average(shots=20, mode="cyclic"):
    with exp.sweep(count) as cnt:
        with exp.sweep(readout):
            with exp.realtime():
                sideband_cool(exp, p, rounds=cnt, cycles_per_axis=2)
                for mode in (0, 1):
                    with exp.case(readout, mode):
                        illuminated_readout(exp, q, p, pushout=bool(mode))
```

`cnt is count`. Python executes the body once and creates a `repeat(cnt)` IR node. The compiler binds 0, 1 or 3 at each scan point. Writing `rounds=count` is equivalent. Repeat accepts a nonnegative integer or a dimensionless integer sweep. A zero count leaves the frame and events unchanged. `range(cnt)` and `if cnt` raise errors. Use supported bindings for scan parameters. General Python arithmetic and real-time feedback are outside this API. The event budget still applies at each point. Undeclared repeat parameters and conflicting definitions raise errors even inside zero-count loops.

Use `case` to select distinct readout operations. Both modes reserve the full push-out window, so imaging and camera triggering start at the same time for a given round count. Different round counts have different shot durations. Use a duration-matched storage control to distinguish cooling loss from storage loss. This scan measures survival and final spin polarization as functions of round count. Establishing motional ground-state preparation requires a motional diagnostic.

## Sideband thermometry after cooling

`build_thermometry_scan` defaults to `cooling_rounds=2` with two cycles per axis, then applies a shorter Raman probe, sweeps one beam's RF frequency and reads out the spin flip. The default x-axis scan combines six placeholder frequencies and two readout modes for 12 points. Select y or z with `axis` and supply measured scan ranges for that axis.

```text
RSC (final pump returns the spin to |0>)
→ two-beam sideband probe
→ push-out or duration-matched control
→ simultaneous imaging light, imaging cooling and camera trigger
```

Read out directly after the probe. Optical pumping at that point would erase its spin transfer. The placeholder 5 µs probe requires an independent weak-excitation check. Identify the red and blue sidebands using the calibrated RF-to-optical relation.

For a thermal distribution with resolved sidebands, comparable effective probe couplings in the weak-excitation regime and corrected background and readout, the first-order strengths give

\[
r=A_{\mathrm{red}}/A_{\mathrm{blue}},\qquad \bar n=r/(1-r).
\]

Saturation, nonthermal populations, pulse dynamics, trap anharmonicity or different detection conditions may require fitting a different model. Determine the strengths from the spectra and corrected readout. A raw camera value at one arbitrary frequency is insufficient. The example supplies the acquisition sequence. For real images, retain loading, loss and validity for every site and shot before correcting background and readout. [Kaufman et al., PRX 2, 041014 (2012)](https://doi.org/10.1103/PhysRevX.2.041014), [Kaufman thesis, near Eq. 3.20](https://jila.colorado.edu/sites/default/files/2019-03/Kaufman_thesis_forGradSub_AMK_150602.pdf)

## Imaging and transfer

`illuminated_readout()` places two illumination beams and a camera trigger in an exposure block of exact duration `exposure_s`. The initialization example uses `trigger_readout()` to emit only the trigger. Compilation fails if the trigger sequence exceeds the exposure block.

Configure the exposure and trigger mode on the camera, and calibrate the trigger latency. TTL pulse width and physical exposure are separate settings. `transfer_wait_s` reserves a fixed interval. The host `after_shot` hook must check readiness or record missing data. The example assumes the existing field and trap support push-out. Supply any required field or trap changes explicitly.

## Run and verify

```powershell
$env:PYTHONIOENCODING = 'utf-8'
uv run --locked python examples/raman_sideband_cooling.py
uv run --locked pytest tests/test_cooling.py --basetemp=.test-tmp-cooling
```

The round scan and thermometry scan write compiled JSON, offline HTML and ARTIQ Python to `output/rsc_round_scan/` and `output/rsc_thermometry_x/`. Run `uv run python examples/verify_optics.py` to create a `verification.json` in each directory and compare default and expanded mock events point by point. The default export retains host average/sweep traversal and shares parameterized kernels between compatible points. Within a shot, bound repetitions expand into events. Large repetition counts therefore grow a kernel. The exporter currently provides neither DMA playback nor real-time loop compression for this sequence.

Tests cover configuration validation and snapshots, three-axis pulse counts, synchronized pairs, pump resets, equal-duration readout controls, probe retention, exposure bounds, rejection of same-channel updates without setup time and agreement of both exporters' mock traces. Offline checks establish digital event behavior. Actual cooling rates, \(\bar n\), DDS phase coherence, RTIO throughput and compilation with real ARTIQ require separate evidence. Record the trap, bias field, optical paths and analysis version when publishing experimentally calibrated settings.
