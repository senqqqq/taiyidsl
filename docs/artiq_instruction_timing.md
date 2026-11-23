# ARTIQ instruction timing, cursor movement and output latency

This reference uses the project's virtual RTIO audit of the installed ARTIQ 9.9664 driver bodies. Its conditions are 1 mu = 1 ns, coarse period C = 8 ns, SPI divider 2 (62.5 MHz), direct TTL IO_UPDATE and zero update delay. Reproduce the measurements with `scripts/artiq-python.ps1 scripts/audit_phase_paths.py`.

| Quantity | Interpretation |
|---|---|
| `now_mu` | Scheduled cursor for the next event. Hardware time can be behind or ahead of it. |
| CPU execution and queueing | Instructions, computations and RPCs consume actual scheduling slack even when the cursor stays fixed. |
| SPI occupancy | The bus becomes available and buffered data complete at the end of the transfer. |
| Output pipeline latency | The delay from commit to the carrier or envelope effect. When calibrated it can be compensated before the requested boundary. |

## Timeline and digital edges

| Instruction | Cursor change | Behavior |
|---|---|---|
| `now_mu()` | None | Reads the scheduled cursor |
| `at_mu(T)` | Sets it to T | Moves forward or backward without waiting for hardware |
| `delay_mu(D)` | +D | Schedules subsequent events later |
| `ttl.on/off` or direct TTL `dds.sw.on/off` | 0 | Schedules an edge at the cursor. Switch response has its own latency. |
| `ttl.pulse_mu(D)` | +D | On, delay, off |
| `io_update.pulse_mu(8)` | +8 ns | Rising edge commits, falling edge follows 8 ns later |
| `with parallel` | Common start, maximum ending cursor | CPU submits branches sequentially |
| `core.break_realtime()` | max(cursor, hardware time + 125000 mu) | Establishes slack outside experiment timing. The actual increment varies. |
| `core.reset()` | hardware time + 125000 mu | Clears the FIFO and resets RTIO |
| `core.wait_until_mu(T)` | None | CPU waits until hardware reaches T |
| `ttl.gate_rising_mu(D)` | +D | Schedules an input gate |
| `ttl.count(T)` | None | Reads input and can block until the deadline |
| Conversion, ordinary calculations, `set_phase_mode` | No intrinsic movement | Consume CPU time. Phase-mode selection configures later driver behavior. |

RTIO executes timestamped events. Rewinding the cursor requires sufficient slack and a valid bus, channel and SED submission order. Independent simultaneous outputs still use lanes. Multiple edges on one channel in a coarse cycle can collide even when their fine timestamps differ.

## SPI and DDS driver calls

| Instruction | Work | Cursor cost | Commit behavior |
|---|---|---:|---|
| `set_pow/write16` | 8-bit instruction and 16-bit data | 416 ns = 52C | Buffered write |
| `set_ftw/set_asf/set_cfr1/set_cfr2/write32` | Instruction and 32-bit data | 704 ns = 88C | Separate IO_UPDATE |
| `write64` for Square | Instruction and 64-bit data | 1248 ns = 156C | Separate IO_UPDATE |
| `set_profile_ram` | Start/end addresses, step and mode | 1248 ns | Configures a descriptor only |
| `write_ram(N)` | N 32-bit words | (67N + 22)C | Preload cost |
| CPLD `set_profile` Rev8 | Changes four shared profile selects | 416 ns | Selection triggers a DDS update |
| CPLD `set_profile` Rev9 | Changes the selected channel field | 896 ns | Four independent calls cost 3584 ns |
| Rev9 packed `cfg_write` | Writes four profile fields together | 896 ns | Too long for a new selection every 200 ns |
| Single-tone `set_mu(CONTINUOUS)` | Profile and IO_UPDATE | 1256 ns | Rising edge at 1248 ns |
| Single-tone `set_mu(ABSOLUTE/TRACKING)` | CFR1, profile, update, stage CFR1 again | 2664 ns | Rising edge at 1952 ns and falling edge at 1960 ns |

The `set_mu` figures assume an entry cursor aligned to 8 mu because the driver rounds it internally. `set(...)` also converts physical parameters. Reaudit for a different mode, RAM destination, update delay, SPI clock or non-TTL update route.

`set_config_mu` advances by C. An n-bit write advances by `((n+1)*div+1)*C`. The last RTIO submission for `set_pow` occurs at 8 ns and the transfer completes at 416 ns. For `write64` those times are 712 and 1248 ns. Preloading 51 or 201 words takes 27.512 or 107.912 μs, respectively. The resulting resident envelopes play in 200 or 800 ns.

## Reading prepare, commit and effect

```text
prepare starts        transfer complete         commit rises       carrier effect
P                     P + 416 ns                C                  C + L_pow
```

To change phase at T, schedule `C = T - L_pow` and finish preparing before C. The 8 ns IO_UPDATE high interval starts at C and is already inside the latency measured from the rising edge. Independently prepared buses may commit at different compensated times to align their output effects.

The RF-off replay example's 592 ns comes from a 480 ns preparation budget plus 112 ns from RAM launch to envelope output. Actual driver occupancy for that write is 416 ns. Declare RAM envelope latency `L_env`, continuous POW latency `L_pow` and the initial NCO epoch latency separately. If one ASF RAM update both replays the envelope and commits a new POW, strict alignment of both effects at T requires `L_env = L_pow`. A second commit restarts the RAM again.

The [POWASF path](powasf_timing.md) compensates sample positions in the packed phase/amplitude data. It needs the RAM phase latency and profile-selection end-to-output latencies. Profile selection still uses the SPI bus for 416 ns on Rev8 and 896 ns on Rev9. Check sustained 800 ns updates independently.

## Sources and evidence

The audit records the local driver version and SHA-256. It checks digital submission and cursor advancement. A real offline ARTIQ build checks the generated kernels against a specific toolchain. Clock synchronization, analog amplitude and phase, long-run CPU/FIFO throughput and optical behavior at the atoms require hardware measurements.

[ARTIQ RTIO semantics](https://m-labs.hk/artiq/manual/rtio.html) · [AD9910 driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html) · [SPI driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/spi2.html) · [Core driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/core.html) · [TTL driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ttl.html) · [AD9910 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/AD9910.pdf)
