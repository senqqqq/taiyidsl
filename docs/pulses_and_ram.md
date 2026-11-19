# Pulse parameters and AD9910 RAM envelopes

`play()` overrides apply to one call and leave the pulse object unchanged. Amplitude is an absolute peak, phase is in radians with the current frame applied afterward, and frequency is in Hz. Omitted fields use the pulse defaults and, where applicable, qubit parameters. Use `duration` for `Square` and `flat_duration` for `FlatTopGaussian`.

```python
from dataclasses import replace

from taiyidsl import (
    Experiment,
    FlatTopGaussian,
    Qubit,
    SweepParameter,
    play,
    reference_lab,
    us,
)

q = Qubit("q0")
flat = SweepParameter("flat_duration", [0, 4, 8], unit="us")
pulse = FlatTopGaussian(flat_duration=flat, sigma=0.5 * us, cutoff_sigma=4)
exp = Experiment("flat_top_scan", [q])
with exp.average(100):
    with exp.sweep(flat):
        with exp.realtime():
            play(q.raman, pulse, amplitude=0.5, phase=0.2)

lab = reference_lab([q])
# The offline example assumes a 64 mu delay. Measure it for the target.
device = lab.setup.devices["urukul0_ch0"]
lab.setup.devices[device.name] = replace(device, ram_latency_mu=64)
compiled = lab.compile(exp)
```

Flat duration, amplitude, carrier frequency and phase may be varied by a static sweep. Sigma, cutoff and sample interval are fixed. A zero plateau still has both Gaussian edges. The older RF-off replay mode below gives 592/112 ns example intervals. For a configured `ram_phase_latency_mu`, see [boundary-phase replay](ram_boundary_timing.md).

## Shape and sampling contract

One edge has duration `edge = cutoff_sigma * sigma`. Total duration is `flat_duration + 2 * edge`. The plateau is normalized to one. For a point outside the plateau, let d be distance to the nearest plateau boundary:

```text
envelope = (exp(-d² / (2 sigma²)) - exp(-cutoff_sigma² / 2))
           / (1 - exp(-cutoff_sigma² / 2))
```

The shift and normalization give exact zeros at both endpoints. Apply the call's effective amplitude, signal mapping scale and hardware amplitude calibration before quantizing to 14-bit ASF. Single-tone amplitude scaling is not applied a second time.

- The default sample interval is `20*ns`. It must equal `4*step/sysclk` with integer `step` from 1 through 65535.
- Total duration and each edge duration must be integer multiples of the interval. Each edge includes at least two intervals.
- Including the terminal zero, the word count is `total_duration/sample_interval + 1` with a 1024-word limit. Choose any coarser sampling explicitly.
- Each DDS can replay resident waveforms within a shot. Per DDS per point there are up to eight RAM profiles sharing 1024 words. Preload before the point/shot and keep RAM resident between its gates.
- Deduplicate by **actual quantized ASF words and sample step**. Carrier frequency and phase are separate. Peak amplitude changes typically require a different payload after calibration.
- An active RAM DDS owns its IO_UPDATE group. Square/RAM mode changes on the same DDS within a shot are unsupported. Different points can restore the appropriate mode and register state.
- A RAM launch occupies an 8 mu commit window. Rearming reserves its full declared preparation time. Check shared buses, submission edges and explicit `reserve()` constraints.

## Long sequences from a small library

[The gate-sequence example](../examples/ram_gate_sequence.py) uses these four plays as a repeated motif. Its π and π/2 labels illustrate a data flow. Each rotation still needs its own measured Rabi calibration.

```python
pulse = FlatTopGaussian(4 * us, sigma=0.5 * us)
gap = 4112 * ns
with exp.average(1):
    with exp.realtime():
        with exp.repeat(256):
            play(q.raman, pulse, amplitude=0.5, phase=0)  # X
            delay(q.raman, gap)
            play(q.raman, pulse, amplitude=0.5, phase=math.pi / 2)  # Y
            delay(q.raman, gap)
            play(q.raman, pulse, amplitude=0.25, phase=0)  # X/2 label
            delay(q.raman, gap)
            play(q.raman, pulse, amplitude=0.25, phase=math.pi / 2)  # Y/2 label
            delay(q.raman, gap)
```

