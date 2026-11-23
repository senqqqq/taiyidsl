# Experiment factories, sequence triggers and independent TTL pulses

The decorator and context functions use the same builder and compiler as `Experiment` and its methods. The [Ramsey example](../examples/artiq_spectrum/decorated_ramsey.py) builds, compiles and exports two sweep points offline.

## Start a sequence on multiple cards

```python
with exp.triggered_sequence(group="qubit_awg", trigger="qubit.start"):
    play(q.rf, p1)
    delay(tau)
    rz(q, math.pi / 2)
    play(q.rf, p2)
```

The signal map resolves `qubit.start` to the TTL outputs wired to the synchronization group. Configure either a shared `trigger_device="awg_trigger"` or per-card lines such as `trigger_devices={"A": "awg_trigger_a", "B": "awg_trigger_b"}`. All lines rise and fall at the same scheduled RTIO times. Each card's contract records its own line. The logical trigger must resolve to precisely those endpoints, once each, with identity amplitude, frequency and phase transforms.

```text
q.rf → nuclear_rf → qubit_awg / card A CH0
qubit.start → awg_trigger_a → card A external trigger
            → awg_trigger_b → card B external trigger
```

Set `group.trigger_signal="qubit.start"` to select the default when the sequence omits `trigger`. Older configurations without a logical trigger name use `trigger_device`. Add cards C, D and their TTLs in the same way. Run `python -m examples.artiq_spectrum.multicard_trigger --cards 4` to exercise the two-, three- or four-card example (select the count with `--cards`). Available TTLs, the time grid and RTIO submission capacity bound the group size. Physical sample alignment also requires a shared reference, verified wiring and measured delays for each card. The `SIM_*` identifiers, widths and delays in the example are illustrative.

A block containing playback launches the group **once**, regardless of the number of gates or trigger lines. A frame-only block can omit a hardware launch. The compiler schedules the trigger ahead of the sequence origin by the configured lead. The card runs its static gates, delays and virtual Z operations autonomously. The compiler owns the active group's trigger outputs for the shot, including aliases. Give an independently triggered instrument a separate TTL.

## Pulse an independent TTL

```python
with exp.realtime():
    trig("scope.trigger", width=64 * ns)
```

`trig` occupies its output and advances the serial branch by the pulse width (64 ns here). The width must be positive and lie on the mapped device grid. A time sweep parameter can supply the width. Parallel alignment and joins work as usual. The branch can continue as soon as the TTL pulse ends, irrespective of how long the external instrument takes.

| `Device.ttl_idle_level` | Idle | During pulse | End of pulse |
| --- | --- | --- | --- |
| `0` (default) | Low | High | Return low |
| `1` | High | Low | Return high |

The example configures `scope_ttl` with `ttl_idle_level=1`, so the falling edge starts the pulse. ARTIQ initializes the output to the declared idle level and restores it after every pulse. The low transition is a real digital event. Digital triggers accept a width only. Insert an explicit idle interval, for example `delay("scope.trigger", 1 * us)`, to separate consecutive pulses. The compiler neither merges adjacent triggers nor inserts idle time. Existing TTL `play(signal, Square(...))` calls generate active-high pulses. Use `trig` for an idle-high output.

`trig` runs in pure ARTIQ or hybrid realtime, outside a Spectrum `triggered_sequence`. It has no instrument upload, arm, capture, acquisition handle or phase side effects. Use `acquire` for shot-associated results and `triggered_sequence` for AWG compilation and group readiness.

## Build a fresh experiment with a decorator

```python
from taiyidsl import experiment, average, sweep, realtime, triggered_sequence


@experiment(qubits=[q], name="ramsey")
def ramsey(dark_time, shots=100):
    with average(shots):
        with sweep(dark_time):
            with realtime():
                with triggered_sequence(group="qubit_awg", trigger="qubit.start"):
                    trigplay(q.rf, pulse)
                    delay(dark_time)
                    rz(q, math.pi / 2)
                    trigplay(q.rf, pulse)


exp = ramsey(dark_time)
compiled = lab.compile(exp)
```

Each call to `ramsey` returns a new `Experiment`. The decorator fixes the qubit membership at declaration. Each build snapshots values, parameters and metadata. By default the experiment takes the function name, and the wrapper preserves the function signature and documentation. The builder must be a synchronous function returning `None`. Generators, async functions and native kernels are unsupported. Ordinary Python executes at build time on the host. Compilation, export and execution are separate calls.

The module-level `average`, `sweep`, `realtime`, `triggered_sequence`, `repeat`, `case`, `stage`, `serial` and `parallel` contexts delegate to the active experiment. They can be mixed with calls such as `with exp.average(...)` and preserve the IR, timing, phase, sweep order and backend. A stage is optional. An active experiment can call a Python helper, `section()` or a `@qubit_operation`. Calling a decorated whole-experiment factory inside it raises an error. If building fails, the previous context is restored. See [Sections and operations](sections_and_operations.md).

Keep ARTIQ kernels in a separate module and declare them through `NativeKernelPackage`. Both ARTIQ and the DSL define names such as `delay` and `parallel` with different behavior. Use explicit imports.

Inside `triggered_sequence`, `play` and the existing block-only `trigplay` both express Spectrum playback. Native ARTIQ, acquisitions and host sweeps belong outside. A serial position in the enclosing realtime block may update a Spectrum-only frame belonging to the same group with `rz`, `shift_phase`, `reset_frame` or `reset_phase`. Entry preserves explicit frame updates. The default carrier reference starts at sequence entry. Select `phase_reference="shot"` to use shot T0. See [Spectrum phase modes](spectrum_phase_modes.md).

## Run the example and inspect phase

```powershell
uv run --locked python examples/artiq_spectrum/decorated_ramsey.py
uv run --locked python examples/artiq_spectrum/decorated_ramsey.py `
  --default-trigger --output output/artiq_spectrum/decorated_ramsey_default
```

The example copies the shared profile in memory and adds a logical trigger route and independent scope TTL. Both commands select the same `awg_trigger`. The second exercises the group default. Passing `None` has the same effect as omitting the trigger.

The dark delays are 10 µs and 100 ms. The frame offset F starts at zero each shot. With the default sequence reference the 20 µs DDS prelude contributes no AWG carrier phase. The in-sequence dark delay advances the carrier, and a positive Rz(π/2) subtracts 90° from the following drive. The scope TTL leaves both the carrier reference and the qubit frame unchanged.

The report shows the DSL, both cards' segment and step tables, AWG and scope trigger timing, the pulse sheet, and the final DAC samples. It models digital scope triggering. It contains no scope acquisition, analog AOM response or atomic state simulation. For a timed native GreenMOT preparation tail see [Native ARTIQ integration](native_artiq_integration.md).
