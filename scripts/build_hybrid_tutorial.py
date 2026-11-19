"""Build the Chinese, offline-first notebook from readable lesson cells.

Run with the optional notebooks dependency group. This script only writes the
source notebook; execute_hybrid_tutorial.py validates it in a fresh kernel.
"""

# Lesson prose and Markdown tables deliberately keep paragraphs on one line.
# ruff: noqa: E501

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "docs/tutorials/hybrid_from_scratch.ipynb"
cells = []


def md(source):
    cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))


def code(source):
    cells.append(nbformat.v4.new_code_cell(textwrap.dedent(source).strip()))


md(r"""
# Build a hybrid experiment from GreenMOT Ramsey to two-qubit RB

Define qubits, wiring, named pulses, operations and four native ARTIQ components. Compose a Ramsey experiment and paired reference/CZ-interleaved Clifford circuits, inspect compiled DAC samples and analyze joint shot data. Restart the kernel and run every cell in order. Each run creates a directory under `output/tutorial_from_scratch/runs/`.

The lesson runs offline. Device identities, TTLs, pulse parameters and the placeholder CZ are teaching inputs. The synthetic decay data illustrate the statistics independently of the compiled waveforms.
""")

md(r"""
## 1. Set up the environment and distinguish four execution stages

From the repository root:

```powershell
cd C:/AIproject/taiyidsl
uv sync --locked --group notebooks
uv run --locked --group notebooks jupyter lab docs/tutorials/hybrid_from_scratch.ipynb
```

Use the project CPython 3.12 kernel. The notebook dependencies are an optional uv group.

| Action | Result |
|---|---|
| Enter a builder context or call `ops.x90(q)` | Python records an experiment node |
| `lab.compile(exp)` or `compile_stream(...)` | Resolve point parameters and phases, schedule and produce offline artifacts |
| Real ARTIQ offline compilation | Compile the generated kernel to ELF using the ARTIQ toolchain |
| Hardware execution | Validate configuration, upload, wait for readiness and dispatch the shot kernel |

`exp.realtime()` defines a DSL scope. Python loops build nodes on the host. The backend generates the program that eventually runs on the device. [Dependency groups](https://docs.astral.sh/uv/concepts/dependencies/) · [uv sync](https://docs.astral.sh/uv/concepts/projects/sync/)
""")
code(r"""
from pathlib import Path
from datetime import datetime
from uuid import uuid4
import sys
import os
import json
import math
import html
import numpy as np
from IPython.display import display, Markdown, HTML, Image

ROOT = next(
    (p for p in (Path.cwd().resolve(), *Path.cwd().resolve().parents)
     if (p / "src/taiyidsl").is_dir() and (p / "pyproject.toml").is_file()),
    None,
)
assert ROOT is not None, "Start the notebook from the repository or docs/tutorials."
assert sys.version_info[:2] == (3, 12), "Select the project's uv-managed CPython 3.12 kernel."
os.chdir(ROOT)
SESSION = ROOT / "output/tutorial_from_scratch/runs" / (
    datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
)
SESSION.mkdir(parents=True)

def table(rows):
    if not rows:
        return display(Markdown("(empty)"))
    keys = list(rows[0])
    head = "".join(f"<th>{html.escape(str(k))}</th>" for k in keys)
    body = "".join("<tr>" + "".join(
        f"<td>{html.escape(str(row.get(k, '')))}</td>" for k in keys
    ) + "</tr>" for row in rows)
    display(HTML(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"))

print("Python:", sys.executable)
print("Run output:", SESSION)
""")
code(r"""
from taiyidsl import (
    Qubit, QubitParameters, DriveParameters, TransitionParameters, Position,
    QubitStore, CalibrationProposal, PairCalibration, QubitOperations,
    LabConfig, Device, DeviceSetup, SignalMap,
    SpectrumCard, SpectrumOutput, SpectrumGroup, SpectrumConfig,
    Experiment, SweepParameter, Square, Section, section, add, layer, barrier,
    qubit_operation, play, delay, rz, shift_phase, reset_frame, reset_phase,
    parallel, acquire, us, ns, MHz, um,
    generate_rb_sequences, clifford_catalog, sequence_tableau, rb_sequence_section,
    ResultSet, joint_rb_counts, analyze_rb_counts,
)
""")

