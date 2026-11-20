# Timing, reuse and multi-qubit experiments

The DSL schedules instructions in source order by default. Put simultaneous work inside `parallel` and group the steps of each branch with `serial`. Separate signal names do not imply independent scheduling. Two branches using the same physical resource may conflict.

## Scheduling rules

| Construct | Behavior |
| --- | --- |
| Consecutive `play` calls | The second starts after the first ends, even on different signals. |
| `with exp.parallel():` | Each direct child starts at a common time. The block exits when the longest branch finishes. |
| `with exp.serial():` | Children run in order. The block can serve as a single parallel branch. |
| `parallel(align="end")` | Branches end together. Shorter branches start later. |
| `delay(signal, duration)` | Advances its serial branch independently of other branches. |
| `serial/parallel(exact_duration=...)` | Asserts that the realized duration matches exactly. No implicit padding. |
| `repeat(count)` | Statically expands repetitions within a shot for each sweep point. The count may be an integer sweep parameter. |
| `average(shots, mode=...)` | Repeats on the host. Cyclic and sequential modes specify the order of points and shots. |
| `sweep([amp, duration])` | Binds values at the same index on one host axis. The parameters must have equal lengths and distinct names. |
| Nested `sweep` | Forms a Cartesian product of axes. |
| `case(parameter, value)` | Selects a structural branch at compile time for the current point. |

Use a `Section` or `@qubit_operation` to reuse a subtree. Where independent qubits should start as soon as possible, opt in with `section(schedule="asap")`. It respects qubit, hardware-output and frame dependencies, and joins at exit. For details see [Sections and operations](sections_and_operations.md). For host order with `average(scope=...)` and `sweep(chunks=... / chunk_size=...)`, see [Chunked execution](chunked_execution.md).

An explicit parallel request still has to fit the hardware. Two logical signals may map to one DDS or contend for another exclusive resource. A timed Square can prepare the next configuration during the preceding output and change parameters back-to-back if programming throughput permits. The compiler rejects infeasible timing. Legacy single-tone and RAM paths have different preparation windows. Adjacent Squares with identical parameters on the same signal may lower to one continuous RF output. To require RF-off between gates, insert an explicit `delay` and check its duration against the selected path. See the [Square timing contract](square_timing.md).

## Express exact offsets with a Python helper

The [parallel experiments example](../examples/parallel_experiments.py) defines a two-pulse branch.

```python
def pulse_pair(exp, qubit, pulse, *, gap=2 * us, second_phase=1):
    with exp.serial():
        play(qubit.raman, pulse)
        delay(qubit.raman, gap)
        play(qubit.raman, pulse, phase=second_phase)
```

The inner `serial` makes the whole pair one child of an enclosing parallel block. Without it each instruction would become a separate parallel branch. Overrides on `play` apply to that call and preserve the pulse template.

```python
pulse = Square(4 * us)
with exp.parallel():
    pulse_pair(exp, q0, pulse)
    with exp.serial():
        delay(q1.raman, 2 * us)
        play(q1.raman, pulse, duration=6 * us)
acquire(q0.readout, duration=10 * us, handle="population", qubits=[q0, q1])
```

Place this code in a `realtime` block inside `average`. The example gives the following logical times.

| Event | Start | End |
| --- | ---: | ---: |
| q0 first pulse | 0 µs | 4 µs |
| q0 explicit gap | 4 µs | 6 µs |
| q0 second pulse, phase 1 rad | 6 µs | 10 µs |
| q1 pulse after initial offset | 2 µs | 8 µs |
| Shared camera trigger | 10 µs | 20 µs |

The two branches join at 10 µs. This fixture accepts a 2 µs gap. Another device may require more preparation time. Optical alignment at the atoms requires separate delay calibration.

## Pair pulse amplitude and duration

Pass a list or tuple of `SweepParameter` objects to one `sweep` to use the same index for all of them. Each parameter can take its values directly from a one-dimensional NumPy array or a Python list.

