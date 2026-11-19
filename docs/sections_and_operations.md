# Sections and reusable operations

A Section captures a subtree of DSL instructions for reuse. It can represent a composite pulse sequence, an operation on one or more qubits, or a layer with explicit scheduling rules.

## Experiment tree and builder

```text
Experiment (qubits, metadata, root)
└─ root: Node(kind="experiment")
   └─ Node(kind="average")
      └─ Node(kind="sweep") … nested host sweep axes
         └─ Node(kind="realtime")
            ├─ optional native prefix / timed calls
            ├─ Node(kind="triggered_sequence") (hybrid AWG scope)
            │  └─ Node(kind="serial", schedule=…, operation_name=…)
            │     └─ play / delay / frame / parallel / repeat / case …
            └─ readout node or native suffix
```

During construction, context managers maintain a stack of nodes. Each `Node` has a `kind`, `params` and `children`. For example, `stage()` creates a named serial node. The diagram shows a hybrid experiment. Pure ARTIQ instructions can appear directly inside realtime. A `Section` holds an unscheduled subtree and inserts a copy of its serial root. A `@qubit_operation` produces a Section. A `@experiment` factory returns a whole Experiment.

In `dsl/_context.py`, the private `_builder` helper retrieves the active Experiment or Section collector. The helper of the same name in `staged_workflow.py` validates a user-supplied Python builder. An ordinary helper typically appends instructions to the current tree and returns `None`. Use a Section to retain the instructions for later insertion.

## Call an operation or save it for reuse

```python
from taiyidsl import (
    Qubit,
    Square,
    Experiment,
    play,
    section,
    add,
    qubit_operation,
    us,
    parallel,
    barrier,
)

q = Qubit("q")


@qubit_operation
def x90(q, pulse):
    play(q.rf, pulse)


pulse = Square(4 * us, amplitude=0.2, frequency=30_000)
saved = x90(q, pulse)  # Outside a builder, this creates a standalone Section.

exp = Experiment("reuse", [q])
with exp.average(10), exp.realtime():
    with exp.triggered_sequence(group="qubit_awg"):
        x90(q, pulse)  # Insert once and return the Section.
        add(saved)  # Insert a copy of the saved Section, equivalent to exp.add(saved).
```

The example values illustrate the interface. Physical X90 calibration is a laboratory measurement.

Inside an active builder, calling an operation inserts one copy and returns the captured Section. Outside a builder it only returns the Section. Similarly, `with section("name") as segment` inserts once on successful exit in an active experiment, or captures a standalone segment outside it. Completed sections are read-only. `to_node()`, `to_dict()` and `parameters` return independent data, and each `add` inserts a fresh subtree.

Avoid `add(x90(q, pulse))` inside an experiment when intending one gate. The call already inserts once and `add` would insert a second copy. Even `saved = x90(...)` inserts once inside the active context. Use `add(saved)` to request another occurrence.

The Python operation builds its subtree on the host once per call. Use `SweepParameter`, `repeat` and `case` for point-specific values and structure. A Section cannot contain `average`, `sweep`, `realtime`, `triggered_sequence`, a native call boundary or a nested whole experiment.

## Bind qubit targets in the signature

```python
class Operations:
    @qubit_operation
    def x90(self, q, pulse):
        play(q.rf, pulse)

    @qubit_operation
    def pair(self, q0, q1, pulse):
        with parallel():
            play(q0.rf, pulse)
            play(q1.rf, pulse)


ops = Operations()
```

The decorator preserves function or method names, signatures and documentation. It binds direct Qubit parameters by signature, so positional and keyword calls to `pair` identify the same targets. Supply explicit parameters for targets. The decorator does not discover them in lists, dictionaries, closures, object attributes or variadic arguments. The synchronous function emits DSL instructions and returns `None`. The wrapper returns the Section. Import `parallel` from TaiyiDSL. Concurrent drives on two qubits alone do not establish a physical two-qubit gate.

A Section captures the bound qubits and calibration snapshots. Insertion requires matching targets and snapshots in the receiving experiment. Rebuild the operation to change either. Sweep parameters must match the axes' names and full definitions. Separate Python objects with identical definitions are valid.

## Serial, parallel and ASAP placement

```python
with section("aligned", schedule="parallel", align="end", qubits=(q0, q1)):
    with section("q0_composite"):
        ops.x90(q0, pulse)
        ops.x90(q0, pulse)
    ops.x90(q1, pulse)
```

A serial Section runs children in order. An enclosing parallel block sees the entire composite operation as one branch. Inside a Section, `exp.serial/parallel/repeat/case/stage` delegate to the active collector. Use the context functions without `exp` when building standalone sections.

