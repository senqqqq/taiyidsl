# Hybrid ARTIQ and Spectrum M4i.6631-x8 AWG experiments

In `artiq_spectrum`, ARTIQ runs cooling, TTL readout and the experiment framework. An explicit `triggered_sequence` compiles to a static program on each participating Spectrum card. Once all cards are armed and ready, ARTIQ launches them together using a shared TTL or synchronized per-card TTLs. Each card then plays gates, delays and phase changes on its own sample timeline. Both `play()` and the existing `trigplay()` can describe playback in the sequence. There is no Python SDK call or ARTIQ trigger for each gate.

This example uses cards A and B in `awg_sequence` mode. `spec_db` selects the mode independently for each card, so the same synchronization group can also contain a DDS20 card for transport. The pure `artiq` backend remains available. Importing, compiling and exporting are offline. See [Trigger configuration](authoring_and_triggers.md), [Sections and operations](sections_and_operations.md), [Phase modes](spectrum_phase_modes.md), [Chunked execution](chunked_execution.md) and [DDS transport](transport_dds.md).

## Run the offline examples

```powershell
$env:UV_CACHE_DIR = "C:/AIproject/taiyidsl/.uv-cache"
$env:PYTHONIOENCODING = "utf-8"
uv run --locked python examples/spectrum_hybrid.py
```

The default run writes `output/spectrum_hybrid/summary.json` and eight case directories. Each contains the joint `manifest.json`, `compiled.json`, `sequence_report.json`, a host entry `experiment.py` and the native kernel source in `artiq/experiment.py`. Export checks native ARTIQ events and replays the actual packed Spectrum samples offline. Running the examples does not perform real toolchain compilation, exercise a vendor driver or measure an output.

```powershell
uv run --locked python examples/spectrum_hybrid.py --case ramsey_delay
uv run --locked python examples/spectrum_hybrid.py --case rb_like_500
uv run --locked python examples/spectrum_hybrid.py --case three_channel --nuclear-stress
```

`--output DIRECTORY` selects the destination. `--nuclear-stress` also compiles, samples and exports the 100- and 500-gate programs with 200 µs nuclear-spin gates. By default these long programs have capacity forecasts only, marked `analytic_upper_bound_not_a_compiled_program`.

## Example drives and envelopes

Card A enables A:0 only, with A:1 disabled. Card B enables both channels. The frequencies are direct real RF at the AWG port. They do not represent an IQ baseband or a frequency offset from an external LO.

| Logical signal | Physical output | Example waveform |
| --- | --- | --- |
| `q.rf` | `nuclear_rf`, A:0 | 30 kHz, 200 µs, Square or FlatTopGaussian |
| `clock` | `clock_rf`, B:0 | 150 MHz, 300 µs, FlatTopGaussian with half-sine phase |
| `rydberg` | `rydberg_rf`, B:1 | 200 MHz, 400 ns, FlatTopGaussian with cubic phase |
| `cooling` | ARTIQ `cooling_dds` | 80 MHz, 20 µs demonstration pulse |
| `readout` | ARTIQ `readout_ttl` | 10 µs TTL window |

`SIM_NUCLEAR` and `SIM_AOM` identify offline fixtures. The cooling pulse stands in for a laboratory MOT/cooling sequence. The readout TTL supplies timing only. A real experiment supplies its preparation, detector transfer and result handling.

The Gaussian examples use `sigma=T/20`, `cutoff_sigma=4` and `sample_interval=0.8*ns`. The first argument of `FlatTopGaussian` is **flat_duration**. Total duration is `flat_duration + 2*cutoff_sigma*sigma`. For a 200 µs nuclear pulse use a 120 µs flat top and 10 µs sigma. For a 300 µs clock pulse use 180 µs and 15 µs. For a 400 ns Rydberg pulse use 240 ns and 20 ns.

```python
clock_pulse = FlatTopGaussian(
    180 * us,
    15 * us,
    cutoff_sigma=4,
    amplitude=0.35,
    frequency=150e6,
    sample_interval=0.8 * ns,
    phase_modulation=HalfSinePhase(math.pi),
)
rydberg_pulse = FlatTopGaussian(
    240 * ns,
    20 * ns,
    cutoff_sigma=4,
    amplitude=0.3,
    frequency=200e6,
    sample_interval=0.8 * ns,
    phase_modulation=PolynomialPhase((0, 0, 3 * math.pi, -2 * math.pi)),
)
```

