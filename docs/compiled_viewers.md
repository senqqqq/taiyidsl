# Inspect compiled experiments

Use `compiled.visualize(path)` to follow signals, resources and driver events in a pulse sheet. Select the logical view for DSL events or the physical view for device activity. Each signal has one lane across all points and shots in execution order. Width shows duration. Frame changes appear as instantaneous markers. Hover for timing and parameters, then click for source paths and links between logical and physical events. The Advanced panel selects units, driver details, executions and time windows. Large experiments page through the visible window with up to 50 executions by default.

## Inspect final Spectrum output

`compiled.waveform_simulator(path)` and the `waveform.html` produced by `compiled.export_report(...)` display all enabled channels across all cards. **By physical pulse** assigns colors in time order within each point and channel. The colors survive zoom, repeated shots and segment boundaries. Idle samples are gray. Hover to see the local start, duration and compiled RF axis. **By channel** assigns a fixed output color. Click the legend to toggle that output and its virtual Z markers across executions.

The viewer replays the final packed int16 segment bytes and static step table. Repeated zero segments preserve gate waits. When zoomed out, each time bin shows actual DAC extrema and splits at pulse boundaries. At sample resolution, neighboring pulse colors share their real boundary samples. The display preserves idle time, adjacent samples and the oscillator phase. A repeated shot shares packed data. Unique nonzero recipes have a 64 MiB raw embedding budget by default (`max_embedded_bytes`). Content hashes deduplicate segments; zero segments stay symbolic. The decompression cache is limited to 16 MiB. Both HTML files open locally in a browser.

Virtual Z markers use a separate display axis and add zero samples. Hover for rotation and frame values before and after. Markers at the same instant list operations in source order, including a trailing recovery operation. When an RB tutorial supplies a frozen basis, gate words and recovery, the viewer validates the record against logical events. Logical IDs connect the native gate, Clifford index and recovery label to individual physical pulses. An inconsistent record stops report export.

Spectrum native DDS shares the horizontal axis with the AWG. Each tone is identified by card, channel and core. Frequency (MHz) and amplitude (FS) come from quantized piecewise-linear commands, preserving pin delays, endpoint corrections and holds across known control windows. AWG amplitude units do not affect DDS amplitude. A DDS-only experiment can be viewed directly. `CompilationStore.export_report(...)` applies the same rules while loading only intersecting AWG shards. Inspect ARTIQ DDS/RAM operations in the pulse sheet.

The pulse sheet includes known control windows, physical pre-roll, fixed native stages and playback tails. The waveform defaults to complete Spectrum windows, including idle intervals and card compensation. Both use the original local samples and phase for each execution. Variable native prefixes, host waits and upload gaps have unknown duration. The concatenated display therefore cannot establish the time between real triggers. Traces describe the DAC output or predicted DDS commands. Measuring the analog response, optical field or quantum state requires additional evidence. Node/DOM checks exercise the HTML logic; record interactive browser inspection separately.

