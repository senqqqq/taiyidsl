# GreenMOT and Ramsey with native ARTIQ methods

`../greenmot_ramsey.py` builds prepare_MOT → delay → triggered_sequence → execute_readout. The `prepare_MOT` helper records two native calls for the variable prefix and the strict preparation tail. A single exported shot kernel joins the native methods and generated control on the core.

`GreenMOTAdapter(source)` in `adapter.py` shares one `NativeKernelPackage` between `prepare_MOT()` and `execute_readout()`. It owns the resources, source validation and prefix/timed/suffix declarations. The helper adds no Section, so the native calls remain direct children of realtime. The underlying kernel code is in `greenmot_stages.py` and `greenmot_fixture.py`.

```python
with exp.realtime():
    prepare_MOT()
    delay(10 * us)  # From the end of the strict preparation tail to the first RF gate
    with exp.triggered_sequence(group="qubit_awg"):
        trigplay(q.rf, pulse)
        delay(dark_time)
        rz(q, math.pi / 2)
        trigplay(q.rf, pulse)
    execute_readout()
```

After the real preparation tail shuts off light and updates the magnetic field, the explicit delay begins at the final digital edge. Cooling is already part of the original preparation.

## Export and compile offline

The default `greenmot_fixture.py` provides native TTL kernels for teaching. The following example exports the experiment offline.

```powershell
uv run --locked python examples/artiq_spectrum/greenmot_ramsey.py
```

Supply the external experiment source explicitly to audit a laboratory version. The example reads the source locally.

```powershell
uv run --locked python examples/artiq_spectrum/greenmot_ramsey.py `
  --greenmot-source C:/path/to/atom-control/artiq-master/lib/_GreenMOTbase.py `
  --output output/artiq_spectrum/greenmot_ramsey_external
```

`device_db_offline.json` uses the original device names and real AD9910, CPLD, Zotino and TTL driver classes for disconnected compilation. RTIO addresses are assigned from 0x200, EEPROM calibration is replaced with static values and the core has no network address. Keep this file separate from laboratory wiring. Compilation checks the device call chain and types.

Compile the default fixture using the separate ARTIQ toolchain:

```powershell
.\scripts\artiq-python.ps1 scripts/compile_native_artiq.py `
  output/artiq_spectrum/greenmot_ramsey/export/artiq `
  --extra-device-db examples/artiq_spectrum/greenmot_native/device_db_offline.json `
  --module-root examples/artiq_spectrum/greenmot_native `
  --target rv32g --output output/artiq_spectrum/greenmot_ramsey/native_compile
```

For external mode use `greenmot_ramsey_external/export/artiq` and an additional `--module-root C:/path/to/atom-control/artiq-master`. The previously reviewed upstream file also imported `tifffile`. That check supplied `tifffile==2025.6.11` from an isolated `output/native_compile_dependencies` using another `--module-root`. Inspect the actual version and paths in each compile report. `kernels_executed=false` identifies disconnected compilation.

The upstream source reviewed for this integration was TaiyiQ/atom-control commit `150c7fe724e8af078d97eeb8718f3e10d210d69c`, including the base class and `artiq-master/repository/20261109/GreenMOT.py`. The example fixes 399-horizontal imaging. A local source hash pins the file supplied to the adapter. Confirm remote byte identity independently when establishing provenance. The ARTIQ interpreter must resolve this package, the external `lib` and dependencies such as `tifffile`. The loader checks resolution and hashes before import. Re-export after changing source.

## Map the original run into the combined shot

The original `run()` already owns initialization, scanning, MOT, imaging and cleanup. The adapter preserves its constituent operations while the DSL owns the scan.

