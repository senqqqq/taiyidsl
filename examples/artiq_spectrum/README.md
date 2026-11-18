# Hybrid examples

New authors should start with [simple_ramsey.py](simple_ramsey.py) and the
[step-by-step notebook](../../docs/tutorials/ramsey_clock_walkthrough.ipynb).
The notebook shows the complete editable descriptor and device databases inline;
the Python example keeps those declarations in [ramsey_lab](ramsey_lab/).

```powershell
uv run --locked python -m examples.artiq_spectrum.simple_ramsey
uv run --locked python -m examples.artiq_spectrum.clock_ramsey
```

This offline example exports to `output/simple_ramsey/report/`. It uses the new
descriptor/Qubit interface, explicit detuning and second-pulse phase, and the
existing GreenMOT native fixture lifecycle. Its fixture pulses do not prepare
atoms or retrieve camera images. The profiles below remain compatible examples.

Simple Ramsey scans dark times of 1, 10 and 100 us with zero additional phase
on the second RF pulse. [clock_ramsey.py](clock_ramsey.py) instead uses
RF 100 us → clock 200 us → RF 100 us and scans the second RF phase over
0/45/90/135/180 degrees. The clock demo has a 100 us pi duration and 200 kHz
carrier; doubling a constant square pulse's duration is an illustrative pulse-area
assumption, not a measured gate calibration. Both examples retain 1 kHz RF
detuning (31 kHz output). Clock Ramsey exports to `output/clock_ramsey/report/`.
The notebook includes both bodies inline and exports both under
`output/simple_ramsey/`, together with embedded full-sequence DAC plots.

These examples explicitly select `backend="artiq_spectrum"`. They do not replace
or move existing pure ARTIQ examples. Generated output has its own
`output/artiq_spectrum/` directory. `lab_simulated.json` is an **unmeasured offline
contract** with SIM card serial numbers, not a laboratory configuration.

Run from the project root:

The default profile uses two cards and two synchronous TTLs. The legacy shared
TTL profile is retained separately as `lab_legacy_shared_trigger.json`.
New extension examples:

```powershell
uv run --locked python -m examples.artiq_spectrum.multicard_trigger --cards 3
uv run --locked python -m examples.artiq_spectrum.multicard_trigger --cards 4
uv run --locked python -m examples.artiq_spectrum.native_components
uv run --locked python -m examples.artiq_spectrum.randomized_benchmarking --qubits 1
uv run --locked python -m examples.artiq_spectrum.randomized_benchmarking --qubits 2
uv run --locked python -m examples.artiq_spectrum.capture_comparison
```

See [native components](../../docs/native_components.md),
[full Clifford RB and CZ IRB](../../docs/randomized_benchmarking.md),
[streamed compilation and recovery](../../docs/streamed_execution.md), and
[capture comparison](../../docs/capture_comparison.md).
The RB circuits have exact ideal recovery; the replaceable demonstration CZ
waveforms are explicitly uncalibrated. All commands are offline.

```powershell
uv run --locked python examples/artiq_spectrum/ramsey_long_delay.py
uv run --locked python examples/artiq_spectrum/rabi.py
uv run --locked python examples/artiq_spectrum/spin_echo.py
uv run --locked python examples/artiq_spectrum/spin_locking.py
uv run --locked python examples/artiq_spectrum/allxy.py
uv run --locked python examples/artiq_spectrum/staged_workflow.py
uv run --locked python examples/artiq_spectrum/greenmot_ramsey.py
uv run --locked python examples/artiq_spectrum/section_chunk_rb.py
uv run --locked python examples/artiq_spectrum/phase_modes.py
```

Except for the compact phase comparison below, each command creates
`output/artiq_spectrum/<example>/index.html` with complete
DSL source, a zoomable rectangular pulse sheet, a separate final-DAC waveform
viewer, actual segment binaries, segment/step/pulse CSV tables, ARTIQ trigger
timing and per-shot frame conventions. All defaults are offline.