```python
import numpy as np
from taiyidsl import Experiment, Qubit, Square, SweepParameter, play, reference_lab

q = Qubit("q0")
amp = SweepParameter("amp", np.array([0.2, 0.4, 0.7]), unit="")
duration = SweepParameter("duration", np.array([4, 8, 12]), unit="us")
exp = Experiment("paired_pulse", [q])
with exp.average(1), exp.sweep([amp, duration]), exp.realtime():
    play(q.rf, Square(duration, amplitude=amp))
compiled = reference_lab([q]).compile(exp)
```

The three points are `(0.2, 4 µs)`, `(0.4, 8 µs)` and `(0.7, 12 µs)`, in that order. Both named values appear in each compiled point's `coordinates`, with duration in seconds. Repeated values keep their original index. Nest a separate `exp.sweep(phase)` inside the paired sweep to scan every phase for each pair. Two separate amplitude and duration sweeps would produce all nine combinations.

## Sweep several qubits together

The same example defines `build_parallel_rabi(qubits, durations, ...)`. It uses one outer duration sweep across all qubits.

```python
duration = SweepParameter("duration", durations, unit="s")
pulse = Square(4 * us)
with exp.average(shots, mode=mode):
    with exp.sweep(duration):
        with exp.realtime():
            with exp.parallel():
                for q in exp.qubits.values():
                    with exp.serial():
                        reset_frame(q.frame)
                        play(q.raman, pulse, duration=duration)
            acquire(
                camera_signal,
                duration=10 * us,
                handle="population",
                qubits=tuple(exp.qubits),
            )
```

Each qubit supplies its own Raman amplitude and frequency when the caller leaves them unspecified. A zero-duration point still belongs to the sweep and retains its readout, with no DDS pulse output. The builder returns an `Experiment`. The caller supplies wiring and devices through `lab.compile(exp)`. The example's `reference_lab` assigns a demonstration board to each qubit. A lab configuration should assign `readout_signal` to the actual camera TTL.

This example varies a common duration. To vary the qubit durations together using separate lists, declare one parameter per qubit and pass them to one paired sweep. The built-in `analyze_rabi` calibration accepts single-drive programs.

Use one shared `acquire` for a single frame covering multiple sites. Its `duration` is the TTL pulse width. Schedule preparation, illumination, exposure and transfer waits where the lab protocol requires them. The image interface accepts one logical acquisition per selected handle per point. Give independent exposures distinct handles. If handles observe different sites, build separate `ImageResults` with matching ROI sets. Associate every site in one frame with the same `acquire`.

## Reusable operations

| Task | Entry points |
| --- | --- |
| Single-qubit tune-up | `rabi`, `ramsey`, `randomized_benchmarking` |
| Traceable RB gate sequence | `rb_sequences`, including seed, Cliffords and recovery |
| Shared optical settings | `OpticsConfig` separates settings from the operation order |
| Pumping, Raman and sideband cooling | `optical_pump`, `raman_pulse`, `sideband_cool` |
| Readout | `trigger_readout`, `illuminated_readout`, shared `acquire` |
| Parallel examples | `pulse_pair`, `build_precise_offsets`, `build_parallel_rabi` |

Compose these blocks with Python functions. Pass a `SweepParameter` for a duration or use `repeat(parameter)` to vary the number of identical operations. Use `case` when the structure changes, for instance to select an RB gate word or choose between push-out and an equal-duration idle. Repetition count alone cannot express an arbitrary RB word.

## Explicit parallelism and ASAP

Use `parallel`, `serial` and `delay` when relative times matter. Use an explicit ASAP Section for resource-aware placement. A global per-signal “Tetris” scheduler would also have to represent shared Raman/RF frames, camera joins and hardware contention across logical names. The base language keeps its declared serial and parallel boundaries.

## Run and inspect

```powershell
uv run --locked python examples/parallel_experiments.py
```

Each experiment writes `compiled.json`, `timeline.html` and an `artiq/` export under `output/parallel_experiments/`. Export includes an independent mock-driver check. The example does not invoke a real ARTIQ compiler or run a kernel. Real compilation, RTIO capacity, optical alignment and camera integration each need their own verification.