`schedule="parallel"` requires explicit participating qubits. Direct child sections form complete branches. The default `align="start"` aligns starts. `align="end"` aligns ends by moving shorter branches later. Independent branches cannot share a qubit or physical output, or operate on an undeclared qubit. A declared qubit remains part of the layer even if it receives no pulse. `exact_duration` asserts duration without adding padding. `segment.schedule` returns `"parallel"` and `segment.align` gives the alignment direction. `layer(*qubits, align=...)` is a wrapper around the same implementation. Serial and ASAP sections reject `align`.

```python
with section("gates", schedule="asap"):
    ops.x90(q0, pulse)
    ops.x90(q1, pulse)
    barrier()
    ops.x90(q0, pulse)
```

An ASAP Section places each direct child as a unit at the earliest time permitted by its qubit, output and frame dependencies. A qubit operation occupies all declared targets and actual outputs. `barrier()` joins all preceding work in the Section. `barrier(q0, q1)` joins those targets, including shared physical outputs addressed under other signal names. A barrier emits no TTL, contains no children and leaves frame state alone. `delay(t)` waits across the Section. `delay(signal, t)` advances only the dependency chain for the signal and its associated resources. Section exit joins every internal operation. The hardware backend still checks preparation time and rejects unschedulable requests.

## Circuit layers and local synchronization

```python
from taiyidsl import QubitOperations, barrier, section

ops = QubitOperations(drive="rf")
with section("circuit", schedule="asap"):
    ops.x90(q0)
    ops.x90(q1)
    barrier(q0)  # Wait for q0 and its shared physical outputs.
    ops.y90(q0)  # Can start before the previous q1 operation ends.
    with section("aligned", schedule="parallel", align="end", qubits=(q0, q1)):
        ops.x90(q0)
        ops.x180(q1)
    barrier()  # Join the entire current Section.
```

In this example, `ops.y90(q0)` can begin while work on q1 continues. The parallel Section then waits for **all** preceding work in its parent, aligns its two branches at the end, and holds subsequent work until the whole layer finishes. This full boundary applies even in an ASAP parent. A targeted barrier alone only joins resources, without aligning earlier gates. Serial Ramsey sequences or circuits already separated into parallel layers rarely need an extra barrier.

Sections store unscheduled instructions. Insertion schedules them at their final times and resolves their physical phase there. Entering, saving, reusing or leaving a Section does not create a carrier epoch, reset a frame, or restore an earlier phase. `rz` and `shift_phase` persist for subsequent instructions.

## Reusable gate bodies

These functions return a Section and insert it when a builder is active. They emit ordinary `play` instructions. The signal map and enclosing `triggered_sequence` select the target. The caller owns preparation, scans, triggering, calibration and readout.

| Interface | Contents |
| --- | --- |
| `rabi_body(q, pulse, *, drive="rf")` | Play one explicit pulse. Duration, amplitude and other values may be sweep parameters. |
| `ramsey_body(q, pi2_pulse, dark_time, *, drive="rf", final_phase=None, virtual_z=None)` | Two π/2 pulses separated by the given dark time. Optional absolute phase override for the last pulse and a persistent logical Rz. |
| `echo_body(q, pi2_pulse, pi_pulse, half_wait, *, drive="rf")` | π/2, dark interval, π, dark interval, π/2. Pulse phases specify rotation axes. |
| `rb_body(q, sequence, pi2_pulse, pi_pulse, *, drive="rf", inter_gate_delay=0)` | Play one fixed RB word, retaining its Clifford and recovery description. |

`drive` accepts RF, Raman or another drive declared by the qubit. Ramsey `dark_time` and echo `half_wait` exclude pulse widths. Generate the RB sequence once on the host with `taiyidsl.experiments.templates.rb_sequences` and an explicit seed. The operation stores the sequence and uses the gate word's absolute X/Y base phases in place of `pulse.phase`. Other pulse parameters are retained. Record the seed in experiment metadata. The backend must still accept the requested inter-gate delay, including the zero default. See [Qubit parameters and operations](qubit_parameters_and_operations.md).

## Native ARTIQ belongs in the laboratory adapter

Parallel alignment supports start and end. Center alignment, rebinding a captured Section to arbitrary qubits, and compiling an independent circuit representation would need additional interfaces.

`examples/artiq_spectrum/greenmot_native/adapter.py` defines `GreenMOTAdapter`, including resources, source pinning and timing declarations. The original integration builds one `NativeKernelPackage`. New multi-component integrations use `NativeModule`, `NativeStage` and `NativeProgram`. `prepare_MOT()` records a prefix and the timed preparation tail. `execute_readout()` records a suffix. Building the experiment imports neither ARTIQ nor external GreenMOT source.

Native proxies stay direct children of realtime, ordered prefix → timed tail → control → suffix. They cannot be moved into a Section or wrapped in `qubit_operation`. The adapter preserves the existing control sequence. The lab supplies MOT physics, wiring, calibration and readout. Inspect construction, compilation, driver traces and physical measurements separately when assessing synchronization, settling or gate fidelity. See [Native ARTIQ integration](native_artiq_integration.md).