Local modulation uses the whole-pulse coordinate `u=t/T`. `HalfSinePhase(a)` gives `a*sin(pi*u)`. `PolynomialPhase((c0,c1,c2,...))` gives `c0+c1*u+c2*u**2+...` in radians. The cubic example is illustrative and makes no claim about a calibrated Rydberg protocol. Samples are real RF values computed as `amplitude * envelope * cos(carrier_phase + local_phase)`.

## Configure cards, outputs and frames

The example's `offline_lab()` constructs `SpectrumCard`, `SpectrumOutput`, `SpectrumGroup` and `SpectrumConfig`, attaches the configuration to `DeviceSetup(..., spectrum=config)`, and chooses `LabConfig(..., backend="artiq_spectrum")`. Keys in `SpectrumGroup.outputs` are physical device names. `SignalMap` binds logical signals to those devices. A card declaration supplies its serial number, active outputs, sample rate, reference clock and output range.

```python
group = SpectrumGroup(
    trigger_device="awg_trigger",
    cards={
        "A": SpectrumCard("SIM_NUCLEAR", (0,)),
        "B": SpectrumCard("SIM_AOM", (0, 1)),
    },
    outputs={
        "nuclear_rf": SpectrumOutput("A", 0),
        "clock_rf": SpectrumOutput("B", 0),
        "rydberg_rf": SpectrumOutput("B", 1),
    },
    calibration={"status": "unmeasured", "revision": "offline-example"},
)
config = SpectrumConfig(
    {"qubit_awg": group}, signal_frames={"clock": None, "rydberg": None}
)
```

`q.rf` and `q.raman` normally use the owning qubit's spin frame. For custom Spectrum signals, `signal_frames` must explicitly specify a qubit UID or `None`. Here `clock` and `rydberg` have no nuclear-spin frame association, so `rz(q, ...)` leaves both unaffected. Map a signal to a shared frame explicitly when required by the physical model.

## Author a triggered sequence

```python
with exp.average(2), exp.realtime():
    play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
    with exp.triggered_sequence(group="qubit_awg"):
        trigplay(q.rf, Square(200 * us, amplitude=0.2, frequency=30000))
        delay(100 * ms)
        rz(q, math.pi / 2)
        trigplay(q.rf, Square(200 * us, amplitude=0.2, frequency=30000))
        rz(q, math.pi / 3)
    play("readout", Square(10 * us))
```

Each group currently accepts one complete region per shot. Put it inside `realtime()`. Regions cannot nest. Use `play` or `trigplay`, delay, frame updates, serial and parallel composition inside. Native ARTIQ operations belong outside the region. The host sweeps outside realtime. Pulse frequency, duration, amplitude and phase can bind per point. This arrangement makes no guarantee about host shot period or cross-shot carrier coherence.

`delay(duration)` advances time on the current serial branch without requiring a dummy TTL. `delay(signal, duration)` retains a signal-specific resource dependency. Ordinary serial code stays serial across signals. Use `exp.parallel()` for explicit overlap or `section(schedule="asap")` for resource-aware placement. Wrap multiple successive instructions for one parallel branch in `exp.serial()`. Parallel exit joins the longest branch. Overlap on the same physical channel is rejected.

```python
with exp.triggered_sequence(group="qubit_awg"), exp.parallel():
    trigplay("clock", clock_pulse)
    with exp.serial():
        delay(100 * us)
        trigplay("rydberg", rydberg_pulse)
```

A zero-duration `rz(q, theta)` subtracts theta from the unified frame offset F. Subsequent plays use `base+F`. Even a final Rz after the last RF pulse appears in the logical IR, the region's `terminal_frames` and `compiled.frame_phases(point)`. `virtual_z_frames` retains Rz provenance since the most recent reset. `reset_frame(q.frame)` clears all of F. `reset_phase(q.frame)` also clears inherited modulation and re-establishes the carrier reference. An Rz cannot interrupt a running gate, and ambiguous frame changes across parallel branches are rejected. Permitted in-pulse hardware `shift_phase/reset_frame` changes are encoded as phase steps at sample positions, preserving the original Gaussian envelope and local modulation coordinates.

By default the carrier starts at sequence entry. A preceding cooling pulse contributes no advance while dark time within Ramsey does. `phase_reference="shot"` selects the common shot T0. A zero prefix for trigger compensation, segment boundaries and caching leave the logical phase intact. Physical phase relative to the Kasli clock still depends on measured epoch and latency calibration. The existing single-trigger and epoch/local examples remain supported.

## Packed samples, finite loops and bank capacity

