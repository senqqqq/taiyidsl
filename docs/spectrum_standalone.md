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
| AWG | DAC segments and a finite sequence step table | The sample grid is 0.8 ns. Use Square, Gaussian, GaussianSquare or [sampled envelope, IQ and waveform arrays](pulse_library.md). Segment alignment can add a documented zero tail after the logical shot. |
| DDS20 | Timed commands for one RF carrier on each enabled output | A Square has a rectangular amplitude envelope and a sinusoidal carrier. Frequency, amplitude and phase can change between pulses. Edges lie on the 6.4 ns command grid and must allow time to prepare each update. Unsupported envelopes, phase modulation and infeasible command intervals raise compilation errors. |

A 400 ns Square fits the AWG grid but falls halfway between DDS updates. A 403.2 ns Square lies on both grids. For the three-frequency, 20-shot example, run these offline commands from the project root.

```shell
uv run --locked python examples/spectrum_standalone.py --mode awg
uv run --locked python examples/spectrum_standalone.py --mode dds
```

Both exports include the timing and compiled card program. AWG validation inspects the quantized samples. DDS validation checks the hardware command sequence. To run on a bench, install the Spectrum native driver and the optional Python SDK (`uv sync --locked --extra spectrum`). Supply the physical serial number in the card record and prepare a fresh setup. If you also specify `address`, use the device path there, not in `serial_number`.

## Run a shot and release the card

Compiling and exporting do not open the card. For a physical lab, `lab.connect()` opens the card immediately. Keep the `with` block as short as you want the card to be reserved:

```python
lab = LabConfig(setup, emulation=False)
compiled = lab.compile(exp)
with lab.connect() as session:
    receipt = session.run(compiled)  # Wait for all scheduled shots; the card is still open.
# The session is now closed. Analyze the receipt here, without holding the card.
```

You can run another compatible experiment inside the same block. When the block ends, whether normally or because of an exception, it calls `session.close()` for you. After playback setup, cleanup attempts to stop replay and DMA, disable the outputs and close the device handle. No separate stop command is needed after a successful run.

If you manage the session yourself (for example, in a notebook), use `finally` so an exception cannot skip the close:

```python
session = lab.connect()
try:
    receipt = session.run(compiled)
finally:
    session.close()
```

Calling `close()` again is harmless. To run after closing, open a new session with `lab.connect()`.

### Interrupting a run or finding the process that owns the card

- **Your run is still in progress:** interrupt the Python process that called `run()` (Ctrl+C in its terminal, or **Interrupt Kernel** in a notebook). Once Python handles the interrupt, the cleanup path can run. `close()` from another thread is not an asynchronous stop: it waits for the same session lock as `run()`. An interrupted shot may already have emitted RF.
- **Another script owns the card:** the SDK can report “not found” even for a present but busy card. On Linux, set `DEVICE` to the configured device node and run `fuser -v "$DEVICE"` or `lsof "$DEVICE"` to look for the owning process. End that script normally in its own terminal or notebook, then reconnect. An open file handle does not establish whether a waveform is being emitted; use a scope to verify the output.
- **You only want to inspect the card:** in the [plain Python example](../examples/spectrum_standalone_plain.py), `ACTION = "compile"` stays offline (the default), `"check"` opens an available card and prints its identity without playing a shot, and `"run"` plays the experiment. `session.observed` is a snapshot of identity and capabilities, not live playback status. The standalone DSL does not monitor or stop another process's session.

## Verify analog output

For scope acceptance, connect the selected output to a properly terminated scope input and trigger on the first burst. Capture each mode separately. Measure the two 640 ns envelopes and the 1280 ns gap. Fit the carrier phase in each burst against a common time origin, including the dark interval, and check the additional quarter-turn. Record the card serial number, output range, scope timebase and compiled artifact identity with the capture. This bench procedure measures the analog output; an execution receipt records the driver's completion status.