md(r"""
## 2. Capture qubit calibration

The qubits in this lesson are built with `QubitParameters` and a calibration revision. `q.rf` and `q.signal("clock")` refer to logical signals. Wiring selects their physical outputs. RF and clock waveform frequencies are distinct from `TransitionParameters.resonance_frequency`, which is unspecified here.

Calibrate `pi_duration`, `pi2_duration`, `pi_amplitude` and `pi2_amplitude` for the chosen shape. The legacy `amplitude` fallback applies when the specific amplitude is absent. Positions carry a coordinate system and SI meters. A position entry on its own schedules no motion. Put custom parameters in named `extensions`.
""")
code(r"""
def teaching_qubit(index):
    return Qubit(
        f"q{index}",
        QubitParameters(
            rf=DriveParameters(
                frequency=(10 + 2 * index) * MHz,
                amplitude=0.30,
                pi_amplitude=0.30, pi2_amplitude=0.17,
                pi_duration=800 * ns, pi2_duration=400 * ns,
                waveform="square",
            ),
            drives={"clock": DriveParameters(
                frequency=(80 + 10 * index) * MHz,
                amplitude=0.12,
                pi_duration=800 * ns, pi2_duration=400 * ns,
            )},
            transitions={
                "spin": TransitionParameters("g_down", "g_up"),
                "optical": TransitionParameters("g_up", "e_up"),
            },
            drive_transitions={"rf": "spin", "clock": "optical"},
            drive_frames={"clock": "clock_control"},
            position=Position(index * 5 * um, 0 * um, 0 * um, "tweezer_plane"),
            extensions={"teaching": {"calibrated": False, "purpose": "offline"}},
        ),
    )

q0, q1 = teaching_qubit(0), teaching_qubit(1)
qubits = (q0, q1)
table([{
    "qubit": q.uid, "RF Hz": q.drive("rf").frequency,
    "π/2 s": q.drive("rf").pi2_duration,
    "π/2 amplitude": q.drive("rf").rotation_amplitude("pi2"),
    "RF signal": q.rf, "clock signal": q.signal("clock"),
    "spin frame": q.frame, "clock frame": q.phase_frame("clock_control"),
} for q in qubits])
""")
md(r"""
### Apply a versioned calibration proposal

`QubitStore` records the new revision. Previously obtained snapshots and Sections keep their original values. After accepting a proposal, retrieve the new qubit, rebuild its gates and check pair-calibration dependency versions. The value 0.18 here illustrates an update. A second application of the same proposal is rejected as stale. The remainder of this lesson deliberately uses the original q0 snapshot.
""")
code(r"""
store = QubitStore(qubits, path=SESSION / "qubits.json")
store.save()
proposal = CalibrationProposal(
    qubit_id=q0.uid, base_revision=q0.revision, base_fingerprint=q0.fingerprint,
    updates={"rf.pi2_amplitude": 0.18}, units={"rf.pi2_amplitude": "1"},
    quality={"example_only": 1.0}, source_experiment="synthetic_tutorial_update",
)
updated = store.apply(proposal)
assert updated.revision == q0.revision + 1
assert q0.drive("rf").pi2_amplitude == 0.17
assert QubitStore.load(SESSION / "qubits.json")["q0"].fingerprint == updated.fingerprint
try:
    store.apply(proposal)
except ValueError as error:
    print("Expected revision check:", error)
else:
    raise AssertionError("A stale proposal must be rejected")
print("Tutorial q0 revision", q0.revision,
      "current store revision", updated.revision)
""")

md(r"""
## 3. Map two trigger lines and the qubit signals

`trigger_devices={"A": "awg_trigger_a", ...}` maps TTL lines to card EXT0 ports. One group launch schedules coincident RTIO edges for its cards. Each card then plays its complete sequence. Separately, `q0.rf → q0_rf → A/channel 0` maps a logical drive to the physical output.

The tutorial builds LabConfig explicitly. SIM serials and trigger/phase calibration are placeholders. This backend targets M4i.6631-x8 at 1.25 GS/s with a 125 MHz reference. Gate amplitudes are normalized; output range, termination and analog transfer determine voltage.
""")
code(r"""
group = SpectrumGroup(
    cards={"A": SpectrumCard("SIM_TUTORIAL_A"),
           "B": SpectrumCard("SIM_TUTORIAL_B")},
    outputs={
        "q0_rf": SpectrumOutput("A", 0), "q0_clock": SpectrumOutput("A", 1),
        "q1_rf": SpectrumOutput("B", 0), "q1_clock": SpectrumOutput("B", 1),
    },
    trigger_devices={"A": "awg_trigger_a", "B": "awg_trigger_b"},
    calibration={"status": "unmeasured", "revision": "tutorial-only"},
)
setup = DeviceSetup(
    {
        **{name: Device(name, "spectrum") for name in group.outputs},
        **{name: Device(name, "ttl", grid_mu=1)
           for name in ("awg_trigger_a", "awg_trigger_b", "camera_ttl")},
    },
    spectrum=SpectrumConfig({"qubit_awg": group}),
)
signals = SignalMap({
    "q0.rf": "q0_rf", "q0.clock": "q0_clock",
    "q1.rf": "q1_rf", "q1.clock": "q1_clock",
    "camera.trigger": "camera_ttl",
})
lab = LabConfig(setup, signals, backend="artiq_spectrum")
profile_path = SESSION / "lab_teaching.json"
profile_path.write_text(json.dumps(lab.to_dict(), indent=2), encoding="utf-8")
assert LabConfig.load(profile_path).to_dict() == lab.to_dict()
table([{"card": card, "EXT0 source": ttl,
        "serial": group.cards[card].serial_number}
       for card, ttl in group.card_trigger_devices.items()])
table([{"logical": logical, "device": device,
        "output": f"{group.outputs[device].card}/{group.outputs[device].channel}"
        if device in group.outputs else "ARTIQ TTL"}
       for logical, device in signals.items()])
""")
md(r"""
Add corresponding `cards`, `outputs`, `trigger_devices` and DeviceSetup/SignalMap entries for additional cards. Every card must pass preflight and report ready before the common trigger. RTIO submission capacity, actual cable skew, clocks, firmware, gains and phase must be checked on the target hardware. Keep the ARTIQ device database and laboratory wiring current.
""")

