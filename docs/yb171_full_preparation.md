# ¹⁷¹Yb preparation and readout reference

[The reference example](../examples/yb171_preparation_reference.py) describes a complete laboratory workflow and exports the control segment covered by the supplied adapters. Its default invocation generates offline files.

## From the atom source to final imaging

Jenkins et al. describe one ¹⁷¹Yb tweezer implementation. Their pre- and post-images identify initial occupancy and final survivors. Nuclear-spin preparation and readout use distinct optical processes. Fields, powers and durations require calibration on the apparatus. [Jenkins et al., PRX 12, 021027 (2022)](https://doi.org/10.1103/PhysRevX.12.021027), [open text, Appendices A, B.3 and D](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

| Stage | Physical purpose | Required control |
|---|---|---|
| Atom source and blue MOT | Capture and precool on the 399 nm line, deliver atoms to the science chamber | Atom source, MOT beams and magnetic-field gradient |
| Green MOT | Broadband capture, narrow-line cooling and compression at 556 nm | Detuning modulation, intensity ramps and field-gradient ramps |
| Tweezer loading | Capture atoms, clear untrapped atoms and prepare single-atom occupation | Tweezer array, bias field and loading beams |
| In-trap cooling | Prepare a cold sample for the first image | Cooling light, field and trap conditions |
| Initial imaging | Determine which sites loaded | Imaging light, simultaneous cooling and camera exposure |
| Spin and motional preparation | Optically pump the spin and apply Raman sideband cooling (RSC) as required | Pump and Raman beams with the required fields |
| Experiment and readout | Apply coherent control, then image directly or perform state-selective removal before imaging | State mapping, field and trap switching, final imaging |

## Export the control segment

```powershell
uv run --locked python examples/yb171_preparation_reference.py
```

`output/yb171_preparation_reference/` contains `workflow.json` with stage descriptions, required capabilities, external builder names and data meanings. `control_segment.json` contains the compiled segment and `control_segment.html` shows its timeline.

```text
External preparation has loaded and precooled the atoms and established operating conditions
    → pre-image without push-out
    → sideband_cool, including the initial optical pump
    → post-image (direct survival by default, without push-out)
```

`build_control_segment(..., rounds=0)` retains the initial optical pump. `pushout=True` adds a push-out sequence before the final image and sets its interpretation to `pushout_survivors`.

The supplied segment assumes field, trap and optical conditions support both imaging and control. If the apparatus requires a different bias field or trap depth after the first image, include the transition explicitly. A wait can reserve settling time once the relevant devices have been commanded.

## Supply laboratory stages as Python builders

`build_full_sequence()` requires three user-provided Python builders. It checks that all three are present before building the sequence.

```python
builders = {
    "load": load_atoms,
    "enter_control": enter_control_conditions,
    "readout": final_readout,
}
exp = build_full_sequence(q0, optics, stage_builders=builders)
```

| Builder | Required behavior |
|---|---|
| `load(exp, qubit, parameters)` | Run loading through readiness for the initial image, including MOT, tweezer loading and precooling. |
| `enter_control(exp, qubit, parameters)` | Transition from imaging into pumping/RSC conditions. Declare ramps, settling time and resources. |
| `readout(exp, qubit, parameters, *, handle)` | Complete final readout, including any state mapping, push-out, field or trap changes, illumination and acquisition using the supplied handle. |

The full order is `load → pre-image → enter_control → RSC → readout`. Insert coherent control in the experiment function between RSC and readout when required. A builder runs once during IR construction and must emit DSL operations. Device connections, image retrieval and shot-dependent decisions require execution-time support. Supplying a builder records its implementation. Physical validation remains `physical_validation=not_verified` pending apparatus tests.

Magnetic-current and tweezer-depth ramps need device adapters with timing and resource contracts. `workflow.json` records the required capabilities, and the ARTIQ backend can emit only the outputs it supports. The reference sequence leaves rearrangement, loading retries and measurement feedback to other implementations. Retain failed loads in the data and apply loading selection offline.

## Preparation, acquisition and host responsibilities

`prepare(q, protocol=...)` expands the configured trigger sequence. A `standard` TTL can start an external apparatus. Verify MOT, cooling and spin preparation through their laboratory contracts. The full builder makes these operations explicit. `LabConfig` owns wiring and device contracts, `OpticsConfig` holds working points, and the experiment function supplies execution order.

`measure(q, protocol=..., handle=...)` declares a trigger and acquisition position. Measured values arrive at execution time. `illuminated_readout()` specifies light, cooling, camera trigger and wait. Configure exposure separately from TTL width, and confirm acquisition readiness after the fixed transfer wait.

The host must match shot, acquisition handle, camera frame number and timestamp. ROI integration, background subtraction and classification run offline. A single exposure may cover multiple sites. Retain one raw image associated with its sites and trigger the camera once for that exposure.

## Occupancy, validity and survival

- `pre` records initial occupancy. A successfully imaged empty trap is a valid observation.
- `post` records final occupancy. Its interpretation depends on whether the protocol images survival directly or images survivors after state-selective removal.
- `validity` records acquisition success and whether the site could be classified. Preserve it independently of the binary occupancy.
- Retain the raw frames, ROIs, classifier version and thresholds, site order, exposure settings and parameter snapshot for reanalysis.

For the no-push-out control, estimate

\[
P(\mathrm{post}=1\mid\mathrm{pre}=1,\;\text{both acquisitions valid}).
\]

Include eligible shots with `post=0` in the denominator. After push-out an empty site may indicate selective removal or unwanted loss. Two occupancy images cannot distinguish these causes shot by shot. Use reference measurements and readout calibration to infer spin populations. Filtering every empty site as `atom_lost` would discard the state-selective signal.

## Verification scope

Tests check pre/post handles, stage order, early rejection of missing builders, retained workflow descriptions and offline export of the supported segment. Loading efficiency, temperature, spin-preparation fidelity, real camera transfer and the waveform at the atom require physical evidence. Record offline validation, compilation with real ARTIQ and laboratory acceptance separately.