`section_chunk_rb.py` combines two independent trigger TTLs, reusable `rb_body`
operations for two qubits, explicit ASAP scheduling and two length chunks. Its
fixed seeds produce reproducible gate words; pulse areas remain illustrative.
Reports include chunk pages with actual resident bank slots and finite entries.
`phase_modes.py` instead writes a compact comparison JSON for the four
epoch/continuous and local/carry combinations. The second pulse follows a frame
reset; the third follows a complete phase reset. Use its report option for HTML.
See [sections](../../docs/sections_and_operations.md),
[phase modes](../../docs/spectrum_phase_modes.md) and
[chunk execution](../../docs/chunked_execution.md).

Newly exported reports also include the complete ARTIQ + Spectrum call order,
the actual host/kernel source and an export-time source snapshot of
the Spectrum adapter with hashes. Expand clock/trigger setup, DMA upload,
step-table writes and arm/ready/completion code on the same page. Each point/card
has a resolved `sdk_calls.txt` inspection listing. Explicit clock calibration is
separate from normal shots; these listings are not hardware execution receipts.
Regenerate `ramsey_long_delay.py` to inspect the updated Ramsey report first.

Alternatively, `uv run --locked python scripts/spectrum/export_hybrid_examples.py` exports
all five experiments plus the existing parallel clock/Rydberg demonstration and
creates a single entry page at `output/artiq_spectrum/index.html`.
It also includes `staged_workflow`, whose explicit offline fixture builders show
preparation, cooling and readout composed with `exp.stage()` inside realtime.
These are DSL builders, not embedded native ARTIQ kernels. See the
[native integration design](../../docs/native_artiq_integration.md).

`greenmot_ramsey.py` instead declares native `@kernel` calls through
`NativeKernelPackage`, composed under one top-level shot kernel. The default is
an explicit offline native fixture; `--greenmot-source` selects the external
GreenMOT base. See [the complete native example](greenmot_native/README.md) for
the original run decomposition, real ARTIQ offline compilation, device fixtures,
and control-epoch boundary. Neither mode operates instruments during export.

`decorated_ramsey.py` demonstrates the optional `@experiment` factory, context
managers without `exp.`, explicit/default block launch paths, and an independent
falling-edge `trig()`. It copies the profile in memory and leaves this directory's
shared JSON unchanged. Run it directly to export only this example; see the
[authoring guide](../../docs/authoring_and_triggers.md).

| Example | Default scan | Sequence / frame behavior |
| --- | --- | --- |
| Rabi | 6 amplitudes × 3 durations | One nuclear RF gate; F=0 |
| Ramsey | 10 us, 1 ms, 100 ms dark time | X90, delay, Rz(+pi/2), base-X90; terminal F=-pi/2 |
| Spin echo | 4 symmetric half-delays | X90, delay, Y180, delay, X90; V restored |
| Spin locking | 0, 100, 500, 2000 us lock | +Y90, +X lock, -Y90; V restored by default |
| AllXY | Standard 21 pairs | I occupies a 200 us slot; I/I needs no AWG trigger |

The rotation notation is an illustrative convention: 200 us, amplitude 0.4 for
pi and 0.2 for pi/2, at 30 kHz. It is not a calibrated Rabi frequency. These
single-qubit examples use card A CH0; both enabled channels of card B replay
zero output when a group is active. Card A CH1 is disabled. The existing
`examples/spectrum_hybrid.py` contains the parallel clock/Rydberg modulation demo.

Every shot starts with F=0. Each AWG sequence defaults to its own entry as the
carrier reference; preceding preparation time does not advance that reference.
Use `phase_reference="shot"` explicitly to retain the shot-wide T0. Delays inside
the sequence advance the carrier in either mode. `rz(q,+theta)` subtracts theta
from F; `reset_frame(q.frame)` clears all of F. `reset_phase(q.frame)` additionally
clears carried modulation and reestablishes a zero carrier reference. Spin locking
has `--persist-frame` to retain terminal F=-pi/2 for comparison. This is a logical
frame demonstration, not a physical qubit-state simulator.

The original `scripts/spectrum/explain_spectrum_ramsey.py` remains available to create
static RF/timing figures and its detailed Chinese walkthrough from the Ramsey
export. See [the viewer guide](../../docs/compiled_viewers.md) for controls,
Python snippet access, precision and scope.
