# Continuous NCO, strict phase boundaries and parallel SPI

Backend 9 introduced continuous NCO operation on a fixed quantized tuning word. Backend 10 added the separate [POWASF profile-selection path](powasf_timing.md). The ASF RAM restrictions below describe the independent POW register path. All latencies in the examples are offline assumptions.

## Configuration

```python
Device(
    "dds0",
    "dds",
    bus="spi0",
    update_group="board0",
    nco_mode="continuous",
    nco_epoch_latency_mu=80,  # Example clear-release to phase reference. Calibrate independently.
    square_latency_mu=80,  # Example single-tone commit-to-output latency.
    ram_latency_mu=112,  # Example RAM envelope latency.
    ram_pow_latency_mu=80,  # Example POW-only latency with autoclear disabled.
    ram_rearm_duration_mu=4000,
    ram_phase_update_duration_mu=480,
)
```

`legacy_tracking` remains the default for loading existing configurations. `reference_lab` and the short-gate examples explicitly select `continuous`. Supply the NCO epoch, Square, RAM envelope and POW-only latencies independently. The older `ram_phase_latency_mu` models the combined POW/autoclear response and cannot supply the POW-only value.

Each DDS uses one fixed quantized FTW per shot. A frequency sweep may establish a new epoch in each shot. An in-shot FTW change fails. Continuous ASF RAM also rejects profile changes that would require disabling RAM and changing the FTW source. One DDS cannot mix Square and RAM in a shot, and an active RAM channel needs its own IO_UPDATE group. Parallel SPI support applies to independent buses on the same RTIO core.

## Establishing the epoch and implementing Rz

During RF-off preparation, hold `phase_clear=1` and load the final mode and data. After all members of a shared update group are prepared, stage `phase_clear=0, phase_autoclear=0` and release them at the planned `dds_nco_epoch` edge. RAM must be in its final ASF RAM mode before this edge so the NCO starts from the correct FTW source.

For epoch E0 relative to the shot origin, quantized tuning word F and C DDS clock cycles per mu:

```text
POW_epoch = floor(E0 × F × C / 2**16)
POW_gate  = (quantized target phase + POW_epoch) mod 2**16
```

The full 32-bit accumulator then runs continuously. Its initial correction has a fixed error below one POW LSB. Replaying RAM at the same phase no longer requires an integer number of carrier periods between gates.

`Rz(theta) = exp(-i theta Z/2)`. Both `rz` and `Rz` subtract theta from the persistent drive frame F, producing drive phase `base + F`. This is the same frame update as `shift_phase(frame, -theta)`. `reset_frame` clears the entire frame, including Rz contributions. ARTIQ implements the phase effect through preparation and an IO_UPDATE even for a standalone or terminal Rz while the RF switch stays closed.

`reset_phase(frame)` also establishes a new logical zero carrier reference at the scheduled time. The continuous path preserves the single hardware epoch and synthesizes the compensation at full phase precision before quantizing the POW. It distinguishes the logical reference from the latency-corrected effect, retains explicit pulse phase and calibration and rejects a reset inside an active pulse or without preparation time.

```python
with exp.average(1), exp.realtime():
    play(q.raman, shaped_800ns)
    rz(q, math.pi / 2)  # Update the carrier at 800 ns, even without another play.
```

Mapped Raman/RF drives can receive the update even if they have no play. Independent illumination and readout keep their own state. Incompatible DDS states/update groups and any additional non-drive use of the same phase-target DDS in the shot are rejected. Each target requires explicit phase ownership.

```python
at_mu(t0 + prepare_time)
self.device_0.set_pow(new_pow)  # Write the buffered register.
at_mu(t0 + phase_time - L_pow)
self.device_0.io_update.on()  # Phase takes effect at phase_time.
at_mu(t0 + phase_time - L_pow + 8)
self.device_0.io_update.off()
```

Timed Square writes its single-tone profile with `write64`. The corresponding generated examples are `output/continuous_nco/standalone_rz/experiment.py` and `output/continuous_nco/ram_800_terminal_rz/experiment.py`.

The standalone `Rz(+π/2)` example has no play. Its logical time 0 corresponds to `t0+1488` after RF-off preroll. FTW stays fixed as POW changes from 23592 to 7208, a difference of −16384 (−π/2).