md(r"""
## 4. From pulse to operation, Section and Experiment

```text
Experiment                         scan axes, averaging, qubit snapshots and metadata
└─ average / sweep                 host execution plan
   └─ realtime                     real-time control region for one shot
      ├─ native preparation call               separately declared NativeProgram node
      ├─ delay
      ├─ triggered_sequence        Spectrum sequence start and carrier reference
      │  ├─ play / delay / rz      primitive DSL nodes
      │  ├─ Section               reusable Node subtree, supporting nesting and composition
      │  │  └─ layer              parallel Section with explicit start and end boundaries
      │  └─ ...
      ├─ acquire                  declare the measurement handle and trigger window
      └─ native readout call
```

Structural contexts such as `serial()`, `parallel()` and `case()` collect nodes. `section()` and `layer()` also produce reusable Sections. An operation body returns None. `@qubit_operation` collects it and returns a Section. In an active experiment it inserts the result exactly once; outside a context, use `add(saved)` later. Calling `add(ops.x90(q))` inside the active context would insert it twice.
""")
code(r"""
drive = q0.drive("rf")
with section(name="pulse_level_x90") as pulse_level_x90:
    play(q0.rf, Square(
        drive.pi2_duration,
        amplitude=drive.rotation_amplitude("pi2"),
        frequency=drive.frequency,
        phase=0.0,
    ))
print("Pulse-level gate node:", pulse_level_x90.to_node().children[0].kind)
""")
md(r"""
The first Section contains one explicit pulse. `ops.x90(q0)` reads the same calibration and records the operation name, arguments and snapshot dependencies. Next, compose two X90 rotations, a delay and a virtual Z into a reusable Ramsey operation.
""")
code(r"""
ops = QubitOperations(drive="rf")

@qubit_operation
def ramsey_gates(q, dark_time, virtual_angle=0.0, final_phase=0.0):
    ops.x90(q)
    delay(q.rf, dark_time)
    rz(q, virtual_angle)
    ops.x90(q, phase=final_phase)
    # The decorator returns the Section.

saved_ramsey = ramsey_gates(q0, 2 * us, virtual_angle=math.pi / 2)
assert isinstance(saved_ramsey, Section) and saved_ramsey.finalized
tree_copy = saved_ramsey.to_node()
original_count = len(tree_copy.children)
tree_copy.children.clear()
assert len(saved_ramsey.to_node().children) == original_count
print("Returned:", type(saved_ramsey).__name__,
      "contains:", type(saved_ramsey.to_node()).__name__,
      "bound qubits:", saved_ramsey.qubits)
print("Captured arguments:", saved_ramsey.parameters)
""")
code(r"""
with section(name="two_ramsey_blocks") as reusable_pair:
    add(saved_ramsey)
    delay(q0.rf, 1 * us)
    add(saved_ramsey)

simple_exp = Experiment("first_authored_experiment", qubits)
with simple_exp.average(1), simple_exp.realtime():
    with simple_exp.triggered_sequence(group="qubit_awg"):
        reset_frame(q0.frame)
        add(reusable_pair)
        ops.y90(q1)  # The active builder inserts this operation alongside ordinary instructions.
simple_compiled = lab.compile(simple_exp)
print("Sweep points:", len(simple_compiled.points),
      "logical events:", len(simple_compiled.points[0].logical_events))
""")
md(r"""
A completed Section stores immutable, unscheduled intent and its qubit dependencies. Insertion copies it. To target q1, call `ramsey_gates(q1, ...)` with the matching qubit. `QubitOperations` provides X/Y π and signed π/2, virtual Z90/Z180 and clock π/π2 rotations. Override a setting explicitly for one call or supply a calibrated custom operation for other rotations or coherent transfers.
""")

md(r"""
## 5. Serial order, aligned layers and targeted barriers

Sequentially written gates run in series, including those on independent qubits. `layer(q0, q1, align="start")` aligns the starts of branches. Use `align="end"` for a common end. A branch can contain a sequence of its own. Branches must have compatible qubit and output resources. Even one shot uses top-level `average(1)`.

An ASAP Section schedules each operation as early as its resources permit. `barrier(q0)` synchronizes q0 and shared resources, while `barrier()` covers the enclosing Section. Adjacent layers remain separate, including inside ASAP. `exact_duration` checks the duration. Inspect scheduled logical events for the actual order.
""")
code(r"""
def scheduling_example(mode):
    exp = Experiment(f"schedule_{mode}", qubits)
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        if mode == "serial":
            ops.x180(q0)
            ops.y90(q1)
        else:
            with layer(q0, q1, align=mode):
                ops.x180(q0)
                ops.y90(q1)
    return lab.compile(exp)

def pulse_rows(compiled, label="", point=0):
    tick = compiled.setup.tick_seconds
    return [{"case": label, "signal": e.signal,
             "start ns": round(e.start_mu * tick * 1e9, 3),
             "end ns": round(e.end_mu * tick * 1e9, 3)}
            for e in compiled.points[point].logical_events if e.kind in ("play", "trigplay")]

schedules = {mode: scheduling_example(mode) for mode in ("serial", "start", "end")}
table([row for mode, compiled in schedules.items() for row in pulse_rows(compiled, mode)])
""")
code(r"""
aligned_exp = Experiment("asap_with_layer_boundaries", qubits)
with aligned_exp.average(1), aligned_exp.realtime(), aligned_exp.triggered_sequence(group="qubit_awg"):
    with section(schedule="asap"):
        ops.x180(q0)
        ops.y90(q1)
        barrier(q0)
        ops.x90(q0)
        with layer(q0, q1):
            ops.x90(q0)
            ops.x180(q1)
        with layer(q0, q1, align="end"):
            ops.y90(q0)
            ops.y180(q1)
aligned = lab.compile(aligned_exp)
table(pulse_rows(aligned, "asap + barrier + 2 layers"))
""")
md(r"""
### One persistent phase offset per frame

\[
\theta(t)=\Phi_{\mathrm{osc}}(t)+s[F(t)+\phi_{\mathrm{pulse}}(u)].
\]

Direct drive usually has `s=+1`. Determine the actual sign from the control chain.

| Instruction or boundary | Effect |
|---|---|
| `shift_phase(frame, δ)` | Adds δ to F |
| `rz(q, θ)` or `ops.z90(q)` | Subtracts the virtual-Z angle from F |
| `reset_frame(frame)` | Clears F including virtual-Z contributions |
| `reset_phase(frame)` | Clears F and inherited modulation and re-establishes the carrier reference |
| Default `triggered_sequence` | References the carrier to sequence entrance and retains prior explicit F |
| Section, layer, barrier, segment or chunk | Preserves phase |

The default carrier reference excludes preceding MOT and cooling. Delays and frequency changes inside the sequence affect subsequent carrier phase. Preserve modulation and its inheritance rules across segments. RF/Raman refer to the spin frame and clock uses clock_control. Corrections after coherent transfer between levels belong in the physical gate model. A waveform phase reset leaves the quantum state untouched.
""")
code(r"""
phase_exp = Experiment("explicit_phase_frames", qubits)
with phase_exp.average(1), phase_exp.realtime(), phase_exp.triggered_sequence(group="qubit_awg"):
    reset_phase(q0.frame)
    reset_frame(q0.phase_frame("clock_control"))
    ops.x90(q0)
    rz(q0, math.pi / 2)
    ops.clock(q0, rotation="pi2", phase=0.2)
    shift_phase(q0.phase_frame("clock_control"), 0.1)
    ops.clock(q0, rotation="pi", phase=0.0)
phase_compiled = lab.compile(phase_exp)
frames = phase_compiled.frame_phases()
assert math.isclose(frames[q0.frame], -math.pi / 2)
assert math.isclose(frames[q0.phase_frame("clock_control")], 0.1)
print("Final F (rad):", frames)
""")

