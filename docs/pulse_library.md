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

An array's **source** interval is required. For `N` samples and interval `dt`, sample `k` is at `k * dt`, and the duration is `N * dt`. Construction snapshots a finite, nonempty 1-D array of unit-normalized samples. Its read-only `pulse.samples` and its duration retain their original grid.

| `kind` | Values | Output before DAC quantization |
| --- | --- | --- |
| `"envelope"` | Signed real, −1 to 1 | Envelope × amplitude × cos(θ) |
| `"iq"` | Complex I + jQ, magnitude ≤ 1 | Amplitude × (I cos(θ) − Q sin(θ)) |
| `"waveform"` | Final real samples, −1 to 1 | Samples × gain. The pulse gain defaults to one. |

Envelope and IQ follow the selected carrier and frame, including `play(..., phase=...)`. A final waveform can already include its own RF carrier. Set `amplitude` to scale it. Device gain and channel delay still apply. Frequency overrides, phase rotations, and detuning are rejected on this form.

**Resampling happens during compilation.** The current 1.25 GSa/s Spectrum AWG outputs one DAC sample every 0.8 ns. The default `resample="strict"` requires matching source and card intervals. With `resample="linear"`, the compiler interpolates at each DAC time. It interpolates I and Q independently and holds the last source value through the final source interval. The pulse ends at the declared time. For downsampling this is linear interpolation without an anti-alias filter.

For example, 800 values spaced at 1 ns occupy 800 ns and produce 1000 DAC samples. The last two DAC times are 798.4 ns and 799.2 ns. At 799.2 ns the last source value is held. An array of 801 values at 1 ns would require 1001.25 DAC samples, so compilation raises a timing error. Hardware padding after a complete shot is recorded separately.

Array pulses work on AWG-mode outputs in the `spectrum` and `artiq_spectrum` backends. Use a `triggered_sequence` for the hybrid configuration. A DDS20 output or a native ARTIQ output raises a compilation error for sampled pulses. See the [single-card](spectrum_standalone.md) and [hybrid](spectrum_hybrid.md) examples for lab configuration.

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
