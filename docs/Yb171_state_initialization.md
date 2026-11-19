# Preparing the ¹⁷¹Yb nuclear-spin ground state

The offline example builds a pump-duration scan, compiles its schedule, generates a timeline and exports ARTIQ source. Its frequencies, amplitudes and times are demonstration values. Real ARTIQ compilation, instrument output and state-preparation performance require separate verification.

## Preparing a specified nuclear spin

Use the computational basis

\[
|0\rangle=|{}^1S_0,F=1/2,m_F=+1/2\rangle,\qquad
|1\rangle=|{}^1S_0,F=1/2,m_F=-1/2\rangle.
\]

To prepare \(|0\rangle\), apply 556 nm σ⁺ light on the \({}^3P_1,F'=1/2\) branch:

\[
|1\rangle\xrightarrow{\sigma^+}
|{}^3P_1,F'=1/2,m_F'=+1/2\rangle
\xrightarrow{\mathrm{spontaneous\ emission}}
|0\rangle\ \text{or}\ |1\rangle.
\]

After repeated scattering the population accumulates in \(|0\rangle\). On this branch there is no \(m_F'=+3/2\) level reachable by σ⁺ excitation from the target state, so the state is dark for ideal polarization. Define σ⁺ relative to the quantization axis set by the bias field and account for both beam direction and polarization. Off-resonant excitation of other hyperfine branches, polarization impurities and field misalignment can spoil the dark state. This pumping scheme and state-selective removal have been demonstrated in ¹⁷¹Yb tweezers. [Jenkins et al., Phys. Rev. X 12, 021027 (2022), Appendix D](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

If Raman sideband cooling (RSC) precedes initialization, check its final operation. Optical pumping during RSC may already polarize the spin. Retain a zero-duration point in the initialization scan to measure the gain from additional pumping relative to the actual starting population. [Jenkins et al., Sec. VII](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

## What one shot requires

```text
Provide loaded and cooled atoms
→ Establish the trap, bias field and optical paths for preparation
→ Pump for t_pump
→ Wait for excited-state decay and optical switching to settle
→ Apply state-selective readout or the survival control
→ Image and retrieve the data
```

Cooling must be reproducible for every sample. Destructive imaging or push-out requires newly prepared atoms for each shot. Here, loading, cooling and stable fields are per-shot prerequisites. A laboratory implementation of the generated host hook `result_adapter.before_shot(point, shot)` can run and confirm preparation before starting the initialization kernel. Use `after_shot` to retrieve the image and record loading, loss and failure states. The example supplies the control sequence from initialization onward.

Host hooks have no deterministic latency. If the delay from the end of RSC to initialization matters, put both stages in the same real-time block. An elapsed delay alone provides no indication that external cooling succeeded.

## Build the scan

The [initialization example](../examples/state_initialization.py) uses push-out or a matched control followed by a camera trigger. Its readout assumes the laboratory supplies imaging illumination.

```python
from taiyidsl import LabConfig, OpticsConfig, Qubit
from examples.state_initialization import build_initialization_scan

q0 = Qubit("q0")
lab = LabConfig.load("examples/config/labs/initialization.json", qubit_id=q0.uid)
parameters = OpticsConfig.load("examples/config/optics/initialization.json")
exp = build_initialization_scan(
    q0,
    parameters,
    durations_us=(0, 10, 20, 50, 100, 200),
    shots=100,
    averaging="cyclic",
)
compiled = lab.compile(exp)
```

The lab configuration maps devices and signals. `OpticsConfig` supplies shared working points. The experiment specifies the sweep, averages and sequence. Construction takes a parameter snapshot and leaves instruments untouched. Inside the builder, read from that snapshot:

```python
p = exp.parameters
with exp.average(shots=shots, mode=averaging):
    with exp.sweep(pump_time):
        with exp.sweep(readout_mode):
            with exp.realtime():
                optical_pump(exp, p["pump"], duration=pump_time)
                for mode in (0, 1):
                    with exp.case(readout_mode, mode):
                        trigger_readout(exp, q, p, pushout=bool(mode))
```

`case` selects a branch from static scan coordinates. The control reserves the same time as the push-out pulse. Both modes trigger the camera at the same time. The logical signal `optics.pump_556` can address a beam shared across the array.

## Parameters, wiring and snapshots

| Input | Owner | Role |
|---|---|---|
| Ground-state definitions and local settings | `Qubit` / `QubitStore` | Independent snapshot at construction |
| Devices, clocks, buses, mappings and trigger sequences | [Initialization lab profile](../examples/config/labs/initialization.json) | Loaded by `LabConfig.load()` |
| Pump, push-out and readout waits | [Initialization optics](../examples/config/optics/initialization.json) | Flat `pump`, `pushout` and `camera` groups |
| Experiment parameters | `exp.parameters` | Copied and frozen by `Experiment(parameters=...)` |
| Compiler inputs | `CompiledExperiment` | Lab and parameter snapshots and content digests |

This experiment needs only the `pump`, `pushout` and `camera` groups. Its code selects `trigger_readout()`. Initialization and RSC can require different pump detunings and powers. Store a working point for each relevant condition.

`frequency_hz` gives the RF control frequency. Use the laser carrier, EOM, AOM order and pass count to calibrate detuning at the atom. The normalized `amplitude` requires a separate calibration to optical power in mW or a Rabi frequency.

`metadata` holds descriptions, labels and information about the protocol. Operations read `exp.parameters`. `Experiment` copies and freezes either an `OpticsConfig` or a plain mapping of the same shape. `OpticsConfig.from_dict(data)` and `to_dict()` validate and round-trip ordinary dictionaries without additional beam or recipe classes.

Override one call with keyword arguments inside the real-time block:

```python
from taiyidsl import us
from taiyidsl.cooling import optical_pump

optical_pump(exp, p["pump"], amplitude=0.12)
optical_pump(exp, p["pump"], duration=50 * us, amplitude=0.08)
optical_pump(exp, p["pump"])  # use the snapshot defaults again
```

Only the supplied fields change for that operation. Explicit zeros are valid. Units and ranges must match, and insufficient DDS setup time raises an error. The operation IR retains effective values, and compiled events retain values bound at each scan point.

Schema 2 provides a versioned parameter table. Apply new settings to a new experiment after reviewing them. Fits do not write back automatically. [Optical parameters and experiment construction](lab_recipes_experiments.md), [Raman sideband cooling](Yb171_raman_sideband_cooling.md)

## Spin readout and survival control

Presence imaging alone measures occupancy. To infer spin, this example selectively removes \(|0\rangle\) on the \({}^3P_1,F'=3/2,m_F'=+3/2\) branch and images the atoms that remain. Calibrate selectivity, bias field and trap conditions for the apparatus. The demonstration does not prescribe the field, power or duration used in the paper. [Jenkins et al., Fig. 10](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

| `readout_mode` | Sequence | Observable |
|---|---|---|
| `0` | Idle for the push-out duration, then image | Survival at the selected pump duration |
| `1` | Apply push-out, then image | Population surviving state-selective removal |

`exp.case()` binds this choice from the scan coordinate. Matching the windows controls for loss during the extra wait. Both branches use `measure(q, protocol="image", handle="survivors")`. The scan coordinate distinguishes the results.

Let \(S_\mathrm{reference}\) and \(S_\mathrm{pushout}\) be the survival probabilities at equal pump duration using the same loading filter. If removal of the target state is complete, the other spin state is unaffected, and other losses and imaging responses are comparable, then

\[
P_0\bigm|_\mathrm{surviving}\approx
1-\frac{S_\mathrm{pushout}}{S_\mathrm{reference}}.
\]

The reference must be nonzero and both conditions need enough valid samples. This probability is conditional on survival. Success per loading attempt also depends on loading and survival.

Account for incomplete removal, unwanted loss, imaging-threshold errors and each condition's valid sample count. Mark missing or failed images explicitly. An empty site can reflect successful removal or earlier loss, and a failed acquisition supplies no occupancy observation. Without independent readout calibration the measurement combines preparation and readout performance.

## Commissioning the experiment

1. Verify the quantization axis, polarization, addressing and resonance at the atoms. Record the trap and field settings.
2. Validate imaging and push-out selectivity. Collect reference data with status labels.
3. At fixed power, scan pump duration including zero. Look for growth or saturation of the inferred target-state fraction.
4. Monitor survival without push-out while scanning power and detuning to find a working point with acceptable preparation and loss.
5. Check for motional heating from the additional pump using an independent motional diagnostic.

Record the source experiment, readout processing and applicable conditions with any proposed calibration. The example provides the sequence and readout controls. Dedicated initialization fidelity fitting and publication of persistent calibration remain separate steps.

## Run and verify

Run from the repository root.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
uv run --locked python examples/state_initialization.py
uv run --locked python examples/verify_optics.py --experiment initialization
```

The output directory is `output/state_initialization/`.

| File | Contents |
|---|---|
| `compiled.json` | Scan coordinates, logical and physical timing, and configuration snapshots |
| `timeline.html` | Offline logical and physical timeline |
| `artiq/experiment.py` | Default host loop and parameterized kernel |
| `artiq/manifest.json` | Required devices and export checks |
| `verification.json` | The separate `verify_optics.py` script compares default and expanded mock executions |

The default scan has six pump durations and two readout modes for 12 points. Compatible points can share a parameterized kernel. Optional events represent zero duration and omitted push-out. See the generated report for the actual grouping.

The example and `verification.json` cover DSL construction, offline scheduling and agreement of mock exports. Real ARTIQ compilation, physical RTIO events, DDS phase, optical output and preparation fidelity each require separate verification. The timeline shows digital event times. Optical path-delay calibration establishes the timing at the atoms.

The camera protocol emits a TTL trigger. The laboratory supplies imaging light, exposure, data transfer and any required field or trap transitions. When adding a readout protocol, express the relevant physical steps as inspectable operations and document external prerequisites.