md(r"""
## 6. Integrate four ARTIQ components in one shot kernel

Write MOT, cooling, preparation and readout classes as a Python package. The next cell writes their source without importing ARTIQ. They inherit `HasEnvironment`, bind devices in `build` and implement `@kernel` methods using teaching TTLs. Packaging permits static source audit and hashing. Package initialization, host build and an explicitly selected host_prepare can run when ARTIQ constructs the program; keep those operations free of hardware side effects.
""")

# The companion is checked in, so notebook regeneration does not depend on output/.
native_text = (ROOT / "docs/tutorials/native_components_walkthrough.md").read_text(
    encoding="utf-8"
)
native_cells = re.findall(r"```python\n(.*?)\n```", native_text, flags=re.S)
assert len(native_cells) == 2, (
    "Native walkthrough must contain exactly two Python cells"
)
code(native_cells[0])
md(r"""
### Declare NativeModule, NativeStage and NativeProgram

`NativeModule` pins the class, source, resources, static build arguments and hooks. `NativeStage` defines the method, public name, placement and duration. `NativeProgram` combines named components. Invoking `native.GM_cooling(...)` records a future call. Several classes can share a source file and retain independent instances. Each shared device needs declared participants and one initialization owner. Here MOT initializes the preparation TTL; cooling and state preparation use it in sequence.
""")
code(native_cells[1])
md(r"""
The root owns reset and initialization. Each session runs initialize once. Each shot runs prefix, timed stages, DSL control and suffix, followed by after_shot. Session close calls finalize once. Chunk changes retain the same root and component instances. Request a host setup method with `host_prepare="method"`.

Native calls are direct children of realtime in `prefix* → timed* → DSL → suffix*` order. Prefix duration may vary. Strict timed/suffix methods must return at the declared cursor, with errors for short or long execution. A declared `duration=20*us` requires matching arguments. Keep reset, break_realtime, blocking waits and RPC outside strict intervals. Allow space for early triggers after the opaque stage.

`source_root` is the example's path variable. Registration and export audit source statically. The ARTIQ worker must later resolve that package at its pinned path. Rebuild declarations and export after a source/path change.
""")

md(r"""
## 7. Build Ramsey and compare the GreenMOT integration

Combine native preparation, control and readout for two dark times. The explicit acquisition declares a `bits` handle and a 64 ns trigger window. Camera exposure, transport and classification belong to the result adapter. Native readout here uses a separate TTL to illustrate illumination. The experiment body exposes timing directly.
""")
code(r"""
dark = SweepParameter("dark_time", [1, 2], "us")
ramsey_exp = Experiment("tutorial_ramsey", qubits, metadata={
    "offline_only": True, "hardware_verified": False,
    "physical_preparation_verified": False,
})
with ramsey_exp.average(2, mode="cyclic", scope="chunk"):
    with ramsey_exp.sweep(dark, chunks="auto"), ramsey_exp.realtime():
        native.prepare_MOT()
        native.GM_cooling(20 * us)
        native.state_prep(8 * us)
        delay(10 * us)
        with ramsey_exp.triggered_sequence(group="qubit_awg"):
            reset_frame(q0.frame)
            ramsey_gates(q0, dark, virtual_angle=math.pi / 2)
        acquire("camera.trigger", duration=64 * ns, handle="bits", qubits=qubits,
                metadata={"classification_required": True})
        native.readout(10 * us)

ramsey_compiled = lab.compile(ramsey_exp)
assert ramsey_compiled.metadata["native_artiq"]["source_audit"]["status"] == "passed"
assert "tutorial_native.stages" not in sys.modules
assert "artiq" not in sys.modules
ramsey_export = ramsey_compiled.export(SESSION / "ramsey_export")
table(ramsey_compiled.acquisition_plan(0))
print("Native source audit passed; export:", ramsey_export)
""")
md(r"""
### Compare the GreenMOT package lifecycle

`examples/artiq_spectrum/greenmot_ramsey.py` uses `GreenMOTAdapter` with one `NativeKernelPackage`. Its default TTL fixture demonstrates a variable prefix and a fixed tail. The tail is 15 ms + 16 ns and the post-preparation gap is explicit. The real adapter checks the ordering of the upstream preparation operations. Legacy cleanup is per shot. This example has its own readout trigger and does not implicitly create an acquisition handle.
""")
code(r"""
from examples.artiq_spectrum import greenmot_ramsey

green_exp = greenmot_ramsey.build_experiment(delays_ms=(0.01,), shots=2)
green_lab = LabConfig.load(ROOT / "examples/artiq_spectrum/lab_simulated.json")
green_compiled = green_lab.compile(green_exp)
assert green_exp.metadata["native_source"] == "native_fixture"
assert green_compiled.acquisition_plan() == []
print("GreenMOT example source:", green_exp.metadata["native_source"])
print("Card trigger mapping:", green_lab.setup.spectrum.groups["qubit_awg"].card_trigger_devices)
print("Acquisition handles after native readout:", green_compiled.acquisition_plan())
""")
md(r"""
To integrate the laboratory GreenMOT implementation, supply `build_experiment(greenmot_source=Path(...))`, the actual device database, configuration and measured timing. Leave the existing program's reset and resource ownership explicit. Check source pins whenever the original source changes.
""")