```python
# Release clear once in the shot preroll, after loading finishes.
at_mu(t0 + 0)
self.device_0.io_update.on()
at_mu(t0 + 8)
self.device_0.io_update.off()

at_mu(t0 + 128)  # prepare: 1248 ns driver occupancy, 1280 ns declared budget
self.device_0.write64(0x15, (8192 << 16) | 7208, 343597384)
if now_mu() > t0 + 128 + 1280:
    raise ValueError("Square prepare driver exceeded declared budget")
at_mu(t0 + 1408)  # commit: L_square=80 ns before effect
self.device_0.io_update.on()
at_mu(t0 + 1416)
self.device_0.io_update.off()
# effect: t0+1488，DSL Rz time 0 ns, independent of later plays
```

The terminal RAM example uses three 800 ns gates with origin `t0+240`. Opcode 7 prepares `set_pow(29507)` at `t0+2080`, down from 45891 by 16384. It commits at `t0+2560` and takes effect 80 ns later at `t0+2640`, the logical 2400 ns boundary. The envelope ends and RF closes at the same boundary. A terminal Rz can therefore use unequal POW and envelope latencies.

## Strict timing and capacity

In the continuous path a requested carrier change takes effect exactly at the DSL boundary, including when the preceding envelope's tail is zero. During RF-off preparation a play may preset its state. While RF remains open every interval of the preceding gate must be preserved.

For a strict coincident ASF RAM replay and POW change, the single IO_UPDATE must satisfy both commitments:

```text
C_env = T - L_env
C_pow = T - L_pow
```

This requires `L_env=L_pow`. A second IO_UPDATE would restart the envelope. A standalone RF-off Rz can have unequal latencies provided its internal restart cannot interfere with a subsequent gate. The 80/112 ns example with 800 ns gates illustrates this restriction.

| Scenario | Continuous offline contract |
|---|---|
| Same-axis Square/RAM replay every 800 ns at any representable fixed FTW | Supported with one epoch |
| Single 200 ns Square/RAM with another channel | Preparation can precede the gate if resources permit |
| New native Square axis every 800 ns | Each full profile write needs 1248 ns |
| New ASF RAM axis every 800 ns, unequal latencies | Strict coincidence fails |
| New ASF RAM axis every 800 ns, equal simulated latencies, independent SPI | Schedulable in this simulation. Real calibration remains necessary. |
| Two POW writes on one SPI every 800 ns | Two 416 ns transactions exceed the period |
| Arbitrary new axis every 200 ns | Sustained control bandwidth is insufficient |
| Terminal/standalone Rz | Real commit when preparation fits |

## Scheduling and verification

Writes on independent SPI buses can overlap and cross unrelated TTL/RF edges. Unknown bus identity is serialized conservatively. Each bus obeys busy times, preparation/commit dependencies and actual shared update aliases. Generated kernels call the drivers with planned absolute `at_mu` timing. The verifier checks outstanding buffered state, data completion, SED lane submission order and channel collisions. Some cases that would need a split SPI transaction still fail. Real CPU and FIFO throughput require target testing.

Square stays single-tone. FlatTopGaussian uses resident ASF RAM with the explicitly requested `sample_interval`, representable as `4*step/SYSCLK`. The default is 20 ns and the short-gate examples use 4 ns. Large gate counts reuse small waveform libraries subject to capacity.

Run `uv run --locked python examples/continuous_nco.py` for accepted and rejected cases. `examples/short_gate_timing.py` covers the 800/200 ns matrix and `examples/ram_boundary_sequence.py` defaults to 1024 same-axis 800 ns calls. Digital instruction costs are listed in the [ARTIQ timing reference](artiq_instruction_timing.md).

Previous offline evidence is indexed by source, ELF, script and driver hashes in `output/continuous_nco/verification_summary.json`. Individual exports retain `artiq_compile/compile_report.json` and `real_driver_trace.json` or `real_ram_driver_trace.json`. Reproduce the instruction trace with `scripts/artiq-python.ps1 scripts/audit_phase_paths.py --output output/continuous_nco/driver_instruction_times.json`. Compare the recorded hashes with the code being reviewed. A mock, real-driver virtual RTIO trace and offline ARTIQ build each verify a different layer. Carrier/envelope response, board synchronization and sustained submission still require hardware measurement.
