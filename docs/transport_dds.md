# Transport with a crossed AOD and Spectrum DDS

In the reference hybrid setup, AWG A drives global RF and AWG B drives clock/Rydberg control. DDS card C drives the crossed X/Y AOD. C holds the terminal trapping frequencies and amplitudes through quantum gates, waits, shot boundaries and A/B bank changes. Simulation and SDK call records describe commands. Measure timing, coordinate calibration, trap depth, motion limits and survival separately.

```powershell
uv run --locked python examples/artiq_spectrum/transport_dds.py --report
```

The example writes to `output/artiq_spectrum/transport_dds/`. Declare the card mode in the project's `spec_db` and select instruments, channels and trigger wiring in the descriptor.

```python
spec_db = {
    "A": {"serial_number": "12345", "mode": "awg"},
    "B": {"serial_number": "12346", "mode": "awg"},
    "C": {
        "serial_number": "12347",
        "mode": "dds",
        "dds_routing": "16/5",
        # Replace these example values with the actual timing contract and measurements.
        "dds_trigger_latency_ns": 560.0,
    },
}
```

A missing `mode` selects AWG. The DDS target is M4i.6631-x8 with DDS20. At connection, inspect card type, options, core count and queue capacity. Configure and read back the route during preparation before starting output. `dds_trigger_latency_ns` is an independent DDS parameter.

| Route | Channel 0 | Channel 1 |
| --- | --- | --- |
| `20/1` | Cores 0–19 | Core 20 |
| `16/5` | Cores 0–7 and 12–19 | Cores 8–11 and 20 |

There is no arbitrary 10/10 allocation. Declare how the two axes map to channels and cores. The AOD uses tones as physical resources.

## Sites, trajectories and shared tones

```python
iz0 = IZ("iz0", sites=(destination_0, destination_1))
profile = MoveProfile.minimum_jerk(segments=32)

# Inside the experiment's realtime / triggered_sequence context.
move([q0, q1], to=[iz0[0], iz0[1]], duration=100 * us, profile=profile)
play(q0.clock, clock_pulse)  # Defined before the experiment body.
move([q0, q1], to=[q0.position, q1.position], duration=100 * us, profile=profile)
```

An `IZ` has two sites. Indexing returns an immutable `SiteRef`. A qubit UID identifies the atom across moves. `duration` is required. The default trajectory is linear with `settle=0`. A nonzero settle value explicitly adds waiting. A move to the same position holds it for the requested time.

Move start, duration and settle, the DDS region length and trigger prefix must match the 6.4 ns grid. Only internal normalized knot times are quantized, and slopes use the resulting segment durations. Coincident quantized knots are rejected. The compiler resolves the initial position from the experiment snapshot and each subsequent move from the previous endpoint on that branch. It leaves `q.position` untouched. Explicitly return each shot to its starting layout.

```python
linear = MoveProfile.linear()
custom = MoveProfile.from_samples([(0.0, 0.0), (0.2, 0.1), (0.8, 0.9), (1.0, 1.0)])
```

Normalized custom samples start at (0,0), end at (1,1), have strictly increasing time and nondecreasing displacement. A curve follows the line between its endpoints; use several moves for a spatial path. Minimum-jerk is a piecewise approximation to the continuous polynomial. The DDS executes linear ramp segments.

```python
transport = TransportConfig(
    card="C",
    x_calibration=((0, 70e6), (40e-6, 90e6)),
    y_calibration=((0, 70e6), (15e-6, 85e6)),
    bindings={
        "q0": {"x_core": 0, "y_core": 8},
        "q1": {"x_core": 1, "y_core": 8},
    },
    initial_amplitudes={0: 0.08, 1: 0.08, 8: 0.1},
    zones=(iz0,),
)
lab = LabConfig(setup, qubits=(q0, q1), transport=transport)
```

Each calibration row contains `position_m`, `frequency_hz` and optionally `amplitude_scale`. A monotone table maps position to frequency within its calibrated interval. The amplitude scale multiplies the core's base amplitude. The laboratory declares coordinate frames, tolerances, separation and physical bounds.

Moving one shared X or Y tone also moves the other atoms bound to it. Include all affected registered atoms with consistent trajectories. Check collisions, tone crossings/merges, conflicting destinations and calibration bounds. This version schedules serial moves or one explicitly coordinated batch. Optical coverage for a B-card channel can be restricted to `coverage={"zones":["iz0"]}`. One shared beam emits one waveform with its calibration source and actual affected targets recorded. Global RF coverage remains global.

## Continuous trapping output

On the first run C establishes an initial hold. Load the complete SINGLE-mode command packet while holding. An external ARTIQ trigger starts the pattern, DDS timers advance the ramps, and the terminal frame pins frequencies and amplitudes, zeros slopes and returns to CARD trigger mode. Completion uses the expected trigger count and error state. An empty software queue can mean the last command reached shadow registers before it took effect.

The queue budget includes initial hold and a conservative allowance of four routing/divider writes. Check actual software queue count before upload. The timer minimum of 83.2 ns, 6.4 ns update grid, full-frame preparation time and conservative 13-command-cycle guard all apply. A varying amplitude requires positive, measured `dds_amplitude_slope_step_per_s`. Constant amplitude does not. At adjacent moves the endpoint frequency pin consumes an update, so use an effective ramp interval reduced by 6.4 ns and record `ramp_update_delay_mu`. Intermediate knots update slopes without unnecessary frequency pins.

Keep the last trapping output on after `session.run()`. Closing the session stops C. A failure stops the affected output, invalidates the position and prevents further execution in that session. DDS preserves its own oscillator behavior and has no common software absolute phase ledger with AWG control.

[Compiled viewers](compiled_viewers.md) plot commanded frequency and amplitude on the AWG time axis. A hold continues to the edge of the known window. Unknown host intervals remain unspecified. Hardware references include the [card manual](https://spectrum-instrumentation.com/dl/m4i_m4x_66xx_96xx_manual_english.pdf), [DDS mode note](https://spectrum-instrumentation.com/download_files/application_notes/PN_Using_DDS_Mode_on_AWG.php), and the SDK examples for [routing](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/07_dds_multiple_carriers_channels.py), [ramps](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/12_dds_complex_ramps.py) and [repeated patterns](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/15_dds_repeated_patterns.py).