The final delay is part of the motif. The 256 iterations give 1024 plays using two resident 401-word shapes, **802 total words**. X and Y use one shape, X/2 and Y/2 the other. The independent POW register supplies phase with frame and shot-origin compensation. A RAM descriptor supplies no arbitrary amplitude multiplier. Too many distinct peaks, durations or sampling steps can exceed eight profiles or 1024 words. There is no mid-sequence bank swap on this path. Halving RF amplitude or plateau duration does not by itself calibrate a half-angle rotation, particularly with Raman coupling or sizable Gaussian edges.

The default `auto` export emits one finite event table in core memory per repeated RAM point and iterates it in the kernel. `mode="unrolled"` emits source per event for debugging. The manifest records `ram_execution` (`event_table` or `unrolled`) and `ram_table_points`. The table stores time, opcode, register words and profile numbers. DDS RAM usage depends on waveform variants. Core data memory, transfers, compile expansion and RTIO throughput still bound sequence size.

## Complete rearm and RF-off spacing

```python
lab.setup.devices[name] = replace(
    lab.setup.devices[name],
    ram_rearm_duration_mu=4000,
    ram_latency_mu=112,
)
```

`ram_rearm_duration_mu` defaults to `None`. When set, repeated-play mode keeps `matched_latency_enable=1` even at a point with only one gate. Calibrate this mode separately. `ram_latency_mu` must cover at least 106 DDS system clocks and match the configuration grid. The example's 112 mu at 1 GHz and 1 ns/mu is an assumption. Without the rearm contract the original single-envelope, matched-off path cannot replay.

The first gate is prepared before the shot origin. Complete rearm of a later gate requires the following steps with RF off:

1. Disable RAM and issue IO_UPDATE so changing profile cannot prematurely start playback.
2. Select its resident profile, stage FTW and POW, and commit those parameters with RAM disabled.
3. Stage RAM enable and phase-autoclear. Issue the next IO_UPDATE at the scheduled launch.
4. Allow for the declared pipeline delay, open RF at envelope start and close RF after the full envelope.

Define the gap from the previous RF close to the next RF open:

```text
min_gap_mu = ram_rearm_duration_mu + ram_latency_mu
```

Grid alignment and other resources must also fit. The example gives `4000 + 112 = 4112 mu = 4.112 µs`. An 8 µs envelope therefore has a 12.112 µs start-to-start period. This full rearm also applies to unchanged profiles unless the fast path below is enabled.

The real ARTIQ `9.9664+071b41b` driver advances `428*C+16` on Rev9 and `368*C+16` on Rev8, where C is the SPI coarse period in mu. At C=8 and 1 ns/mu these give **3440 mu** and **2960 mu**, excluding the final start pulse. The host checks the minimum budget and the kernel checks the actual cursor. With zero extra margin on Rev9 the model admits `3440 + 112 = 3552 mu = 3.552 µs`. The example reserves another 560 ns for preparation. Real underflow limits must be measured. Audit a changed clock or toolchain with:

```powershell
.\scripts\artiq-python.ps1 examples/artiq_ram_timing.py --proto-rev 9 --output output/ram_replay/driver_timing_rev9.json
```

This audit executes actual Python driver bodies with virtual RTIO.

## Fast replay with the same waveform and FTW

For a shared resident profile and *quantized* FTW, declare `ram_phase_update_duration_mu` along with the rearm and matched-latency contract:

```python
lab.setup.devices[name] = replace(
    lab.setup.devices[name],
    ram_rearm_duration_mu=4000,
    ram_phase_update_duration_mu=480,
    ram_latency_mu=112,
)
```

RAMPUP restarts at its start address on each IO_UPDATE. These shortcuts keep RAM enabled and avoid another CFR1, FTW or profile write.

