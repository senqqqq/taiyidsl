# Phase switching, throughput and virtual Z at 800 ns and 200 ns

Three constraints determine whether short gates can share a boundary: the DDS data path, sustained control bandwidth and the compiler's scheduling strategy. An 8 µs example exercises different constraints from an 800 ns or 200 ns sequence. This page distinguishes the earlier `legacy_tracking` tests from [continuous NCO](continuous_nco.md) and [POWASF](powasf_timing.md).

## What determines the fastest update?

| Operation | Example figure | Effect on scheduling |
|---|---|---|
| Prepare the next register value | `set_pow` uses 416 ns, a full Square profile write uses 1248 ns | Can precede the boundary but still consumes throughput |
| Commit | IO_UPDATE rises at a scheduled time, stays high 8 mu, then low for at least one coarse cycle | The rising edge timestamps release of the buffered value |
| Propagate to output | Assumed 80 ns carrier and 112 ns envelope | Calibrate separately and compensate to align both effects |

At an SPI coarse period of 8 ns and divider 2 (62.5 MHz), the ARTIQ 9.9664 `set_pow` sends an 8-bit instruction and 16-bit data. Even at the AD9910 SCLK limit of 70 Mbps the 24 serial bits alone take about 343 ns. Advancing a write moves work into earlier slack but the DDS still has a finite buffer. DMA changes CPU submission cost while serial bandwidth stays fixed. TaiyiDSL budgets 480 ns for POW and 1280 ns for a profile, checks intervening output edges, commit high/low intervals and resource conflicts, and rejects infeasible requests. [AD9910 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/AD9910.pdf) · [ARTIQ AD9910 driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html) · [Instruction timing](artiq_instruction_timing.md)

## The zero-tail condition on legacy ASF RAM

One IO_UPDATE both commits POW and restarts the ASF RAM envelope. Given boundary B, envelope latency La and carrier latency Lp:

```text
requested boundary B = 800 ns
commit             C = B - La = 688 ns
phase effect       E = C + Lp = 768 ns
new envelope       B = 800 ns
```

