# Timed Square and hardware phase updates

Timed Square separates register preparation, IO_UPDATE and output effect. The compiler preserves requested `play`, `delay` and phase command boundaries and rejects timing it cannot meet. This page covers the `legacy_tracking` autoclear and shared-group scheduling contract. Reference configurations now use [continuous NCO](continuous_nco.md), which establishes one phase epoch per shot.

## Device contract

```python
Device(
    "dds0",
    "dds",
    bus="spi0",
    update_group="board0",
    square_latency_mu=80,
    square_prepare_duration_mu=1280,
    spi_ref_period_mu=8,
)
```

These example values assume 1 ns/mu and a 1 GHz DDS. The 80 ns Square latency is an unmeasured assumption. Derive the deployed value from the actual clocks, IO_UPDATE alignment, matched-latency DDS path and output measurement. Optical and RF switch delays still enter through device latency and `DeviceCalibration.latency_mu`.

With `square_latency_mu=None`, the RF-off path uses `set_mu(TRACKING)`. Its default `config_duration_mu` is 3000 mu. The old 1000 mu estimate was too short. At an SPI coarse period of 8 mu and zero update delay, the ARTIQ 9 driver advances 2664 mu for a full tracking update. A timed profile-only `write64` advances 1248 mu and uses a 1280 mu budget in the example.

The compiler and exported program check minimum driver budgets. The exported program verifies actual core/DDS and SPI clocks, direct TTL IO_UPDATE, shared update wiring and the supported profile API. Timed Square currently requires `io_update_delay=0`.

## Preparation and digital commits

In the legacy mode each shot configures single-tone profile 7, matched latency and phase-autoclear before the origin while RF is off.

1. `dds_prepare` writes a buffered profile with `write64`. Preparation may overlap the preceding output.
2. `dds_commit` schedules the IO_UPDATE rising edge. `dds_commit_off` schedules the falling edge 8 mu later. Independent channels can submit edges at the same timestamp.
3. `dds_effect` records the state change after the declared pipeline latency and emits no additional RTIO instruction.

```text
effect  = T - device.latency_mu - calibration.latency_mu
commit  = effect - square_latency_mu
prepare must complete by commit
```

While RF is off, settings can take effect before a finer-grid switch opening. The RF-on boundary remains at the requested time. During continuous output and for explicit phase commands, effect and commit must fall on their precise grids.

Adjacent Square intervals keep the RF switch open across changes of frequency, phase or amplitude. Preparation must respect prior commits, explicit reservations and incompatible submission edges. The original legacy implementation serialized preparation globally. Consult the continuous contract for independent-bus scheduling.

Every participating DDS in an `update_group` shares one commit edge. Persistent phase-autoclear clears all their accumulators at the edge, so each group commit prepares the state and recomputes the tracking POW for every participant, including channels with unchanged logical parameters. Different groups can commit together. Each reset incurs the 16-bit POW quantization precision. Active groups cannot mix this mode with RF-off `set_mu` or active RAM.

## Phase command effects

`shift_phase(q.frame, angle)` and `reset_frame(q.frame)` have zero logical duration and schedule real profile writes and commits. Shift adds the angle to the drive frame. Reset clears the frame while retaining the pulse's base phase and hardware calibration offset. The physical carrier continues to evolve.

A phase command can act in an RF-off interval or, on a long Square, in a parallel command branch. A terminal command is retained. The target includes the qubit's relevant DDS drives. If the shot has no DDS play, mapped Raman/RF defaults supply its state while RF stays off. Commands at one boundary collapse to the final frame and a coincident play uses it once. Concurrent writes from distinct branches to one frame are ambiguous and rejected. Tracking uses a common shot origin. A frequency jump alone does not implement integration of prior frequency history.

Missing mappings or indeterminate DDS state reject the operation. A RAM boundary update requires a separate `ram_phase_latency_mu` contract. The older RF-off RAM path cannot apply hardware frames. See [RAM boundary timing](ram_boundary_timing.md).

[The Square example](../examples/square_timing.py) exports continuous amplitude/frequency/phase updates across two independent boards and shifts/resets at 4 and 8 µs inside a 12 µs Square.

## Verification

The mock maintains buffered and committed state separately. It advances the preparation cursor with the real SPI sequence formula and checks missing writes, premature commits, incomplete shared groups, missing falls, pulse width and effect time. Timeline plots split continuous output at `dds_effect`.

`scripts/verify_square_driver.py` runs installed ARTIQ Python driver bodies against virtual RTIO and compares register words, SPI cursor advancement and digital edges. The compiler checks the configured coarse cycle (`DeviceSetup.rtio_coarse_mu`) and SED capacity (`rtio_lane_count`), both defaulting to 8. Exports align the origin to the coarse grid and check the core's `ref_multiplier`. The lane count must match gateware. Measure real CPU throughput, FIFO slack, RF/IQ output and clock synchronization separately and inspect RTIO analyzer traces and core logs.

Timed Square emits one kernel per point in both `auto` and `unrolled` modes. Long experiments use a finite static event sequence. Shots return to the host. DMA, hardware loops, cross-shot continuous replay and arbitrary intra-gate modulation fall outside this Square contract.
