# Spectrum carrier and modulation phase

A `triggered_sequence` defaults to a carrier epoch at sequence entry. MOT or cooling duration before the sequence can therefore vary without changing the samples in an independent control sequence. Select `phase_reference="shot"` to continue from shot T0 or a later explicitly established epoch. Within a shot, Sections, operations, chunks, segments and tiles preserve phase state.

## Drive frame and oscillator

Each shot starts at F = 0. `shift_phase(frame, delta)` adds delta, and `Rz(q, theta)` subtracts theta from the same frame. `reset_frame` clears F, including all Rz contributions. `reset_phase` also clears inherited modulation and resets the carrier reference at the instruction's final scheduled position. It cannot interrupt an active pulse. Pulse-local phase and calibration still apply. Sequence entry preserves explicit frame shifts before it, so a first DAC sample can be nonzero.

```python
SpectrumOutput("A", 0)  # Default carrier_mode="epoch"

SpectrumOutput(
    "A",
    0,
    carrier_mode="continuous",
    initial_frequency_hz=7e6,
)
```

`initial_frequency_hz` for continuous mode is a physical output frequency. An epoch-mode output rejects this unused setting. For a pulse with physical frequency fᵢ and reference time Tref:

- **Epoch** uses `C(t) = 2π fᵢ (t − Tref)` for that pulse.
- **Continuous** starts at `C(Tref) = 0` and integrates the physical frequency piecewise. It uses the configured initial frequency before the first pulse and the previous frequency in a gap. A carrier reset leaves the current frequency in effect.

Two aliases for one physical output share oscillator and modulation state. Different physical channels keep separate state. A zero-duration play consumes no samples and changes neither frequency nor modulation state.

## Pulse-local and carried modulation

```python
pulse = Square(
    400 * ns,
    frequency=11e6,
    phase_modulation=PolynomialPhase((0.2, 0.7)),
    phase_modulation_mode="carry",
)
play(q.rf, pulse)
play(q.rf, pulse, phase_modulation_mode="local")
```

Let `u=(t−tᵢ)/T` and let m(u) be the modulation. The binding has phase scale s and offset b. Pulse base phase is p and calibration phase is c.

```text
Phi_osc(t) = C(t) + M
phi_pulse(u) = p + m(u)
θ(t) = Phi_osc(t) + s [F(t) + phi_pulse(u)] + b + c
RF(t) = amplitude × envelope(t) × cos(θ(t))
```

In `local` mode, M after the pulse is unchanged. In `carry` mode, update `M ← M + s m(1)` once at the end. Both modes play the same samples for this pulse. Carried phase is already in the physical output frame and must not be scaled again on the next pulse. Base phase p, offset b and calibration c do not carry. F follows the control frame, while M follows the physical output.

```text
f_inst = f_i + s m'(u)/(2πT)
Additional phase accumulated over the smooth part = s [m(1) − m(0)]
```

A pulse with `m(u)=0.2+0.7u` starts with a 0.2 rad jump and advances another 0.7 rad. Its complete carried endpoint is 0.9 rad. `HalfSinePhase(a)` has zero analytic endpoints. Evaluate the analytic endpoint at u=1 for inheritance. The N emitted samples use u=n/N for n from 0 to N−1. Using the last DAC sample as the endpoint would make the next pulse's phase depend on the sample rate.

## Scheduling and diagnostics

```python
with exp.realtime():
    rz(q, 0.3)
    with exp.triggered_sequence(group="qubit_awg"):
        play(q.rf, pulse)
    rz(q, 0.7)
```

The first virtual Z changes the phase of subsequent pulses. The final instruction changes the terminal frame only. `frame_phases()` reports F. `virtual_z_frames()` gives virtual-Z provenance since reset. A region also records `terminal_frames`. Outside a sequence, serial Rz, shift and reset are supported for a pure Spectrum frame in the same group. Mixed DDS/AWG frames, cross-domain Rz in external parallel branches and multiple independent starts of one group in a shot require different contracts.

Determine final pulse times first, then F, carrier, modulation, mapping and calibration, and finally DAC words. Deduplication, tiling and resident replay preserve these results. `phase_evolution` records oscillator start/end, M on entry/exit, modulation endpoints, discontinuities and the smooth instantaneous frequency range. An out-of-range smooth frequency produces `spectrum_modulation_frequency_range` diagnostics. Check finite samples and nominal output frequency bounds. Actual bandwidth and optical response require measurement.

ARTIQ phase reset compensates for known DDS state under the particular square, ASF RAM or POWASF update contract. Its continuous NCO path requires one fixed FTW per DDS per shot. See [ARTIQ phase switching](phase_switching_and_virtual_z.md).

```powershell
uv run --locked python examples/artiq_spectrum/phase_modes.py
uv run --locked python examples/artiq_spectrum/phase_modes.py --reports
```

The first command compares the four carrier/modulation mode combinations and writes `output/artiq_spectrum/phase_modes/phase_comparison.json`. The second additionally exports four reports. In the example the second pulse follows a frame reset that retains carry and carrier evolution. The third follows a phase reset that clears those histories.