Program JSON stores pulse targets, segment recipes, SHA-256 identities and a finite step chain. The compiler produces samples in tiles. It packs active channels from each card in sample-major order as little-endian signed int16 and rejects clipping. Phase is already present in the RF bytes. Playback uses no implicit per-step amplitude or phase scale. Deduplication requires byte-identical packed segments.

| Constraint for this configuration | Value |
| --- | --- |
| Minimum segment length, two active channels on B | 192 samples |
| Minimum segment length, one active channel on A | 384 samples |
| Segment length increment | 32 samples |
| Default maximum tile length | 500000 samples |
| Short-gate stress tile length | 4000 samples |
| Shared card memory | 4 GiB per card |
| Maximum segments / steps | 65536 / 4096 |
| Finite loop count per step | 1 through `2**20-1` |

A 400 ns gate occupies 500 samples and can sit inside a longer tile or span tiles. An individual gate does not have to satisfy the segment-length multiple. The compiler compresses silence to zero-segment loops only when **all enabled channels on that card** are zero. A gate on the other channel prevents that compression. Hardware-required zero tails are reported separately in physical duration and preserve logical gates and readout boundaries. The 100 ms Ramsey example therefore avoids storing 100 ms of explicit zeros.

Segment slot allocation rounds the slot count up to a power of two and partitions the board memory evenly. `resident_bytes` counts unique packed data. `allocated_bytes` gives the required capacity lower bound, largest segment size times allocated slot count. `slot_bytes` gives capacity per slot under that partition. Adding up transferred bytes alone cannot establish that the program fits.

## Stress programs and historical capacity measurements

The default 100- and 500-gate tests play 400 ns, 200 MHz Squares back-to-back, changing phase according to a seeded random word. They perform no Clifford sampling, recovery, survival measurement or statistical fit. They provide stress workloads, with no RB fidelity estimate. Likewise the Rabi, Ramsey and echo examples describe pre-acquisition schedules. Their illustrative amplitudes are uncalibrated.

For the 200 µs nuclear gate, the default forecast assumes no segment deduplication. Its upper bounds on A's equal-slot capacity requirement are 64000000 bytes for 100 gates and 256000000 bytes for 500 gates. B can use looped zeros. For actual segmentation, step counts, phase variants and bytes, run `--nuclear-stress`.

The 2026-11-10 local record reports both long programs compiled, jointly exported and checked by offline playback. Each used one group trigger. Both the minimum and maximum adjacent-gate gap were zero samples. The table reproduces `output/spectrum_hybrid/nuclear_stress_report.json`. Wall times describe that run on that machine.

| Nuclear gates | A unique segments / steps | A resident bytes | A equal-slot requirement, bytes | Compile / export and verification |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 35 / 49 | 34000320 | 64000000 | 2.79 s / 4.97 s |
| 500 | 67 / 245 | 66000320 | 128000000 | 17.34 s / 17.36 s |

For both cases B used two zero segments, two steps and 32640 resident bytes. At 500 gates the 100 ms timeline would otherwise require about 500 MB of explicit zeros on B. Deduplication reduced A's measured capacity need for that particular sequence. The forecast retains its upper bound for words with less reuse.

## Time grids, triggering and the runner

Both cards in this example share a 125 MHz reference and a common trigger. It configures no StarHub. `trigger_latency_samples=500`, `trigger_lead_mu=512` and `epoch_grid_mu=32` are unmeasured example contracts. Group `_mu` values use the native ARTIQ machine unit, 1 ns here. The internal hybrid tick is 200 ps and the DAC sample grid is 800 ps. Native ARTIQ export retains native mu. Timing that misses a required grid is rejected without rounding or adding a gap.