md(r"""
## 8. Freeze reference and CZ-interleaved two-qubit Clifford circuits

Length counts the sampled random Cliffords. It excludes recovery and is distinct from the physical pulse count. Paired reference and interleaved circuits reuse the same random Cliffords. The latter inserts CZ after each random Clifford and computes its own exact recovery. Check the final tableau is identity and the paired random subsequences match. A fixed seed defines the circuit independently of shots, averaging and chunks.
""")
code(r"""
LENGTHS = (0, 1, 4, 12)
SEQUENCE_COUNT = 3
SEED = 101
VARIANTS = {0: "reference", 1: "interleaved"}
rb_sequences = generate_rb_sequences(
    LENGTHS, sequence_count=SEQUENCE_COUNT, seed=SEED,
    num_qubits=2, interleaved=True,
)
identity = sequence_tableau((), num_qubits=2)
assert len(clifford_catalog(1)) == 24
assert len(clifford_catalog(2)) == 11520
assert all(sequence_tableau(seq.gates, num_qubits=2) == identity for seq in rb_sequences)
descriptors = {(s.length, s.sequence, s.variant): s for s in rb_sequences}
for length in LENGTHS:
    for number in range(SEQUENCE_COUNT):
        assert (descriptors[length, number, "reference"].clifford_indices
                == descriptors[length, number, "interleaved"].clifford_indices)
table([{"length": s.length, "sequence": s.sequence, "variant": s.variant,
        "primitive gates incl. recovery": len(s.gates), "recovery": s.recovery_index}
       for s in rb_sequences[:8]])
""")
md(r"""
### Replace the CZ body with a calibrated interaction

The two clock pulses below are a compilation fixture. An entangling implementation needs a real interaction and measured local compensation. `PairCalibration` records endpoints, implementation parameters, version dependencies and phase corrections. `QubitOperations.cz()` checks that the selected pair matches the qubit snapshots. The supplied implementation must explicitly enact the compensation.
""")
code(r"""
pair = PairCalibration(
    (q0.uid, q1.uid), "tutorial_placeholder_cz",
    status="demonstration",
    parameters={"physical_cz_verified": False, "clock_rotation": "pi"},
    qubit_revisions={q.uid: q.revision for q in qubits},
    qubit_fingerprints={q.uid: q.fingerprint for q in qubits},
    phase_corrections={q.uid: 0.0 for q in qubits},
    source="offline teaching only; replace with a calibrated interaction",
)

@qubit_operation
def placeholder_cz(control, target):
    pair.validate(control, target)
    with layer(control, target):
        ops.clock(control, rotation="pi", phase=0.0)
        ops.clock(target, rotation="pi", phase=0.0)
    # A physical implementation must supply measured phase corrections.
    for q in (control, target):
        shift_phase(q.frame, pair.phase_corrections[q.uid])

rb_ops = QubitOperations(drive="rf", cz_impl=placeholder_cz, pair_calibrations=(pair,))
pair.save(SESSION / "pair_calibration_demo.json")
try:
    pair.validate(updated, q1)
except ValueError as error:
    print("Expected stale pair after updating q0:", error)
else:
    raise AssertionError("A stale pair must reject a new qubit revision")
""")
md(r"""
### Encode length, sequence and variant as sweep axes

At each compiled point, `case` selects only the matching circuit. `rb_sequence_section` maps its word to operations and layers. Specify `"dimensionless"` for length and sequence axes. The function accepts frozen descriptors so chunking preserves the sampled circuits.
""")
code(r"""
def make_rb_experiment(*, chunks="auto", shots=256):
    exp = Experiment("tutorial_2q_rb_irb", qubits, metadata={
        "experiment_type": "rb", "num_qubits": 2,
        "seed": SEED, "variant_labels": VARIANTS,
        "rb_sequences": [s.to_dict() for s in rb_sequences],
        "offline_only": True, "hardware_verified": False,
        "physical_cz_verified": False, "native_fixture": True,
        "calibrated_rotations": False,
    })
    length_axis = SweepParameter("length", LENGTHS, "dimensionless")
    sequence_axis = SweepParameter("sequence", range(SEQUENCE_COUNT), "dimensionless")
    variant_axis = SweepParameter("variant", tuple(VARIANTS), "dimensionless")
    with exp.average(shots, mode="cyclic", scope="chunk"):
        with (exp.sweep(length_axis, chunks=chunks), exp.sweep(sequence_axis),
              exp.sweep(variant_axis), exp.realtime()):
            native.prepare_MOT()
            native.GM_cooling(20 * us)
            native.state_prep(8 * us)
            delay(10 * us)
            with exp.triggered_sequence(group="qubit_awg"):
                for q in qubits:
                    reset_frame(q.frame)
                    reset_frame(q.phase_frame("clock_control"))
                for length in LENGTHS:
                    with exp.case(length_axis, length):
                        for number in range(SEQUENCE_COUNT):
                            with exp.case(sequence_axis, number):
                                for variant, label in VARIANTS.items():
                                    with exp.case(variant_axis, variant):
                                        rb_sequence_section(
                                            qubits, descriptors[length, number, label], rb_ops
                                        )
            acquire("camera.trigger", duration=64 * ns, handle="bits", qubits=qubits,
                    metadata={"joint_readout": True, "classification_required": True})
            native.readout(10 * us)
    return exp

rb_exp = make_rb_experiment()
rb_compiled = lab.compile(rb_exp)
assert len(rb_compiled.points) == len(LENGTHS) * SEQUENCE_COUNT * len(VARIANTS)
print("Global points:", len(rb_compiled.points), "shots/point:", rb_compiled.shots)
print("First and last coordinates:", rb_compiled.points[0].coordinates, rb_compiled.points[-1].coordinates)
""")
md(r"""
The reusable version is `examples/artiq_spectrum/randomized_benchmarking.py`. Its `build_experiment` accepts qubits, operations, native_program and chunk settings. Set `num_qubits=1, interleaved=False` for the complete 24-element single-qubit Clifford group without CZ. Resetting clock and spin control frames independently leaves physical transfer compensation to the calibrated gate.
""")