| Original operation | Position in the combined experiment |
|---|---|
| Hardware initialization including core reset | `initialize_native`, once |
| Scan loop | DSL average/sweep |
| `break_realtime`, 5 ms and first part of `prepare_GreenMOT` | Native preparation prefix before the control epoch |
| DRG compression/cooling | Original helper, ending the prefix at probe-shutter opening |
| 15 ms hold → MOT light off → Zotino update | Three original statements in strict `finish_preparation` |
| Last rising Zotino LDAC edge | End-of-preparation anchor after the explicit 16 ns advance |
| Post-preparation delay | Default 10 µs to the first gate |
| Ramsey | Generated AWG sequence with one common start trigger |
| `imaging_399_hor` | Fixed 100.48 ms `execute_readout` |
| Original final `initialization` | `finalize_native` cleanup, allowed to wait after readout |

The upstream program only prints `scan_index`. It does not use it to set `f_SF` or another frequency. Native parameters still come from the original build, while the DSL scans Ramsey dark time. Keep host-like prints outside the strict shot kernel.

`validate_greenmot_split()` removes the run's break/5 ms lead from the reviewed prefix and rejoins it to the final three statements. It compares the resulting AST with the full supplied `prepare_GreenMOT()`. Changes in operation, value or order fail the comparison. This verifies the split independently of comments and formatting.

AD53xx/Zotino `set_dac()` schedules SPI writes before the cursor, schedules LDAC and restores the incoming cursor. The last rising LDAC edge follows by two SPI reference periods. `delay_mu(2 * self.zotino0.bus.ref_period_mu)` extends the tail to include that edge. The adapter requires a 1 ns RTIO mu and an 8 mu SPI reference period, giving a declared **15 ms + 16 ns** tail. Negative-offset SPI writes remain inside the initial hold.

The preparation boundary marks the digital LDAC edge. Measure analog DAC settling, coil current and atomic readiness separately. The explicit 10 µs gap carries no measured settling guarantee. `initialize_native` drives `ttl_Camera_Trig` high because 399-horizontal imaging triggers on the falling edge. Frame retrieval requires a host adapter. Imaging here is insensitive to the final drive phase. The terminal virtual Z remains recorded in the artifact.

## Timing and phase

The wall time of the native prefix varies with slack. Establish control T0 at the start of strict `finish_preparation`. All breaks precede T0. Preserve a fixed interval from the final preparation edge through the first gate. The strict tail, gap, gate and readout exclude breaks, resets and blocking waits. Cleanup can wait after readout. The 512 ns pretrigger must fit the gap. The compiler rejects a trigger that would overlap the opaque native tail.

The default first 30 kHz, 200 µs pulse starts at T0 + 15.010016 ms. Its first sample has phase 0° for the sequence reference and 108.1728° for explicit `phase_reference="shot"`. With `rz(q, pi/2)` the next drive frame decreases by 90°. The intervening time still contributes to carrier evolution.

| Dark time | Second gate start from T0 | First sample phase modulo 360° |
|---|---:|---:|
| 10 µs | 15.220016 ms | 18° |
| 1 ms | 16.210016 ms | 270° |
| 100 ms | 115.210016 ms | 270° |

Both 200 µs pulses illustrate structure. Measure their physical angles before using them as calibrated gates. Per point the sequence has one common trigger, F=−π/2 and an Rz contribution reported as +π/2. Hardware operation requires a real device database, exclusive trigger wiring, calibrated SDK/clock settings and verified GreenMOT parameters. `SIM_*` and simulated latencies are for offline export. Supply laboratory methods for tweezer loading, state preparation and image analysis.

## Import the adapter or use multiple components

From the project root, import `GreenMOTAdapter` from `examples.artiq_spectrum.greenmot_native.adapter` or run `uv run python -m examples.artiq_spectrum.greenmot_ramsey`. Host declarations do not import the raw GreenMOT class or ARTIQ. The standalone example also supports direct invocation and `runpy.run_path` through an explicit local declaration path. To use several independent native classes, see the adjacent `native_stages/` example and the [component guide](../../../docs/native_components.md). The legacy single-package adapter preserves its initialization reset and per-shot cleanup.
