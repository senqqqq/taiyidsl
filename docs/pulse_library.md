# Pulse shapes and NumPy samples

`Pulse` is the common type for declarations accepted by `play()`. Construct a shape directly, call a Python factory, or build a `SampledPulse` with `Pulse.from_array()`. The same pulse can be used by multiple plays, Sections and scan points.

## Gaussian shapes

```python
from taiyidsl import Gaussian, GaussianSquare, MHz, ns

bell = Gaussian(
    sigma=16 * ns,
    cutoff_sigma=4,
    amplitude=0.2,
    frequency=10 * MHz,
    sample_interval=0.8 * ns,
)
flat = GaussianSquare(
    flat_duration=160 * ns,
    sigma=16 * ns,
    cutoff_sigma=4,
    amplitude=0.2,
    frequency=10 * MHz,
    sample_interval=0.8 * ns,
)
```

Each Gaussian edge spans `cutoff_sigma * sigma`. The truncated envelope is shifted to zero at its boundaries and normalized to one at its peak or plateau. The examples last 128 ns and 288 ns respectively. `GaussianSquare` is an alias for `FlatTopGaussian`, and `Gaussian` constructs the same shape with zero plateau. Both preserve the original analytic shape's 20 ns default interval. Specify the card interval for analytic shapes on Spectrum AWG.

## Import an array

```python
import numpy as np
from taiyidsl import Experiment, Pulse, Qubit, MHz, ns, play

q0 = Qubit("q0")
samples = np.hanning(800)
pulse = Pulse.from_array(
    samples,
    sample_interval=1 * ns,
    kind="envelope",
    resample="linear",
    amplitude=0.2,
    frequency=10 * MHz,
)
exp = Experiment("sampled_envelope", [q0])
with exp.average(1), exp.realtime():
    play(q0.rf, pulse)
# Compile with the standalone AWG lab in the single-card playback guide.
```

An array's **source** interval is required. For `N` samples and interval `dt`, sample `k` is at `k * dt` from the start of the active waveform. The active duration is `N * dt`. Construction snapshots a finite, nonempty 1-D array of normalized samples. The read-only `pulse.samples` retains the source grid.

| `kind` | Values | Output before DAC quantization |
| --- | --- | --- |
| `"envelope"` | Signed real, −1 to 1 | Envelope × amplitude × cos(θ) |
| `"iq"` | Complex I + jQ, magnitude ≤ 1 | Amplitude × (I cos(θ) − Q sin(θ)) |
| `"waveform"` | Final real samples, −1 to 1 | Samples × gain. The pulse gain defaults to one. |

Envelope and IQ follow the selected carrier and frame, including `play(..., phase=...)`. A final waveform can already include its own RF carrier. Set `amplitude` to scale it. Device gain and channel delay still apply. Frequency overrides, phase rotations, and detuning are rejected on this form.

**Resampling happens during compilation.** The current 1.25 GSa/s Spectrum AWG outputs one DAC sample every 0.8 ns. The default `resample="strict"` requires matching source and card intervals. With `resample="linear"`, the compiler interpolates at each DAC time. It interpolates I and Q independently and holds the last source value through the final source interval. The pulse ends at the declared time. For downsampling this is linear interpolation without an anti-alias filter.

For example, 800 values spaced at 1 ns occupy 800 ns and produce 1000 DAC samples. The last two DAC times are 798.4 ns and 799.2 ns. At 799.2 ns the last source value is held. An array of 801 values at 1 ns would require 1001.25 DAC samples, so compilation raises a timing error. Hardware padding after a complete shot is recorded separately.

Array pulses work on AWG-mode outputs in the `spectrum` and `artiq_spectrum` backends. Use a `triggered_sequence` for the hybrid configuration. A DDS20 output or a native ARTIQ output raises a compilation error for sampled pulses. See the [single-card](spectrum_standalone.md) and [hybrid](spectrum_hybrid.md) examples for lab configuration.

## Sweep the carrier of an IQ pulse

Use a complex envelope when you already know the amplitude `A` and phase `phi` at each sample. In the cosine convention, `z = A * exp(1j * phi) = I + 1j * Q`, with `I = A * cos(phi)` and `Q = A * sin(phi)`. For carrier angle θ, multiplying out the envelope and carrier gives

```text
(I + jQ)(cos θ + j sin θ) = (I cos θ − Q sin θ) + j(I sin θ + Q cos θ).
```

The DAC uses the real part. Both components contribute because `jQ * j sin θ = −Q sin θ` is real. Substituting I and Q gives `A * cos(phi) * cos(θ) − A * sin(phi) * sin(θ) = A * cos(θ + phi)`. For instance, at `phi = π/2`, the envelope is purely imaginary and the real output is `−A * sin(θ)`. To recover magnitude and phase from I and Q, use `np.hypot(I, Q)` and `np.arctan2(Q, I)`. Phase is undefined at zero magnitude.