md(r"""
## 9. Choose a chunk axis and inspect automatic boundaries

Here `chunks="auto"` applies to length. Each value includes all inner sequence × variant coordinates. Split contiguous axis values subject to each card's deduplicated segments, slot count, largest segment and step limit.

| Setting | Meaning |
|---|---|
| `chunks=2` | Make two chunks |
| `chunk_size=2` | At most two selected-axis values per chunk |
| `chunks="auto"` | Select boundaries from card capacity and managed workspace |

If all inner values for one length exceed capacity, choose sequence as the chunk axis. A single oversized shot must be redesigned. The compiler uses one chosen axis and leaves shots intact. `average(..., scope="chunk", mode="cyclic")` finishes averaging a chunk before switching banks. This reduces uploads but alters temporal sampling and susceptibility to drift. Coordinates remain global.
""")
code(r"""
table([{"chunk": chunk["index"], "global points": chunk["points"]}
       for chunk in rb_compiled.host_execution_plan["chunks"]])

manual_compiled = lab.compile(make_rb_experiment(chunks=2, shots=2))
table([{"manual chunk": chunk["index"], "global points": chunk["points"]}
       for chunk in manual_compiled.host_execution_plan["chunks"]])
assert len(manual_compiled.host_execution_plan["chunks"]) == 2
del manual_compiled
""")
md(r"""
### Compile a large experiment to a completed store

`lab.compile()` returns an in-memory artifact. `compile_stream()` writes points as they are produced, exports chunks, deduplicates final samples and publishes a complete index at the end. Use a new directory. Execution starts after completion. Interrupted compilation currently requires restarting.

The managed budget measures compiler-owned work buffers. The AST, RB words, Python objects, index and external compiler can increase total RSS. Consume loaded chunks incrementally. This small comparison keeps both results and sets an 8 MiB workspace budget to demonstrate a boundary independent of card memory capacity.
""")
code(r"""
artifact = lab.compile_stream(
    rb_exp, directory=SESSION / "rb_artifact",
    host_memory_budget_bytes=8 * 1024**2,
)
artifact.verify()
table([{"chunk": c["index"], "global points": c["points"], "directory": c["directory"]}
       for c in artifact.manifest["chunks"]])
print("artifact SHA-256:", artifact.sha256)
print("Peak managed workspace bytes:", artifact.manifest["managed_peak_bytes"])
print("Process RSS includes additional allocations.")
""")
md(r"""
### Inspect the final DAC samples

The logical/physical IR explains constraints and waits. The output view decodes the packed int16 data including quantization, segment layout and inter-card padding. Compare measured captures separately for cable and analog effects. The next cell compares a window of the final global point in the in-memory and disk versions, using shot T0 and the correct chunk-local point number.
""")
code(r"""
global_point = len(rb_compiled.points) - 1
chunk_info = artifact.chunk_for_point(global_point)
local_point = chunk_info["points"].index(global_point)
one_chunk = artifact.load_chunk(chunk_info["index"])
assert one_chunk.points[local_point].coordinates == rb_compiled.points[global_point].coordinates
first_pulse = min(e.start_mu for e in rb_compiled.points[global_point].logical_events
                  if e.kind in ("play", "trigplay"))
start_s = first_pulse * rb_compiled.setup.tick_seconds
window = rb_compiled.simulate_output(point=global_point, start_s=start_s, duration_s=2e-6)
stream_window = one_chunk.simulate_output(point=local_point, start_s=start_s, duration_s=2e-6)
print("Output window fields:", list(window))
""")