| Hardware state relative to previous gate | Between-gate work | Example minimum RF-off interval |
|---|---|---:|
| Same profile/FTW, new compensated POW (often X/Y) | `set_pow` plus launch IO_UPDATE | 480 + 112 = 592 ns |
| Same profile, FTW and compensated POW | Launch IO_UPDATE only | 112 ns |
| Different profile or FTW | Complete rearm | 4000 + 112 = 4112 ns |

At 1 ns/mu, 1 GHz DDS and SPI coarse 8 mu, `set_pow` advances `52*C = 416 ns` on Rev8 and Rev9. A zero-margin budget of 416 mu gives a 528 ns gap. The example uses 480 mu. The 8 mu final pulse width already lies inside the 112 mu edge-to-output window. Both host budget and kernel cursor checks apply.

Compare the **actual compensated POW**. With `phase_autoclear=1` each launch uses its time relative to shot origin. Identical logical `phase=0` may require different POW words. The example chooses exactly representable 125 MHz at 1 GHz on an 8 ns grid to keep POW identical for exact repeats. X/Y share the same amplitude payload even when POW changes.

[The fast-replay example](../examples/ram_fast_replay.py) generates 1024 alternating X/Y gates and 1024 identical gates, each with 401 resident words. It also exports an eight-gate unrolled comparison. `ram_replay_action` records first/rearm/phase/retrigger. A `dds_ram_phase` interval records phase programming. Manifest `replay_actions` and `min_gap_by_action_mu` summarize counts and bounds. `min_gap_mu` is the maximum required interval for subsequent calls at the point, retaining the rearm contract for a single gate.

```powershell
uv run --locked python examples/ram_fast_replay.py
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_fast_replay/xy --target rv32g --output output/artiq_compile/ram_fast_xy
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_fast_replay/repeat --target rv32g --output output/artiq_compile/ram_fast_repeat
```

The RF-off path prepares POW only after the preceding gate closes. The [boundary path](ram_boundary_timing.md) may prewrite while a preceding gate runs when intermediate commits can be ruled out. Neither 112 ns nor the budget values constitute measured RF or optical limits.

## Launch and verification

Before the timing origin, load RAM words and descriptors with RF off. `dds_ram_start` records the actual ASF data, step, profile/address and POW. `dds_ram_rearm` or `dds_ram_phase` intervals identify preparation. A pure retrigger needs no preparation event. Each physical event links back to its logical play.

Supply `Device.ram_latency_mu` explicitly for the selected hardware configuration. Its `None` default rejects Gaussian compilation. This IO_UPDATE-to-envelope delay is separate from the general output `latency_mu`. Require at least 8 mu and compatibility with the DDS configuration grid. Launch that many mu before target envelope start. Preload commits zero amplitude and selects the appropriate matched latency for single-envelope or repeated mode. Returning to a Square point restores single-tone profile 7 and its amplitude source.

The exported host checks direct TTL IO_UPDATE, the ARTIQ 9 `set_profile(channel, profile)` API, independent DDS chip select, and zero `sync_data.io_update_delay`. It checks again after EEPROM calibration has loaded during device initialization. SPI RegIOUpdate and nonzero update-delay correction are unsupported. The legacy mode clears the phase accumulator on update and applies shot-referenced phase compensation using the actual launch.

The mock inspects preload, calls, launch-to-gate interval, full envelope duration, shared resources, scan indices and Square/RAM restoration. An ARTIQ offline build verifies code generation, while RTIO, RF/DAC and the optical phase after the AOM require target measurements. Example assumptions of 64/112 mu must be calibrated on the deployment configuration.

```powershell
uv run --locked python examples/ram_gate_sequence.py
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_gate_sequence/artiq --target rv32g --output output/artiq_compile/ram_gate_sequence
```

Earlier rv32g compile records for the 1024-gate table and eight-gate unrolled form are under `output/artiq_compile/ram_gate_sequence/` and `output/artiq_compile/ram_gate_sequence_unrolled/`. Match hashes before reusing any recorded evidence. [ARTIQ AD9910 driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html) · [Urukul driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html) · [AD9910 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/ad9910.pdf)
