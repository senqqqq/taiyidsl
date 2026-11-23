# Realtime scope and the hybrid Ramsey example

`realtime` describes the timing of one shot. The first sections below describe current construction and scheduling. The numerical phase and playback comparisons at the end document a **historical export**. Since the 2026-11-12 phase revision, the default AWG carrier reference is sequence entry, each frame has a unified offset F, `reset_frame` clears the entire F, and `reset_phase` also establishes a new carrier reference. Select `phase_reference="shot"` for the shot-based time origin. This option alone does not restore the former H/V reset behavior. See [Spectrum phase modes](spectrum_phase_modes.md).

## What realtime means

Inside `exp.realtime(timing="strict")`, DSL nodes enter a shot timeline with compile-time timing relationships. The default is serial. Explicit `parallel` overlaps its direct child branches. Wrap multiple successive instructions in `serial` to make them one branch. Resource-aware concurrency is available in an explicitly scheduled Section.

A Python function may emit DSL instructions. The function itself, along with list computation, file access or `time.sleep()`, executes on the host during construction. Only the emitted nodes reach the hardware timeline. Define intra-shot timing with DSL operations. Entering and leaving realtime preserves the cursor and frames and does not emit a trigger. Nested realtime scopes are rejected. Sibling scopes compose according to their parent and share the shot's schedule and frames.

The exporter uses one point kernel invocation per shot and a separate device-initialization kernel at run start. Realtime blocks and gates do not each require a kernel. ARTIQ also permits device-side calls between kernels on the same core. Separate host-launched kernels can hand off precisely if the cursor, queue and necessary slack are preserved. For `break_realtime`, `core.reset`, native kernels, host lifecycle and named `exp.stage` blocks see [Native ARTIQ integration](native_artiq_integration.md).

`timing="strict"` disallows adding gaps, stretching gates or padding to hide insufficient preparation time. On pure ARTIQ, explicit `compile(..., allow_quantization=True)` may round to a grid and record a diagnostic. Rounding is off by default. The hybrid backend rejects this switch and requires exact representation on the applicable 0.2 ns common tick, 0.8 ns DAC sample and native RTIO grids.

## Which device executes which commands

| Scope | Commands | Timed execution |
| --- | --- | --- |
| Pure ARTIQ realtime | `play`, signal-specific `delay`, `reserve`, frame updates, `measure/acquire`, `serial/parallel/repeat/case` | RTIO/DDS subject to the target's capabilities |
| Hybrid realtime, outside a Spectrum block | Ordinary ARTIQ operations and explicit Spectrum blocks | ARTIQ schedules its own events and the common launch and completion boundaries |
| Inside `triggered_sequence` | `play` or `trigplay`, scoped or signal-specific `delay`, frame updates, `serial/parallel/repeat/case` | Spectrum executes a static program without per-gate ARTIQ instructions |
| Outside realtime | Host `average/sweep`, configuration, template construction, compilation and export, plus DSL `prepare` | Depends on the operation |

`prepare()` must be written outside realtime, yet expands to timed preparation-protocol TTL events on the shot timeline in the same point kernel. A cooling helper that emits timed `play` belongs inside realtime. The example's DDS cooling pulse stands in for a laboratory sequence. `measure/acquire` declares timing and result association. The host result adapter's hooks transfer detector data around the shot. The kernel does not block waiting for an image.

`average` must be the sole top-level block. Its nested `sweep` chain wraps the shot body. Both execute on the host. Within the shot `repeat` expands a finite number of times and `case` selects a branch for a known sweep point. Neither provides live measurement feedback.

A Spectrum block rejects `prepare`, `measure`, `acquire`, `reserve`, nested sequences, operations on another group and frame updates that would also affect an ARTIQ DDS. Ordinary `play` on a supported Spectrum signal is accepted alongside `trigplay`. `delay(duration)` advances the active serial branch. It does not silence the whole card or synchronize parallel branches. `delay(signal, duration)` preserves its signal resource dependency.

For an existing GreenMOT kernel, declare a `NativeKernelPackage` proxy to call it inside the composed top-level kernel. Calling the original Python function directly does not capture it. An opaque prefix finishes before control T0. To fix the time from genuine preparation to the first gate, declare the actual preparation tail `timing="timed"` and follow it with explicit one-argument `delay(...)` in the direct realtime serial sequence. The composer places its last break/epoch alignment before the tail. In the GreenMOT example the tail and gap advance the shot-referenced carrier from T0 at tail entry. The native source retains its own implementation.

## Example layout

The two backends share the DSL, sweeps, frames, results and validation infrastructure. The hybrid examples live in `examples/artiq_spectrum/` and write to `output/artiq_spectrum/`. They select `backend="artiq_spectrum"` explicitly and use `lab_simulated.json` to identify uncalibrated settings. Pure ARTIQ examples keep their existing configurations. `examples/spectrum_hybrid.py` provides the combined demonstration. Compiler validation rejects operations incompatible with the selected target. Further experiments such as Rabi, echo, spin-locking or allXY can use separate entry points in the hybrid directory.

## Run the Ramsey example

See the [Ramsey source](../examples/artiq_spectrum/ramsey_long_delay.py) and [simulated hardware mapping](../examples/artiq_spectrum/lab_simulated.json).

```powershell
uv run --locked python examples/artiq_spectrum/ramsey_long_delay.py
uv run --locked python scripts/spectrum/explain_spectrum_ramsey.py
```