For the distinction between schedule and output views, see the [LabOne Q Pulse Sheet Viewer](https://docs.zhinst.com/labone_q_user_manual/core/functionality_and_concepts/03_sections_pulses/concepts/02_pulse_sheet_viewer.html) and [Output Simulator](https://docs.zhinst.com/labone_q_user_manual/core/functionality_and_concepts/10_advanced_topics/tutorials/00_output_simulator.html).

## Python examples

```python
compiled = lab.compile(exp)  # lab.backend == "artiq_spectrum"
bundle = compiled.export_report("output/my_experiment", source_path=__file__)
print(bundle["index"])

compiled.visualize(
    "output/my_experiment/pulse_sheet.html", waveform_path="waveform.html"
)
compiled.waveform_simulator("output/my_experiment/waveform.html")

# Inspect a 2 us window relative to logical shot T0, offline.
window = compiled.simulate_output(point=0, start_s=20e-6, duration_s=2e-6)
```

`simulate_output` returns AWG traces per card and channel with physical sample indices and times, integer DAC codes, normalized values and ideal voltages for the configured range. The default `max_samples=100000` limit applies per channel. The output lies on the actual sample grid. Use the common waveform viewer to inspect DDS commands.

Run `uv run --locked python examples/artiq_spectrum/<name>.py` for one example or `uv run --locked python scripts/spectrum/export_hybrid_examples.py` for the complete example index at `output/artiq_spectrum/index.html`. Backend selection is explicit in the lab configuration. `staged_workflow.py` demonstrates composing preparation, cooling and readout as Python DSL functions. See [native integration](native_artiq_integration.md) to reuse existing ARTIQ kernels.

| Example | Experiment | Frame check |
| --- | --- | --- |
| `rabi` | Six amplitudes by three gate widths | No programmed frame rotation |
| `ramsey_long_delay` | X90, a dark time of 10 µs, 1 ms or 100 ms, and a second pulse | Rz(+90°) changes the second base-X axis to −Y |
| `spin_echo` | X90, τ, Y180, τ, X90 | Rotate the middle gate axis and restore it |
| `spin_locking` | +Y90, +X lock, −Y90 | `reset_frame` clears the accumulated frame |
| `allxy` | 21 ordered gate pairs with a 200 µs slot per identity | Select and restore Y axes. I/I retains a 400 µs wait and skips Spectrum upload and trigger |

These examples use a nominal 30 kHz carrier, 200 µs gate slots and uncalibrated amplitudes of 0.4 and 0.2 for π and π/2. Cooling and readout are placeholders. The single-qubit examples use card A CH0. When the sync group is active both B outputs play zeros and A CH1 is disabled. `examples/spectrum_hybrid.py` also exercises parallel clock/Rydberg and local phase modulation. The AllXY gate order follows [Reed, Table 5.1](https://rsl.yale.edu/sites/default/files/2024-08/2013-RSL-Thesis-Matthew-Reed.pdf).

## Read an exported report

For a browser acceptance check, open an exported directory report, switch chunks, zoom from the overview to individual samples and follow a source link back to its snapshot. Record the browser version and the report used.

Each `index.html` links the DSL source, triggers, final frames, waveform and per-point segment/step tables. `point_N/group_N/card_N/` contains packed binary, CSV and `sdk_calls.txt`. Card names appear in tables and JSON. `export/artiq/experiment.py` contains the ARTIQ export. `report.json` includes the snapshot hash, host plan and bank layout.

The execution section displays line-numbered, hashed source snapshots of the host bridge, ARTIQ kernels and Spectrum adapter (`runtime/spectrum_runtime.py`). `GeneratedExperiment.run()` validates targets and calls `initialize_devices()` once before the shot loop. The host dispatcher `_execute_point(point)` selects the appropriate `point_N()` kernel. For each shot, the host prepares and checks all cards, configures and uploads as needed, selects the entry, arms all cards and waits for readiness. The ARTIQ point kernel controls preparation, trigger and timed readout. Completion checks precede host archival. `configure_for_calibration()` is a separate clock-configuration step.

Schema 2 reuses a resident bank and uploads when switching blocks. Schema 1 uploads per point. Cyclic global averaging can revisit earlier banks. Chunk averaging completes the current chunk before switching. Each chunk page shows shared slots, the complete step table, per-point entries and packed files, with up to 20 points on a page. Per-point tables decode an individual program. The SDK listing expands the manifest for inspection. At runtime the adapter generates or caches recipe samples, `start_step(entry)` selects an entry and `arm()` waits for EXT0. Consult an execution receipt for `uploaded`, `bank_sha256` and `entry_step`. See [chunked execution](chunked_execution.md).

To regenerate the Ramsey report, run

```powershell
uv run --locked python examples/artiq_spectrum/ramsey_long_delay.py
```

## Trace the first pulse and phase

In the example Ramsey configuration the shot begins at logical T0 with frame F=0 and cooling. The trigger is at 19.488 µs. With the declared 400 ns delay, sample zero is at 19.888 µs. The first 140 samples are zero (112 ns), placing the first gate at 20 µs. These values describe this configured model.

The final sample phase is `Phi_osc(t) + s[F + phi_pulse(u)] + offset + calibration`. The default oscillator epoch is sequence entry. `phase_evolution` records modulation state, analytic endpoints and instantaneous frequency. `operation_path` and `section_schedule` connect each event to section ancestry, dependencies, resources, actual start and grid waits. Check `blocking_resources`, `boundary` and `start_grid_mu` for the binding constraint. The report lists up to 1,000 overlapping final DAC pulse intervals and marks truncation. See [Spectrum phase modes](spectrum_phase_modes.md).

Rz(+90°) shifts subsequent base phases by −90°. A local phase of −π/2 affects its own pulse. `reset_frame` clears F and preserves the oscillator reference. `reset_phase` also clears inherited modulation and establishes a new carrier reference. Shots start with a fresh frame. `start_phase_deg` includes carrier advance, frame, calibration and the initial local modulation.

In `greenmot_ramsey.py`, the earlier variable native prefix precedes T0. An opaque block identifies the fixed 15 ms + 16 ns GreenMOT tail, ending on the final LDAC rising edge. After the configured 10 µs wait, the first RF pulse starts at T0 + 15.010016 ms. Its phase is 0° with the default sequence reference, or 108.1728° with an explicit shot reference. The report records the native boundary even if a point contains no RF. Physical field settling, alignment below 5 ns and phase agreement below 1° require measurements. See the [native timing boundary](native_artiq_integration.md).

Large experiments can use a [directory report](streamed_execution.md) with chunked metadata and windowed packed samples. A [capture comparison](capture_comparison.md) can attach measured residuals and separately identify an exploratory fit.