# Compare final DAC fields; native lifecycle metadata uses chunk-local indices.
code(r"""
def compare_arrays(a, b):
    if isinstance(a, dict):
        assert set(a) == set(b)
        for key in a:
            compare_arrays(a[key], b[key])
    elif isinstance(a, (list, tuple)):
        assert len(a) == len(b)
        for left, right in zip(a, b):
            compare_arrays(left, right)
    elif isinstance(a, np.ndarray):
        np.testing.assert_array_equal(a, b)
    else:
        assert a == b, (a, b)

# Match global and chunk-local coordinates before comparing timing, samples and phase.
for field in ("coordinates", "start_s", "duration_s", "time_reference", "traces"):
    compare_arrays(window[field], stream_window[field])
assert rb_compiled.frame_phases(global_point) == one_chunk.frame_phases(local_point)
print("Eager and streamed final outputs match in this window.")
del one_chunk, stream_window
""")
code(r"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Each channel is decoded from the final DAC data.
fig, ax = plt.subplots(figsize=(10, 3))
for channel in window["traces"]:
    if channel["channel"] == 0:
        ax.plot(np.asarray(channel["time_s"]) * 1e6,
                channel["voltage_v"], label=f"card {channel['card']} / RF", lw=0.8)
ax.set(xlabel="Time from shot T0 (us)", ylabel="Predicted DAC voltage (V)",
       title="Final packed DAC prediction — no hardware measurement")
ax.legend()
fig.tight_layout()
waveform_png = SESSION / "final_dac_window.png"
fig.savefig(waveform_png, dpi=140)
plt.close(fig)
display(Image(filename=str(waveform_png)))
""")
code(r"""
report = artifact.export_report(SESSION / "rb_report")
print("Report directory:", report)
print("Open index.html in the report directory to load chunks and windows on demand.")
""")
md(r"""
The directory report loads metadata per chunk and packed samples per window. Open it in a browser to verify zoom, switching and resource release. For capture comparison, declare card/channel, time reference, artifact, calibration version and fixed deskew in the input. A fitted time shift supports exploration only. [Compiled viewers](../compiled_viewers.md)
""")

md(r"""
## 10. Preserve joint shots before computing P00

`acquire` declares the observation. The laboratory adapter acquires the frame, classifies q0 and q1 from the same shot, keeps the frame reference and records loss/invalid states in ResultSet or RunStore.

The example samples a joint four-state distribution with `P00 = 0.70 p^m + 0.25`, p=0.96 for reference and p=0.96×0.98 for interleaved. It extracts both bits from each sampled pair. This synthetic decay is independent of the placeholder CZ waveforms. Multiplying the marginal means would discard correlations.
""")
code(r"""
results = ResultSet.from_compiled(rb_compiled, metadata={
    "data_source": "synthetic_depolarizing_model_not_hardware",
    "alpha_reference": 0.96, "cz_ratio": 0.98,
})
rng = np.random.default_rng(20261112)
for point in rb_compiled.points:
    m = int(point.coordinates["length"])
    alpha = 0.96 * (0.98 if point.coordinates["variant"] == 1 else 1.0)
    p00 = 0.70 * alpha**m + 0.25
    states = rng.choice(4, size=rb_compiled.shots,
                        p=[p00, (1-p00)/3, (1-p00)/3, (1-p00)/3])
    for shot, state in enumerate(states):
        for bit, q in enumerate(qubits):
            results.record("bits", point.index, shot, q.uid,
                           int((state >> (1-bit)) & 1),
                           labels={"source": "synthetic"})
results.save(SESSION / "synthetic_results")
counts = joint_rb_counts(results, "bits", qubits=("q0", "q1"), variant_labels=VARIANTS)
table([c.to_dict() for c in counts[:4]])
""")
md(r"""
Fit `A p^m + B`. SPAM mainly enters A/B. In dimension d=2 or 4, the standard EPC is `(d−1)(1−p)/d`. The interleaved ratio estimate depends on its model assumptions. Keep negative raw estimates, paired sequence bootstrap intervals and the separate theoretical bounds. Three random sequences per length and 256 shots demonstrate the workflow. [Two-qubit RB](https://arxiv.org/abs/1210.7011) · [IRB method and bounds](https://arxiv.org/abs/1203.4550)
""")
code(r"""
analysis = analyze_rb_counts(counts, bootstrap_samples=40, seed=1234)
analysis["data_source"] = "synthetic_depolarizing_model_not_hardware"
(SESSION / "synthetic_rb_analysis.json").write_text(
    json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
)
table([{"variant": key, **value} for key, value in analysis["fits"].items()])
print("CZ IRB (conditional on the model):", analysis["interleaved_cz"])
print("Paired sequence bootstrap:", analysis["bootstrap"])
print("Synthetic reference EPC =", 0.75 * (1-0.96),
      "; CZ ratio estimate =", 0.75 * (1-0.98))
""")
code(r"""
fig, ax = plt.subplots(figsize=(8, 4))
xx = np.linspace(0, max(LENGTHS), 200)
for variant, marker in (("reference", "o"), ("interleaved", "s")):
    selected = [c for c in counts if c.variant == variant]
    ax.scatter([c.length for c in selected], [c.probability for c in selected],
               marker=marker, alpha=0.65, label=f"{variant}: synthetic sequences")
    fit = analysis["fits"][variant]
    ax.plot(xx, fit["A"] * fit["alpha"]**xx + fit["B"],
            label=f"{variant}: fit")
ax.set(xlabel="Random Clifford count (excluding recovery)", ylabel="Joint P00",
       title="Synthetic RB / CZ IRB — not waveform simulation or hardware data")
ax.legend(fontsize=8)
fig.tight_layout()
rb_png = SESSION / "synthetic_rb.png"
fig.savefig(rb_png, dpi=140)
plt.close(fig)
display(Image(filename=str(rb_png)))
""")
md(r"""
### Choose a loss policy

ResultSet retains `atom_lost`, `loading_failed` and invalid states. `joint_rb_counts` requires an explicit policy when loss is present. `count_as_failure` includes loss as failure. `exclude` conditions the sample and changes the interpretation of EPC. Edit only the reloaded teaching copy and retain the original synthetic shots.
""")
code(r"""
loss_demo = ResultSet.load(SESSION / "synthetic_results")
loss_demo.record("bits", 0, 0, "q0", status="atom_lost")
try:
    joint_rb_counts(loss_demo, "bits", qubits=("q0", "q1"), variant_labels=VARIANTS)
except ValueError as error:
    print("Expected explicit loss policy:", error)
else:
    raise AssertionError("Loss must require an explicit policy")
loss_counts = joint_rb_counts(
    loss_demo, "bits", qubits=("q0", "q1"), variant_labels=VARIANTS,
    loss_policy="count_as_failure",
)
print("Raw shots, losses, denominator:",
      loss_counts[0].shots, loss_counts[0].lost, loss_counts[0].denominator)
""")

md(r"""
### Recover interrupted data commits using a host mock

Use the four-shot Ramsey artifact to test bank selection, dispatch, SQLite and recovery without executing kernels. A real persistent run records execution, point, shot, attempt, bank and raw asset references. Resume skips committed observations. After hardware completion, recover only the original attempt's data if the detector supports it. Unknown state stops by default. Explicit retry or skip preserves attempt history; physical exactly-once execution cannot be guaranteed.
""")
code(r"""
from examples.artiq_spectrum.rb_offline_run import run_offline_journal

ramsey_artifact = lab.compile_stream(
    ramsey_exp, directory=SESSION / "ramsey_artifact",
    host_memory_budget_bytes=8 * 1024**2,
)
mock_summary = run_offline_journal(
    ramsey_artifact, SESSION / "mock_run",
    lambda coordinates, shot: {
        "source": "synthetic_mock_not_detector", "coordinates": coordinates,
        "shot": shot, "joint_bits": [0, 0],
    },
)
assert mock_summary["dispatches_on_completed_resume"] == 0
assert mock_summary["data_commits"] == 4
assert not mock_summary["hardware_executed"]
print(json.dumps(mock_summary, indent=2))
""")

md(r"""
## 11. Supply the laboratory inputs

| Input | What to provide |
|---|---|
| QubitStore and gates | Calibrated RF/Raman/clock waveform parameters, transition, position, revision and measured coherent-transfer/CZ compensation |
| LabConfig and device database | Actual serials, output settings, trigger wiring, shared resources, reference clock, RTIO grid and capacity |
| NativeProgram | Audited MOT/cooling/preparation/readout sources, source roots, exclusive/shared resources and lifecycle |
| Preparation gap | Measured analog settling after the digital boundary and sufficient trigger lead |
| Detector and ResultSet | Exposure, frame retrieval, joint site classification, loss/invalid flags and raw references |
| RunStore | Durable observation commits and recovery by original attempt |
| RB study design | Lengths, paired sequences, shots, sampling order, loss model and uncertainty estimates |

```text
my_lab/
  lab.json                  wiring and hardware contracts
  qubits.json               single-qubit snapshots
  pairs/                    two-qubit calibrations
  native/                   importable native ARTIQ package
  native_program.py         Module / Stage / Program declarations
  operations.py             gate implementations and explicit phase compensation
  experiments/              Ramsey and RB experiment builders
  detector.py               classification and persistent data adapters
output/<run>/               separate compiled artifacts, execution records, raw images and reports
```

Start from current calibration, build dependent gates, select scans and chunks, compile and inspect the DAC. Use real ARTIQ compilation for the affected native chain and measure physical timing before an instrument run. Save shots before fitting, then publish a new calibration proposal and rebuild dependent artifacts.
""")
md(r"""
### Real ARTIQ offline compilation

The notebook generates source in ordinary Python. Use `scripts/artiq-python.ps1` and `scripts/compile_native_artiq.py` with the independent ARTIQ interpreter, disconnected device database, `SESSION/native_sources` import root and explicit tutorial_prepare/tutorial_camera TTL entries to invoke `Core.compile`. See the [native walkthrough](native_components_walkthrough.md) and [component guide](../native_components.md). A deployed worker must resolve the pinned laboratory sources and actual devices.
""")

md(r"""
## 12. Laboratory acceptance and current boundaries

Measure card-to-card skew and analog response, validate clock/calibration identity, confirm readiness/re-arm under load, connect actual preparation and detector adapters, verify the CZ interaction, and test retrieval of the original interrupted attempt. Compare artifacts with captured waveforms and check browser interaction independently of notebook execution.

GreenMOT's legacy package supports ordinary compilation. Streaming native execution uses NativeProgram. Native placement supports preparation followed by DSL and readout. The input AST, RB words and global indices can grow with scale. DDS transport is available as a separate target, while cross-DDS/AWG phase coherence, arbitrary mid-gate native feedback, multi-axis chunking, splitting a shot and resuming interrupted compilation require additional capabilities. Begin deployment with a measured Ramsey and complete readout before interpreting a two-qubit benchmark.
""")

md(r"""
## 13. Exercises

1. Change `virtual_angle` from π/2 to zero. Rebuild, compile and compare the final second-pulse DAC. Repeat with `reset_frame` and explain how `reset_phase` differs.
2. Compare serial X180(q0)/Y90(q1) with layers aligned to start and end. Put both branches on q0 and inspect the resource conflict.
3. Freeze the seed/descriptors and compare manual and automatic chunks. Move the axis to sequence and check point coordinates and samples. Explain the different averaging order and drift sensitivity.

[Qubits and gates](../qubit_parameters_and_operations.md) · [Authoring and triggers](../authoring_and_triggers.md) · [Native components](../native_components.md) · [Streaming and recovery](../streamed_execution.md) · [Waveforms and capture](../compiled_viewers.md)
""")
code(r"""
lesson_summary = {
    "kind": "offline_tutorial_run",
    "python": sys.version.split()[0],
    "session": str(SESSION),
    "rb_points": len(rb_compiled.points),
    "rb_artifact_sha256": artifact.sha256,
    "ramsey_source_audit": ramsey_compiled.metadata["native_artiq"]["source_audit"]["status"],
    "checks": ["qubit revision", "section copy isolation", "independent frames",
               "Clifford sizes and recovery", "paired circuits", "pair revision",
               "artifact integrity", "eager/stream DAC window", "loss policy",
               "mock committed resume"],
    "hardware_executed": False,
    "generated_artiq_kernels_executed": False,
    "synthetic_results": True,
    "browser_render_verified": False,
}
(SESSION / "lesson_summary.json").write_text(
    json.dumps(lesson_summary, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("Saved tutorial artifacts:", SESSION)
print("Laboratory execution requires the configuration in section 11.")
""")


def main():
    notebook = nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3.12 (taiyidsl)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
            "taiyidsl": {
                "offline_only": True,
                "source_generator": "scripts/build_hybrid_tutorial.py",
            },
        },
    )
    # Stable IDs keep regeneration diffs readable.
    for index, cell in enumerate(notebook.cells):
        cell.id = f"hybrid-lesson-{index:03d}"
    nbformat.validate(notebook)
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, DESTINATION)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "I,F401",
            "--fix",
            str(DESTINATION),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", str(DESTINATION)], check=True
    )
    print(f"Wrote {DESTINATION}: {len(cells)} cells")


if __name__ == "__main__":
    main()