With La=112 ns and Lp=80 ns, nonzero output between 768 and 800 ns would acquire the new phase 32 ns early. Delaying the commit by 32 ns also delays the next envelope. In the **legacy** mode, every quantized sample of the preceding envelope must therefore be zero throughout the affected interval unless exact accumulator continuity can be proven. Matched latency cannot generally align RAM-sourced parameters. The newer continuous mode enforces the exact carrier boundary even for a zero tail. [ADI matched-latency discussion](https://ez.analog.com/data_converters/dds/f/q-a/30438/data-latency-pipeline-delay-explanation-for-ad9910-dds/212578)

Extending only `flat_duration` leaves the quantized tail unchanged when sigma, sampling interval and amplitude stay fixed. Scaling the whole envelope shortens its edges. For cutoff 5, sigma = gate length / 10, zero plateau, 4 ns sampling and peak 0.5:

| Gate length | RAM words including terminal zero | Quantized zero tail | Against the assumed 32 ns difference |
|---:|---:|---:|---|
| 800 ns | 201 | 48 ns | Covers the difference, subject to preparation throughput |
| 200 ns | 51 | 12 ns | Too short |

The compiler preserves cutoff, sampling, envelope duration and gate spacing. Legacy phase-autoclear additionally assumes a single coherent latency for both the POW change and accumulator reset. An unconfirmed oscilloscope report describes a possible difference of 1–2 SYNC_CLK periods. Measure this on the deployed system. A single continuous NCO epoch avoids repeated clears and loss of low accumulator bits. [Original measurement report](https://ez.analog.com/data_converters/dds/f/q-a/164335/ad9910-autoclear-phase-accumulator-glitches)

## Interpreting the short-gate examples

`examples/short_gate_timing.py` reports accepted and rejected paths, compares table/unrolled output on accepted cases and includes equal-latency counterfactuals. Its original 40-case matrix compared direct phase, shift/reset, Rz, one/two boards and same-phase replay. In the original legacy model:

| Gapless X/Y/X | 800 ns per gate | 200 ns per gate | Limiting condition |
|---|---|---|---|
| Square, one or two boards | Reject | Reject | 1280 ns preparation budget |
| ASF RAM, one board | Legacy declared model accepts | Reject | Short zero tail and serial POW throughput |
| ASF RAM, two independent boards | Original global scheduler rejects | Reject | Two serialized 480 ns preparations. At 200 ns a single write already exceeds the period. |
| Exact repeated hardware state with no new register transfer | Model accepts | Model accepts | Applies to the exact repeated state |

Making Lp=La does not fix a 200 ns POW transfer budget. Independent-bus scheduling in the current implementation allows parallel transactions. Two writes of 416 ns each on a *shared* SPI exceed 800 ns. Use the [continuous timing guide](continuous_nco.md) for the current capability matrix.

- Continuous NCO avoids repeated clearing. Each new required POW still consumes SPI bandwidth.
- Preloading Square profiles can replace full 64-bit writes with profile selects. Standard Urukul exposes IO_UPDATE/RF-switch TTLs but selects profiles through CPLD SPI.
- POWASF uses packed phase and amplitude samples with separately declared playback latencies. The implemented method adjusts a phase prefix and preserves all requested ASF samples.
- A fast dedicated profile interface, timed FPGA queue or I/Q waveform back end could provide the bandwidth for general 200 ns sequences. Each requires the corresponding hardware/firmware and its own timing contract.

| Audited operation | Rev8 | Rev9 |
|---|---:|---:|
| `set_pow` | 416 ns | 416 ns |
| `set_profile` | 416 ns for four shared selects | 896 ns with per-channel fields |
| Full DDS profile write | 1248 ns | 1248 ns |
| Select four channels in one CPLD write | Shared by design | 896 ns packed or 3584 ns as four calls |

An isolated preselected 200 ns gate can fit, yet repeated profile changes every 200 ns cannot. Rev9 also cannot select a different profile at every 800 ns gate. A finite X→Y sequence may fit if its first selection begins in preroll and independent edges can occur during the transfer. Use the actual submission-order checks. [Urukul driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html) · [ARTIQ Urukul gateware wiring](https://github.com/m-labs/artiq/blob/master/artiq/gateware/eem.py)

An early design estimate used the RAM phase/amplitude latencies of 94/106 SYSCLK, giving a three-sample difference at 1 GHz and 4 ns sampling. A theoretical four-axis library would take about 800/200 words at 800/200 ns when omitting terminal zero samples. The implementation preserves terminal samples, accounts for incoming phase, uses an RF-off initial hold and checks the profile-selection budget. A fixed hardware motif does not cover arbitrary random gates. See [the implemented POWASF model](powasf_timing.md).

## Rz convention

`rz(q, angle)` and `Rz` are the same zero-duration operation. The angle is in radians. Static sweeps and repeats are supported.

```text
Rz(theta) = exp(-i theta Z/2)
R_phi(alpha) = exp[-i alpha (cos(phi) X + sin(phi) Y)/2]
shift_phase(delta): F += delta
Rz(theta): F -= theta
reset_frame/reset_phase: F = 0
subsequent drive phase = pulse.phase + F
```

A positive π/2 Rz updates the drive as `shift_phase(q.frame, -π/2)`. To compensate a known logical Z error δ, use `Rz(q, -δ)`. Drive phases 0 and π/2 select X and Y, and π selects negative X.

```python
play(q.raman, envelope)  # Example 800 ns envelope
Rz(q, -math.pi / 2)  # Zero duration. The subsequent physical X drive becomes Y.
play(q.raman, envelope)
Rz(q, math.pi / 2)  # A terminal update keeps RF off.
```

ARTIQ schedules an actual register preparation and commit. Spectrum applies the frame to subsequent samples. A zero-duration Rz retains its hardware preparation constraints even between two 200 ns gates. An initial Rz can merge into preload. A terminal Rz remains in the IR and `compiled.virtual_z_frames(point)`. `compiled.frame_phases(point)` gives the complete running frame. The virtual-Z view only traces contributions since the last reset.

The frame acts on the qubit's mapped Raman/RF drives. TTL and ordinary optical signals keep their state. `reset_frame` preserves the carrier. `reset_phase` also resets inherited modulation and establishes a new carrier reference. Place Rz in the drive's serial branch. Mid-gate or ambiguous cross-branch updates to one frame are rejected. Each qubit can use its own frame in its own parallel branch.

A virtual Z rotates the reference for subsequent controls or phase-sensitive measurements. A terminal Rz leaves Z-basis populations invariant. Transverse tomography and external or multiqubit controls must consume the same frame explicitly. [McKay et al., Efficient Z gates for quantum computing](https://link.aps.org/doi/10.1103/PhysRevA.96.022330)

## Reproduction

```powershell
uv run --locked python examples/short_gate_timing.py
uv run --locked python examples/ram_boundary_sequence.py
.\scripts\artiq-python.ps1 scripts/audit_phase_paths.py
```

`output/short_gate_timing/summary.json` records acceptance or rejection reasons. `driver_phase_paths.json` records driver identity, hashes and cursor traces. Digital tests, virtual RTIO driver audits, offline ARTIQ builds, real analog POW/autoclear transients and board-to-board clock measurements provide separate evidence.