Without a common reference, the [internal PLL bench policy](device_setup_descriptor.md#one-awg-on-the-bench-without-a-shared-reference) still uses the real ARTIQ TTL and EXT0. It preserves sample timing within the waveform but does not establish absolute phase or calibrated latency relative to the core. Two independent internal PLLs can be used in one explicit `setpoints_only` AWG group; neither configuration falls back automatically. See [clock and calibration options](hybrid_session_options.md).

For actual deployment, install the optional Python SDK and the Spectrum native driver. Offline use needs neither.

```powershell
uv sync --extra spectrum --locked
```

Configure the native ARTIQ `device_db` and any result adapter explicitly. Supply a `SpcmAdapter` and `SpectrumRunner` to the generated host entry. A genuine calibration requires measurements in addition to real serial numbers.

```python
from taiyidsl.runtime.spectrum.awg import SpcmAdapter, SpectrumRunner

# The laboratory launcher supplies real_group and generated_experiment.
adapters = {
    name: SpcmAdapter(card.serial_number) for name, card in real_group.cards.items()
}
generated_experiment.spectrum_runner = SpectrumRunner(
    adapters,
    cache_dir="output/spectrum_cache",
    max_cache_bytes=256 * 1024**2,
)
```

Constructing an adapter does not open a card. Probing or explicitly configuring one invokes the driver. Before configuring, loading or arming, the runner preflights **all** cards for identity, features, memory, clock and calibration. It then arms and waits for **all** cards to be waiting for the external trigger before invoking the ARTIQ shot entry once. `M2STAT_CARD_READY` marks completion and is unsuitable for this pre-trigger check. On failure the runner stops cards it touched and withholds the trigger if it has not been issued. `allow_unverified=True` is limited to non-hardware mock adapters.

## Derive calibration from measured captures

`group.calibration` carries the revision, card fingerprints, per-channel integer sample shifts and exact-frequency `frequency_points`. Each frequency entry has gain, `phase_offset_rad` and phase uncertainty. A carrier must have one exact frequency match. Adding a new sweep frequency requires a matching measurement. Do not substitute a 150 MHz record for 200 MHz. Card, firmware, range or wiring changes and PLL relocks can invalidate calibration. Runtime does not detect past relocks. `configure_for_calibration()` explicitly configures hardware. A normal runner never silently relocks the PLL to satisfy a check.

The offline tools in `taiyidsl.spectrum_calibration` do not operate a scope or card. `load_capture_npz(path, metadata)` reads `voltage`, `envelope` and `reference_phase_rad` arrays and calls `fit_rf_trace()`. The reference phase must already include the Kasli epoch, target carrier and local modulation. Metadata identifies the channel, carrier, trigger, rearm and lock IDs, capture time, independently measured envelope delay, fixed deskew and measurement uncertainty.

Apply timing and RF phase corrections in this order to avoid double compensation.

1. Independently measure and deskew the raw envelope delay. Supply `delay_ns`, `timing_uncertainty_ns` and `sample_rate_hz` for each entry, such as `timing_calibration["A:0"]`.
2. Shift the entire RF pulse by `round(-delay_ns*sample_rate_hz/1e9)` samples. Then capture its gain and phase with `sample_shift_applied` and `carrier_frequency_hz` in each record. The phase correction acts on the remaining error, including the carrier phase due to any residual fractional-sample delay.

```python
from taiyidsl.spectrum_calibration import calibrate_channels, analyze_measurements

# Supply real calibration captures and an independent measured validation set.
profile = calibrate_channels(
    post_shift_records,
    revision="lab-controlled-revision",
    card_fingerprints=measured_card_fingerprints,
    timing_calibration=measured_envelope_timing,
)
report = analyze_measurements(
    independent_validation_records,
    profile,
    corrections_applied=True,
    required_channels=("A:0", "B:0", "B:1"),
    lock_calibrations=profiles_by_lock_id,
)
```

`calibrate_channels()` returns estimates with `physical_acceptance="not_verified"`. Recompile after installing the new profile. The program records `calibration_sha256`, which the runner checks against `calibration_fingerprint(profile)`. A new calibration cannot be paired with old samples. `resolve_channel_calibration()` verifies the carrier point and `phase_reference="after_sample_shift"`.

Pass the full exported `groups[*].cards[card_id].contract` to `hardware_fingerprint(contract, observed)`. The `SpectrumCard` fields alone omit the trigger device and width, native ARTIQ tick, and epoch grid/offset. Changing the trigger capture phase class or declared output delay invalidates the previous calibration.

## Physical acceptance evidence

Capture the Kasli reference, ARTIQ trigger and three AWG outputs simultaneously. Deskew the scope channels first. Evaluate every permitted trigger phase class and repeat after restart or PLL relock. `select_trigger_window()` finds the centre of a common stable window from multi-card records. The result is a candidate. Sweep external trigger delay independently to measure setup/hold margin. Compare against a common epoch throughout and preserve per-shot phase errors.

`analyze_measurements()` reports peak and RMS phase error across records, a phase bound including uncertainty, and skew between simultaneously captured channel pairs. Coverage requires at least:

- 10000 triggers and 100 stop/rearm cycles per required channel.
- 30 minutes of data within a single lock for each required channel.
- 10000 simultaneous captures for every required channel pair.
- Three distinct, independently calibrated locks. For this offline acceptance campaign, record `lock_id` in the calibration captures: the profile retains `measurement_lock_id` and the dataset hash. Neither is a connection token.

Missing coverage leaves physical acceptance unverified. Calibration and validation datasets must be independent, and validation must measure corrected hardware. Predictions or replayed synthetic captures cannot establish acceptance.

A single constant phase fit across a modulated pulse cannot establish the within-pulse phase bound. For high-frequency channels, retain evidence from predefined windows covering the effective envelope:

| Field | Required evidence |
| --- | --- |
| `measurement_scope` | `"phase_windows"` |
| `phase_window_bounds_deg` | Absolute phase residual plus measurement uncertainty per window. The analyzer also includes fixed calibration uncertainty. |
| `phase_window_alignment_removed` | `False`. No fitted per-window phase or time alignment was removed. |
| `phase_windows_cover_effective_envelope`, `phase_analysis_threshold_fraction` | `True` and `0 < phase_analysis_threshold_fraction <= 0.1` |
| `rf_residual_rms`, `rf_residual_rms_limit` | RMS and predeclared limit in the same voltage unit. No default passing threshold. |
| `rf_residual_reference` | `"unaligned_target"`. Compare the complete RF with the target without per-record realignment. |

Only the actual 30 kHz carrier permits `measurement_scope="known_waveform_fit"`. It also requires `phase_was_subtracted_for_acceptance=False` and complete RF residual evidence. Six carrier periods support a known-waveform phase fit but do not establish instantaneous phase throughout the pulse. `fit_rf_trace()` retains the fit scope and residual against the unaligned target. The caller must supply separate high-frequency phase-window evidence using its declared analysis procedure. `fit_residual_rms` is diagnostic only.

An exceeded local phase or complete RF residual bound yields `failed`. Missing evidence yields `not_verified` when the evaluated limits otherwise pass. `waveform_evidence_missing` lists the omissions. The output targets remain **envelope skew < 5 ns and phase error < 1°**. A `measured` label or offline checks alone do not establish either bound.

## DDS transport and waveform limits

The three quantum-gate outputs on A/B use AWG sequences. In this hybrid backend, DDS20 on a third card drives continuous AOD transport tones using dedicated instructions. DDS transport checks frequency and slope quantization, command preparation time and SINGLE preload capacity. The [standalone Spectrum backend](spectrum_standalone.md) implements ordinary `play` of Square RF pulses on a DDS20 card, including the 6.4 ns update grid and terminal mute for test pulses. The transport interface maintains the terminal trapping hold. See [DDS transport](transport_dds.md).

Hardware references include the [M4i.66xx data sheet](https://spectrum-instrumentation.com/dl/m4i66_datasheet_english.pdf), [Spectrum hardware manual](https://spectrum-instrumentation.com/dl/m4i_m4x_66xx_96xx_manual_english.pdf) and [official spcm Python source](https://github.com/SpectrumInstrumentation/spcm).

## Historical software validation

The 2026-11-10 report records **946 passing pytest cases**, including the preceding 782 tests with unchanged frozen baselines. Ruff lint/format and wheel/sdist builds passed. The eight default examples covered 13 sweep points. The 100- and 500-gate programs of 200 µs nuclear pulses were also compiled and exported. Ten native ARTIQ artifacts, including coexisting Rev8/Rev9 Square, ASF and POWASF paths, contained 25 kernels. A real top-level bridge contributed two inherited kernels. All passed real offline compilation with ARTIQ 9.9664 / rv32g and a synthetic `device_db`. No kernel was executed and no instruments were connected for this record.

The software report is `output/spectrum_hybrid/verification_summary.json`. Per-case native compilation records are under `artiq_compile/compile_report.json`, and the bridge record is in `bridge_compile/compile_report.json`. SDK fakes are separate from native-driver verification. The original run supplied no measured captures. Wiring, reference-clock electrical compatibility, random trigger slips, sustained stability and the < 5 ns / < 1° output criteria remain **physically unverified**.

For a real multi-card deployment, verify that the installed SDK and driver configure every card, complete the DMA transfers, select the expected bank entry and re-arm reliably between shots. Capture the trigger and analog outputs together to measure physical inter-card skew and phase after re-arming and clock relocking. Mock readiness transitions and byte-identical offline playback cannot demonstrate any of these hardware properties.

The current interface accepts a single launch per group per shot. Repeated launches of the same group within one shot, absolute-phase handover between DDS and AWG, and real-time measurement feedback remain outside its supported contracts. A common scheduled trigger alone does not establish absolute analog phase across those output modes.

For a worked example see [Realtime and hybrid Ramsey](realtime_and_hybrid_ramsey.md). Additional hybrid examples live in `examples/artiq_spectrum/` and write to `output/artiq_spectrum/`.
