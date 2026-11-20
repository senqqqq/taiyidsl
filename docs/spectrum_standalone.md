# Single-card Spectrum playback

Select `"backend": "spectrum"` in the descriptor to play a complete DSL shot on one M4i.6631-x8. The card supplies its internal clock. The host starts each shot once and waits for confirmed completion. Pulse edges and idle periods within the shot follow the card clock. The interval between shots depends on the host. Scan points and repetitions follow the experiment's host execution order.

## Select the card and mode

```python
from taiyidsl import DeviceSetup, LabConfig

spec_db = {"A": {"serial_number": "OFFLINE", "mode": "awg", "active_channels": [0]}}
descriptor = {
    "backend": "spectrum",
    "instruments": {"A": {"kind": "spectrum", "device": "A"}},
    "channels": {"rf_out": {"instrument": "A", "channel": 0}},
    "signals": {"q0": {"rf": "rf_out"}},
}
setup = DeviceSetup.from_descriptor(descriptor, spec_db=spec_db)
lab = LabConfig(setup)
print(setup.describe()["instruments"]["A"]["settings"]["mode"])
```

The default mode is `"awg"`. Select `"dds"` on a card equipped with the DDS20 option to generate gated RF carriers. To switch modes, change the card record, rebuild the setup and compile the same experiment again. The standalone descriptor needs only the card, its output routing and its logical signals.

## Write and compile a shot

```python
import math

from taiyidsl import Experiment, MHz, Qubit, Square, delay, ns, play

q0 = Qubit("q0")
pulse = Square(640 * ns, amplitude=0.2, frequency=10 * MHz)
exp = Experiment("two_pulses", [q0])
with exp.average(20), exp.realtime():
    play(q0.rf, pulse)
    delay(1280 * ns)
    play(q0.rf, pulse, phase=math.pi / 2)

compiled = lab.compile(exp)
compiled.export("output/spectrum_standalone/two_pulses")
```

The first pulse occupies 0–640 ns, and the second occupies 1920–2560 ns from the card's shot origin. The second pulse adds a quarter-turn to the carrier phase at 1920 ns. The outputs return to zero. A leading or trailing `delay` also counts toward the shot duration.

| Mode | Card program | Timing and waveform limits |
| --- | --- | --- |
| AWG | DAC segments and a finite sequence step table | The sample grid is 0.8 ns. Square, flat-top Gaussian and supported phase modulation use the existing waveform renderer. Segment alignment can add a documented zero tail after the logical shot. |
| DDS20 | Timed commands for one RF carrier on each enabled output | A Square has a rectangular amplitude envelope and a sinusoidal carrier. Frequency, amplitude and phase can change between pulses. Edges lie on the 6.4 ns command grid and must allow time to prepare each update. Unsupported envelopes, phase modulation and infeasible command intervals raise compilation errors. |

A 400 ns Square fits the AWG grid but falls halfway between DDS updates. A 403.2 ns Square lies on both grids. For the three-frequency, 20-shot example, run these offline commands from the project root.

```shell
uv run --locked python examples/spectrum_standalone.py --mode awg
uv run --locked python examples/spectrum_standalone.py --mode dds
```

Both exports include the timing and compiled card program. AWG validation inspects the quantized samples. DDS validation checks the hardware command sequence. To run on a bench, install the Spectrum native driver and the optional Python SDK (`uv sync --locked --extra spectrum`). Supply the physical serial number in the card record and prepare a fresh setup. Connect explicitly after compilation.

```python
lab = LabConfig(setup, emulation=False)
compiled = lab.compile(exp)
with lab.connect() as session:
    receipt = session.run(compiled)
```

For scope acceptance, connect the selected output to a properly terminated scope input and trigger on the first burst. Capture each mode separately. Measure the two 640 ns envelopes and the 1280 ns gap. Fit the carrier phase in each burst against a common time origin, including the dark interval, and check the additional quarter-turn. Record the card serial number, output range, scope timebase and compiled artifact identity with the capture. This bench procedure measures the analog output; an execution receipt records the driver's completion status.
