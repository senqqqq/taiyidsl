# Run a hybrid AWG experiment

Choose the **clock on each card** in `spec_db` and the **output basis for the group** in the descriptor, before constructing the setup. Neither is a `connect()` argument. There is no clock-session ID to create, pass or copy. Changing the settings or profile requires rebuilding the setup and recompiling.

## Scope test: play the setpoints

```python
# Card already configured for a 125 MHz input and 1.25 GS/s sampling.
spec_db["A"]["clock_policy"] = "external_reference"
wiring = descriptor["sync_groups"]["clock_awg"]
wiring["output_basis"] = "setpoints_only"  # no calibration file
```

Without an external reference use `"internal_pll_bench"` on each AWG instead; omit `reference_clock_hz`. Each card must already be using its own PLL at 1.25 GS/s. Two internal-clock cards must be in one explicit `setpoints_only` group. Use `"triggers": {"spcm0": "ttl20", "spcm1": "ttl21"}` to connect each EXT0 to its own Kasli TTL. The kernel schedules the edges together, **not** the analog carrier phases. Do not mix internal and external clocks in this bench group. Setpoint playback uses sequence-relative phase and AWG mode, not shot-wide phase or DDS transport.

Build the experiment as usual. The hardware step is the same in every case:

```python
compiled = lab.compile(exp)
with lab.connect() as session:
    receipt = session.run(compiled)
```

The worker reads back the clock configuration, checks the cards and uploaded program, waits for **all** cards to arm before the shot, then waits for completion. It does not switch clocks or retry the shot. `output_basis="setpoints_only"` means the analog delay, gain and absolute phase were not calibrated or verified; a successful receipt is not a scope measurement. The external-reference register alone does not prove shared wiring or lock continuity.

## Apply a measured correction

```python
import json
from pathlib import Path

wiring = descriptor["sync_groups"]["clock_awg"]
wiring["output_basis"] = "measured"
wiring["calibration"] = json.loads(Path("my_lab/awg_calibration.json").read_text())
```

Use external reference on every participating card. The strict path refuses missing calibration, a different hardware fingerprint, changed profile bytes or a missing exact carrier point. It does **not** detect a past PLL relock; after a power cycle, relock or wiring change the lab must remeasure before relying on physical phase. A legacy profile can be used unchanged (its entire contents are hashed); a token in it is not checked as evidence of lock. Do not attach a profile to `setpoints_only`: that conflict is rejected, not silently ignored.

The profile is an ordinary JSON object returned by `calibrate_channels()`: `revision`, `status`, per-card `card_fingerprints`, `channels` and a calibration dataset digest. `channels["A:0"]` holds the original `delay_ns`, `sample_rate_hz`, integer `sample_shift_samples`, residual delay and timing uncertainty. Its `frequency_points` contain the **output carrier** (not the reference clock), transfer gain, residual phase correction and uncertainty. [Derivation and fields](spectrum_hybrid.md#derive-calibration-from-measured-captures). Calibration estimates and independent physical acceptance are separate.

**Existing defaults:** an external-reference group still requires measured calibration for real execution unless setpoints were explicitly selected. The original single-card internal PLL notebook still plays setpoints without a new option. Neither path silently falls back.

Open the [one-card tutorial](tutorials/artiq_spectrum_single_ttl_clock_zh.ipynb) or the [two-card, two-TTL tutorial](tutorials/artiq_spectrum_dual_ttl_clock_zh.ipynb). Both are offline until explicitly enabled.