This example uses the `lab` from the [standalone AWG setup](spectrum_standalone.md#select-the-card-and-mode). The pulse starts at the beginning of the shot with zero frame offset.

```python
import numpy as np
from taiyidsl import Experiment, Pulse, Qubit, SweepParameter, ns, play

q0 = Qubit("q0")
n = np.arange(1000)
A = np.hanning(len(n))
phi = 0.6 * np.sin(2 * np.pi * n / len(n))  # radians
pulse = Pulse.from_array(
    A * np.exp(1j * phi),
    kind="iq",
    sample_interval=0.8 * ns,
)

f = SweepParameter("carrier", [8, 10, 12], unit="MHz")
exp = Experiment("iq_carrier_scan", [q0])
with exp.average(1), exp.sweep(f), exp.realtime():
    play(q0.rf, pulse, frequency=f, amplitude=0.5, phase=0)

compiled = lab.compile(exp)
```

Each point reuses the same 800 ns envelope. At time `t` seconds from shot start, the requested output is `0.5 * A[n] * cos(2π * f * t + phi[n])` with `f` in hertz. To give the pulse a default carrier, set `frequency` in `Pulse.from_array()`. A frequency supplied to `play()` overrides that default for that play. You can likewise bind a sweep parameter to `amplitude` for an overall gain or to `phase` for an additional phase offset.

## Explore and crop a waveform CSV

`load_waveform_csv(path)` reads a rectangular CSV into a one-dimensional NumPy structured array. Each row is a record. Use `table.dtype.names` to see all column names, `table["name"]` for a single column, and a slice or a boolean mask to select rows. Columns can contain labels, times, real values, or complex values written as `0.2-0.4j` or `0.2-0.4i`. Values retain their original scale. Row order and repeated time stamps are preserved.

For example, a file can contain several named waveforms with a separate time axis for each:

```python
from taiyidsl import load_waveform_csv

table = load_waveform_csv("10.2_waveforms.csv")
print(table.dtype.names)
shaped = table[table["scheme"] == "pulse_shaped"]
window = shaped[(shaped["time_us"] >= 4) & (shaped["time_us"] < 8)]
time_us = window["time_us"]
iq_khz = window["inphase_khz"] + 1j * window["quadrature_khz"]
```

The window contains the original rows whose time lies in `[4, 8)` µs. This selection creates no new boundary samples. A real waveform column or an existing complex column can be selected directly, for example `window["voltage"]` or `window["z"]` if those names appear in the file. To play irregular time nodes, choose how to handle jumps and resample to a uniform time grid with a known interval. Convert physical values such as kHz or volts to normalized samples using the scale appropriate to the experiment before calling `Pulse.from_array()`.

The default is UTF-8 (with optional BOM), comma-delimited, with a header of unique nonempty names. For a headerless file, use `header=False` to obtain `col0`, `col1`, etc. Use `delimiter=";"` for a semicolon-separated file, and `text_columns=("id",)` to retain identifiers such as `001` verbatim. Numeric columns with empty cells use NaN. A column mixing numbers and labels retains its text.

## Read a normalized IQ CSV and pad the pulse

Save one uniformly spaced sample per row, with a header. For example, `waveforms/burst.csv` could contain four samples spaced 1 ns apart, with normalized amplitude and phase in degrees.

```csv
A,phi
0.5,0
0.5,90
0.5,180
0.5,270
```

```python
from pathlib import Path

from taiyidsl import Pulse, load_iq_csv, ns

iq = load_iq_csv(Path("waveforms") / "burst.csv", phase_unit="deg")
padded = Pulse.from_array(
    iq,
    kind="iq",
    sample_interval=1 * ns,
    resample="linear",
    pad_before=1.6 * ns,
    pad_after=2.4 * ns,
)
```

`load_iq_csv()` returns a complex NumPy array in file order. For `A,phi`, the default phase unit is radians. For `I,Q`, it returns `I + 1j * Q` directly. Select custom column names with `amplitude="gain", phase="angle"` or `i="in_phase", q="quadrature"`. Supply the uniform source spacing to `Pulse.from_array()`.

Both pads are optional durations and default to zero. They apply to all three sample kinds. On the 0.8 ns DAC grid the example plays two zero samples, five active samples and three zero samples. Linear interpolation applies only inside the active interval. The original `padded.samples` still contains four values. The full 8 ns count toward scheduling. At the first active sample, the carrier has advanced by `2π * f * pad_before` from its phase at pulse start. Each pad and the active duration must individually fit the target grid.

To add one microsecond of silence on just one side, import `us` from `taiyidsl` and pass `pad_before=1 * us` or `pad_after=1 * us`. For equal padding, pass `pad_both=1 * us` alone. This adds 1 µs on each side and extends the pulse by 2 µs in total. Each microsecond occupies 1250 DAC samples at 1.25 GSa/s.

## Name a custom pulse

```python
import numpy as np
from taiyidsl import Pulse, PulseLibrary, ns


def hann_burst(count, *, gain):
    return Pulse.from_array(
        np.hanning(count),
        sample_interval=1 * ns,
        kind="waveform",
        resample="linear",
        amplitude=gain,
    )


pulses = PulseLibrary()
pulses.register("hann_burst", hann_burst)
burst = pulses.make("hann_burst", 800, gain=0.25)
```

Each library has its own names. A factory takes ordinary Python arguments and returns a pulse. Calling `hann_burst(800, gain=0.25)` directly works too. Compilation captures the resulting declaration. Each self-contained playback manifest and compiled JSON stores each distinct source array once by content hash, shared by repeated plays and scan points. Replay uses the captured samples and needs no factory or registration.