The dark times are 10 µs, 1 ms and 100 ms. Each point plays two 200 µs, 30 kHz Squares with normalized amplitude 0.2 and applies `rz(q, pi/2)` before the second. Their rotation angles have not been calibrated. The 10 µs readout TTL demonstrates triggering only. Output includes the joint export, per-point and per-card segment and step CSVs, packed interleaved little-endian int16 binaries, decoded JSON, PNG/SVG plots and `walkthrough.md`. The plotted RF is reconstructed from the step table and packed samples and compared independently against the target cosine within half an LSB.

## Historical timing and packed playback

The following numbers refer to the recorded shot-reference export before the unified-frame revision. For the 100 ms point, cooling occupied 0–20 µs from logical T0. The common trigger occupied 19.488–19.552 µs. With 400 ns configured latency, program sample 0 occurred at 19.888 µs. After 140 leading zero samples (112 ns), P1 occupied 20–220 µs, P2 occupied 100220–100420 µs, and readout occupied 100420–100430 µs. The early trigger compensated for declared latency. No ARTIQ gate was inserted between the RF pulses. Trigger latency and the phase class were model assumptions.

| Card | Actual playback at the 100 ms point |
| --- | --- |
| A | S0 (250144 samples) → S1 (4000) × 31249 → S2 (3968) → S3 (250048) → END |
| B | S0 (4000 samples) × 31374 → S1 (4160) → END |

Card A's S0 contained P1 and boundary zeros. S3 contained P2 and boundary zeros. The dark interval used 4 + 4000 × 31249 + 3968 + 28 = 125000000 samples, exactly 100 ms. The final 20 zero samples (16 ns) were recorded separately from the gate width and logical readout start. Card A CH1 was disabled. Card B played zeros on both channels but took part in the common arm/ready/trigger protocol. This export did not drive clock or Rydberg. Each card had its own segment IDs, memory and step table.

For this fixture, `trigger_lead_mu=512` with 1 ns per native mu, and `trigger_latency_samples=500` with 0.8 ns per sample. The prefix was therefore 512 / 0.8 − 500 = 140 zero samples, inside the first waveform segment. The 112 ns prefix was specific to the fixture. Actual lead and prefixes depend on measured latency, a legal trigger grid and repeatable capture phase. Cards with different latencies can need different prefixes to align the RF onset.

Program sample 0 is the modeled output time of the first playback sample. The DAC clock runs independently of host upload and playback launch. Samples 0 through 139 were zero codes, and P1 began at sample 140. Physical output onset, zero-voltage offset and connector response require measurement.

## Historical phase and per-shot initialization

The historical export referenced its carrier to shot T0. P1 began at 360° × 30 kHz × 20 µs = 216°. P2 began at (360° × 30 kHz × (220 µs + dark delay) − 90°) modulo 360°. Its three starting phases were 234°, 126° and 126°. The last two delays differ by an integer number of carrier periods. An additional 1 µs would advance the carrier by 10.8°. The compiler calculated the phase and wrote it into subsequent DAC samples. Gaps contained no hidden NCO output. Virtual Z added no duration, TTL or segment. These are voltage carrier phases. Qubit evolution, detuning, Ramsey contrast and connector phase accuracy (including a 1° bound) require separate evidence.

`uv run --locked python scripts/spectrum/verify_spectrum_phase_equivalence.py` writes `output/artiq_spectrum/phase_equivalence/comparison.json` and variant exports. The recorded comparison covered 10 µs, 1 ms and 100 ms, comparing packed int16 bytes, step tables, native ARTIQ source and terminal frames, plus a third pulse to test persistence. Rerunning the script exercises the current semantics.

For the historical drive convention `carrier(t−T0) + base + H − V`, with `pulse.base_phase=0` and `binding.phase_scale=1`, the 100 ms point gave:

| Second gate | First RF sample phase | V after gate | Later pulses |
| --- | ---: | ---: | --- |
| `rz(q,+pi/2)` then `trigplay(q.rf,pulse)` | 126° | +90° | Same-frame drives kept the −90° shift |
| `trigplay(q.rf,pulse,phase=+pi/2)` | 306° | 0° | One-play base-phase override |
| `trigplay(q.rf,pulse,phase=-pi/2)` | 126° | 0° | One-play base-phase override |

The first and third variants gave identical two-card packed samples, step tables and native ARTIQ source for the pulse pair, while their logical IR and terminal frames differed. With a third default-phase pulse, the persistent Z case started at 126° and the override case returned to 216°. Adding `rz(q,-pi/2)` after P2 in the Z variant restored identical third-gate samples. For pulse base phase φ, an equivalent one-play override was φ − θ.

Each point began with H = V = 0. Each shot replayed its point program from `sequence.start_step(0)`. Terminal V was recorded and did not carry to the next shot. Over two cyclic averages, all six P1 waveforms were byte-identical and began at 216° after the 20 µs prelude. An explicit historical `reset_frame(q.frame)` at sequence entry left samples, steps and ARTIQ source unchanged. After `rz`, that historical reset cleared H only and preserved V and the carrier epoch. **Current `reset_frame` clears the unified offset F.** Real trigger repeatability, PLL state and analog phase still require hardware calibration.

Implementation paths include `src/taiyidsl/dsl/experiment.py`, `compiler/scheduling.py`, `backends/artiq_spectrum/scheduling.py`, `backends/artiq_spectrum/backend.py`, `backends/artiq_spectrum/export.py` and `runtime/spectrum/awg.py` under the package.
