# Legacy ASF RAM boundary phase and continuous replay

This page describes the legacy phase-autoclear path for adjacent resident-envelope replays introduced in backend 7. Backend 9 continuous NCO enforces strict carrier boundaries. See [continuous NCO](continuous_nco.md) and [short-gate phase switching](phase_switching_and_virtual_z.md) when selecting a mode.

With the same resident ASF envelope and quantized FTW:

```python
play(q.raman, envelope)
shift_phase(q.frame, math.pi / 2)
play(q.raman, envelope)
reset_frame(q.frame)
play(q.raman, envelope)
```

All three gates share one RAM payload. During the preceding gate the compiler may stage the next POW. A scheduled IO_UPDATE commits the buffered change and restarts the sequence. RF remains open across adjacent gates, and the original samples, sampling rate and durations are preserved. Independent update groups may start together. The earlier global preparation scheduler could serialize independent SPI writes. Current continuous-mode scheduling models the buses individually.

## Distinct envelope and carrier responses

The AD9910 datasheet lists 106 SYSCLK periods for the matched RAM amplitude path. Single-tone 79 SYSCLK and RAM phase 94 SYSCLK describe other sources. A combined serial-POW and phase-autoclear response requires its own calibration. [AD9910 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/ad9910.pdf) · [ADI explanation of matched latency](https://ez.analog.com/data_converters/dds/f/q-a/30438/data-latency-pipeline-delay-explanation-for-ad9910-dds/212578)

Set `ram_phase_latency_mu` explicitly to enable this path. With `None` the older RF-off scheduling contract applies.

```python
lab.setup.devices[name] = replace(
    lab.setup.devices[name],
    ram_latency_mu=112,  # Example envelope latency, unmeasured
    ram_phase_latency_mu=80,  # Example carrier latency, unmeasured
    ram_phase_update_duration_mu=480,  # set_pow needs at least 416 mu with SPI coarse=8
    ram_rearm_duration_mu=4000,
)
```

POW and accumulator clear must have a single coherent carrier response for one latency to describe them. The supported range is `0 < ram_phase_latency_mu <= ram_latency_mu`. The output delay need not be on the SPI grid, but the commit edge must land exactly on the configuration grid.

```text
C = B - ram_latency_mu        IO_UPDATE rising edge
E = C + ram_phase_latency_mu  carrier response
B                             start of the next envelope at output
```

When E precedes B, the legacy path checks every actual held 14-bit ASF sample in `[E, B)`. A phase change there is permitted only if every sample is zero or the phase-continuity test below passes. The carrier may therefore change inside an existing zero tail while every nonzero portion of the waveform retains its assigned phase.

`ram_boundary_sequence.py` explicitly chooses `cutoff_sigma=5`, `sigma=80 ns`, zero plateau, 4 ns sampling and peak 0.5. At 800 ns total duration, 201 words produce a 48 ns quantized zero tail covering the assumed 32 ns pipeline difference. Replaying 1024 such gates takes 819.2 µs with only 201 resident words. Scaling the envelope to 200 ns with sigma 20 ns produces only 12 ns of zero tail and also leaves insufficient preparation throughput. The earlier 8 µs case with cutoff 4 and 20 ns sampling has a 20 ns zero tail. These are explicit author choices.

Phase-autoclear resets the accumulator at each replay. Even equal POWs alone cannot guarantee matching phase. The full 32-bit continuity condition is:

```text
((E_new - E_old) * cycles_per_mu * FTW
 + (POW_old - POW_new) * 65536) mod 2**32 == 0
```

This path computes shot-reference tracking relative to carrier effect E. POW is quantized to 16 bits. The older RF-off path computes its compensation from launch.

## Frame commands and restrictions

- At the next gate boundary, `shift_phase` or `reset_frame` can share its commit. Apply the final frame once and enforce the preceding nonzero-output protection.
- At the end of a sequence or during an RF-off gap, a standalone commit retains the requested carrier effect even without a later play. The internal RAM restart must still leave enough time before the next gate. RF stays off.
- Frame changes inside an active RAM gate are rejected. Multiple successive commands at one instant resolve to their final frame. Distinct parallel branches cannot simultaneously write the same frame.
- Changing envelope/profile or FTW needs a complete RF-off rearm and enough explicitly authored gap.
- Each active RAM DDS owns its IO_UPDATE group. Square and RAM cannot share a DDS in one shot.
- Resident data are reused. The finite event table grows with gate count. DMA, infinite looping and cross-shot continuous replay are separate capabilities.

## Reading the plan and checking it

`dds_ram_phase` records buffered preparation. `dds_ram_start` timestamps the nonblocking rising edge, `dds_ram_commit_off` lowers it 8 mu later and `dds_ram_effect` marks the envelope response. `carrier_effect_mu` separately records carrier response. The plot changes output at the modeled effect.

The mock checks buffered/committed POW, SPI occupancy, paired edges, waveform data, RF gates, output latency and phase protection for nonzero samples. The real-driver audit executes installed ARTIQ Python methods against captured virtual RTIO. The offline compiler uses `Core.compile(host=None)`.

```powershell
uv run --locked python examples/ram_boundary_sequence.py
.\scripts\artiq-python.ps1 scripts/verify_ram_driver.py output/ram_boundary_sequence/repeat --output output/ram_boundary_sequence/real_driver_trace.json
.\scripts\artiq-python.ps1 examples/artiq_offline_compile.py output/ram_boundary_sequence/repeat --target rv32g --output output/artiq_compile/ram_boundary_repeat
```

For deployment, measure the combined POW/autoclear response and envelope response in the selected ASF RAM/CFR configuration. Verify per-board reference clocks, SYNC_CLK/IO_UPDATE alignment, RF/IQ and optical paths, CPU slack and FIFO capacity. The example's 112/80 mu values remain unmeasured.
