# POWASF amplitude/phase alignment and hardware Rz

Enable the optional path with `Device(ram_destination="powasf", nco_mode="continuous", ...)`. ASF RAM is the default RAM destination. Native Square uses single-tone profiles. The offline model verifies packed samples, output boundaries and digital calls using explicitly declared latencies. Deployment requires independent latency, synchronization and long-run throughput measurements.

Urukul CPLD interface revisions Rev8/Rev9 correspond to `proto_rev=0x08/0x09`. Check the physical board, firmware, device database and DSL configuration for a matching protocol. The PCB v1.x revision and ARTIQ software version identify different components. Firmware replacement has its own hardware compatibility requirements. The offline device database uses demonstration values. [Urukul firmware compatibility table](https://github.com/sinara-hw/Urukul/wiki/Firmware)

## Aligning separate playback pipelines

A packed AD9910 POWASF word contains 16-bit POW in `[31:16]` and 14-bit ASF in `[15:2]`. Both fields are read together, then traverse separate playback pipelines. The datasheet RAM phase and amplitude latencies of 94 and 106 SYSCLK refer to that internal data path. Board-level latencies include additional effects. The SPI preload takes place separately. [AD9910 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/AD9910.pdf) · [ADI explanation of matched latency](https://ez.analog.com/data_converters/dds/f/q-a/30438/data-latency-pipeline-delay-explanation-for-ad9910-dds/212578)

Let `L_env - L_phase = m × sample_interval >= 0`, with integer m. Preserve every ASF value, sampling interval and gate boundary. In the first m RAM phase samples, emit the incoming phase. Emit the new gate phase from sample m onward. For assumed `L_env=112 ns`, `L_phase=100 ns` and a 4 ns interval, `m=3`. Starting the profile at T−112 ns places the Y phase boundary at T:

| Sample | Phase output time | POW | Amplitude output time | ASF |
|---|---:|---|---:|---|
| 0 | T−12 ns | Previous gate phase | T | Original sample 0 |
| 1 | T−8 ns | Previous gate phase | T+4 ns | Original sample 1 |
| 2 | T−4 ns | Previous gate phase | T+8 ns | Original sample 2 |
| 3 | T | Y phase | T+12 ns | Original sample 3 |

Both effects now respect the requested gate boundary regardless of the preceding envelope's zero tail. Reject fractional sample offsets, gates too short to contain the compensated prefix and selections that truncate a required phase sample.

## Retrigger and selection contracts

| Field | Timing reference |
|---|---|
| `ram_latency_mu` | IO_UPDATE rising edge to envelope output |
| `ram_polar_phase_latency_mu` | IO_UPDATE rising edge to phase output |
| `ram_profile_latency_mu` | End cursor of `cpld.set_profile` to envelope output |
| `ram_profile_phase_latency_mu` | Same end cursor to phase output |
| `ram_profile_control` | Deployed protocol, `urukul_rev8` or `urukul_rev9` |
| `nco_epoch_latency_mu` | Clear release in shot preroll to the continuous phase reference |

The profile end cursor is a software anchor. Measure pin response, SPI completion, synchronization and output propagation relative to it. Supply these latencies explicitly. The older autoclear or separate POW-register response describes another path.

```python
Device(
    "dds0",
    "dds",
    bus="spi0",
    update_group="board0",
    nco_mode="continuous",
    nco_epoch_latency_mu=80,
    ram_destination="powasf",
    ram_latency_mu=112,
    ram_polar_phase_latency_mu=100,
    ram_profile_control="urukul_rev8",
    ram_profile_latency_mu=112,
    ram_profile_phase_latency_mu=100,
)
```

This unmeasured example assumes 1 ns/mu, 1 GHz SYSCLK and an 8 ns SPI coarse period. Preload RAM data and descriptors with RF off. Retrigger the same resident profile with IO_UPDATE. To change profile, the CPLD SPI selection itself triggers playback. A second update would restart RAM a second time.

During playback the FTW is fixed and clear/autoclear both remain zero. Establish one NCO epoch per shot. The full 32-bit accumulator runs continuously with a fixed 16-bit POW epoch correction. The exporter supports separate continuous-mode DDSs running timed Square, ASF RAM and POWASF alongside TTL on one core. It rejects an opaque legacy path mixed into that contract, mixed modes on one DDS, shared active IO_UPDATE, and an update to the same path during a profile-selection transfer. See [mixed playback paths](mixed_pulse_paths.md).

## Selection bandwidth

| Scenario | Outcome under the stated offline contracts |
|---|---|
| Rev8, 800 ns X/Y boundaries, 112/100 ns, 4 ns samples | Phase compensation and 416 ns profile select fit |
| Rev9, 800 ns repeating X/Y | 896 ns per changed profile exceeds the period |
| Rev8/Rev9, single 200 ns gate | Selection can precede the gate |
| Rev8, new profile every 200 ns | 416 ns control transfers exceed the period |
| Two independent SPI buses, synchronized 800 ns X/Y | Calls may overlap and have independently compensated effects |
| Multiple individually phased active DDSs sharing Rev8 CPLD | Shared profile pins preclude independent selection in this implementation |

An isolated gate or a gate repeated in distinct shots can use preroll. Consecutive X/X/X gates starting from X can retrigger an unchanged resident profile with IO_UPDATE. Y/Y starting from X may require different X→Y and Y→Y phase prefixes. Sustained arbitrary axis changes pay the real 416/896 ns profile-selection cost: 52C or 112C at `spi_ref_period_mu=8`. This audit covers driver cursor advancement on one core. Synchronization of independent cores and sustainable CPU/FIFO load require separate acceptance. [ARTIQ Urukul driver](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)

The exporter does not use the internal automatic profile sequencer. An 800 ns envelope including its terminal zero occupies 201 words at 4 ns intervals. Automatically chaining after the stored word count would impose an 804 ns period.

## Standalone Rz and resident library size

In `RAM_DEST_POWASF` the output uses RAM phase samples. A hardware Rz selects or retriggers a zero-ASF phase-hold block. Its `m+1` words retain the old phase for m samples and store the new phase in the last sample. All amplitudes are zero. RF stays closed and the phase changes exactly at T and persists until the next update. A standalone `set_pow()` would not implement this behavior for the selected destination.

- `Rz(theta)` subtracts theta from the persistent frame F, matching `shift_phase(frame, -theta)`. `shift_phase` adds to F and `reset_frame` clears F. The drive uses `base + F`.
- `reset_phase` also establishes a new carrier origin. Packed phase samples carry the compensation.
- Standalone and terminal Rz work even in a shot with no play. An Rz and gate beginning at the same boundary may share a selection. RF-off updates still have a strict phase effect time.

Deduplicate by the actual packed words, sample step and mode. Different incoming phases can require different variants of the same target axis. Each DDS has 1024 words and eight profiles, including an initial RF-off hold. A typical long X/Y sequence uses `1 + 3×201 = 604` words, even after 1024 calls. Many distinct Rz angles or phase transitions can exhaust the library, in which case compilation fails. Core event tables, expanded plans and repeated bank metadata in JSON still grow with gate count.

## Independent checks

```powershell
uv run --locked pytest tests/test_ram_polar_validation.py --basetemp=.test-tmp-polar
uv run --locked python examples/powasf_sequence.py
./scripts/artiq-python.ps1 scripts/verify_polar_driver.py output/powasf/rev8_800_xyyx
./scripts/artiq-python.ps1 examples/artiq_offline_compile.py output/powasf/rev8_800_xyyx --target rv32g --output output/powasf/rev8_800_xyyx/real_compile
```

`examples/powasf_sequence.py` writes `output/powasf/summary.json`, generated sources and plans. Cases cover 800 ns X/Y and X/Y/Y/X, resident-library reuse, independent SPI, single and parallel 200 ns gates, standalone/terminal/coincident Rz and rejection for Rev9 800 ns or Rev8 200 ns profile selection. The standalone-frame example explicitly authors an 800 ns idle.

`taiyidsl.backends.artiq.validation_polar_output.verify_polar_output` decodes the actual packed words and replays both pipelines using trigger/selection events and their latencies. It compares with the authored pulses and phase commands at every quantized sample and boundary, including RF-off state, final hold and the fixed NCO epoch. Dropping the prefix, corrupting ASF, missing a terminal Rz, changing the sign or changing FTW between gates will be detected. The comparison is independent of the planner's `dds_polar_effect` target and bank `asf/pow_samples` annotations.

| Verification | Evidence |
|---|---|
| Independent output model | Packed word decoding, RF windows and strict boundaries under declared latencies |
| Export and mock equivalence | Generated code, plan and each export mode agree |
| Real-driver virtual RTIO audit | Driver version/hash, register and RAM data order, CPLD field/descriptor selection, digital edges, clear release, timing and SED submission |
| Offline ARTIQ compilation | Chosen compiler version accepts the kernels associated with a source hash |
| Hardware acceptance | Clock and DDS synchronization, calibrated response to selection, RF amplitude/phase and stability of long runs |

For prior ARTIQ 9.9664/rv32g evidence see `output/powasf/verification_summary.json`, including an unrolled 800 ns case. The Rev9 same-axis example selects at its first gate and retriggers subsequent gates. Rev9 continuously changing profiles at 800 ns is rejected separately.
