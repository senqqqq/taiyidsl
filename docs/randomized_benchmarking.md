# Clifford RB and CZ interleaved RB

The mathematical circuit generator, pulse implementation, measured joint
outcomes and fitted error estimate are separate objects. An ideal recovery
identity proves the circuit algebra; it does not prove that a pulse implements
the intended gate. All example defaults are offline.

## Generate frozen circuits

```python
from taiyidsl.rb import generate_rb_sequences

circuits = generate_rb_sequences(
    [0, 1, 4, 12],
    sequence_count=20,
    seed=101,
    num_qubits=2,
    interleaved=True,
)
```

Single-qubit mode preserves `templates.rb_sequences` and its 24-element SO(3)
catalogue, seeds and primitive gate words. Two-qubit mode uses all 11,520
Cliffords, represented by exact signed Pauli conjugation tableaus. There is no
Qiskit runtime dependency. The catalogue version, sampled indices, primitive
gates and recovery index are saved in each descriptor. Chronological gate
matrices multiply on the left; global phase is irrelevant to this tableau.

The two-qubit catalogue is enumerated using local ±X90/±Y90/X180/Y180 and CZ.
Its deterministic decompositions minimize CZ count first, then single-qubit
pulse count. The numbers requiring 0/1/2/3 CZ gates are 576/5184/5184/576.
This is an explicit compilation cost, not an estimate of pulse quality.
Sampling uses a uniform integer over the complete catalogue; a random walk
over primitive gates would not implement this sampling contract.

For each `(seed, length, sequence)` the reference and interleaved descriptors
share the same random Clifford indices. The reference executes each Clifford
once; the interleaved circuit follows each random Clifford with one CZ. Each
variant computes its own inverse of the whole accumulated circuit, including
the interleaved CZ gates. Length counts random Cliffords and excludes recovery.
At length zero both circuits are the identity. Changing shots or chunks never
redraws random gates. The default execution order visits each reference/IRB pair
adjacently; it is deterministic and is not claimed to remove laboratory drift.

These definitions follow the full two-qubit protocol and joint survival
measurement in [Córcoles et al., Process verification of two-qubit quantum gates
by randomized benchmarking](https://arxiv.org/abs/1210.7011).

## Bind a circuit to pulses

```python
from taiyidsl import QubitOperations
from taiyidsl.rb_dsl import rb_sequence_section

ops = QubitOperations(drive="rf", cz_impl=my_lab_cz)
with exp.triggered_sequence(group="qubit_awg"):
    rb_sequence_section((q0, q1), circuits[0], ops)
```

The helper creates a reusable Section. Each primitive is a complete circuit
layer on the declared register, so disjoint targets in consecutive layers do
not overlap when inserted inside ASAP scheduling. It does not reset frames,
prepare atoms, read out a state, average or start another triggered sequence.
Those decisions belong to the surrounding experiment.

`my_lab_cz(q0, q1)` emits DSL or returns a Section. Supply measured pair
calibration and the corresponding qubit snapshots through `QubitOperations`
when available. The implementation must include the actual interaction and
its measured phase corrections; the wrapper cannot infer these from the ideal
CZ matrix. RF/Raman control and clock control use their declared phase frames.

`examples/artiq_spectrum/randomized_benchmarking.py` contains complete one- and
two-qubit examples with four independently instantiated ARTIQ components
(MOT preparation, cooling, state preparation, readout), two cards and two
trigger TTLs, length chunking and joint acquisition. Pass `native_program=` to
replace the four native stage declarations; its method contract is documented
in `build_experiment`. The default components are TTL timing fixtures, not
physical atomic preparation or detection. The timed 20 us cooling and 8 us
state-preparation stages are followed by an explicit 1 us preparation-to-gate
interval, which keeps the AWG's early trigger outside the opaque native call.
The generated camera acquisition TTL precedes a separate 10 us native readout
illumination fixture. This Z-basis readout does not consume the terminal phase.
Its explicitly named
`demonstration_cz` plays two ordinary clock pulses. **Those pulses are not a
physical CZ implementation.** The pair snapshot is marked `demonstration`.

```powershell
uv run --locked python examples/artiq_spectrum/randomized_benchmarking.py --qubits 1
uv run --locked python examples/artiq_spectrum/randomized_benchmarking.py --qubits 2
uv run --locked python examples/artiq_spectrum/randomized_benchmarking.py --qubits 2 --synthetic-results
uv run --locked python examples/artiq_spectrum/randomized_benchmarking.py --qubits 2 --stream --chunks auto --shots 2 --sequences 2 --mock-run-store
```

The third command creates artificial depolarizing observations and runs the
analysis path. `synthetic_results/` and `synthetic_analysis.json` identify their
data source. They are not predictions from the demonstration waveform, measured
fidelities or instrument execution receipts. The example's small default
sequence count illustrates the workflow; choose enough independent sequences
and a suitable length range for an actual statistical experiment.
The fourth command exercises persistent mock execution and recovery; add
`--synthetic-results` separately when fitted synthetic results are also wanted.

`--stream` compiles directly to `compiled_store/` and exports a paged final-DAC
viewer under `viewer/`. `--chunks auto` selects automatic chunk boundaries;
`--host-memory-mib` controls the managed compiler budget, not total process RSS
or caller-owned DSL/metadata. Complete stores require a fresh empty destination
and are not overwritten. With `--synthetic-results`, artificial data are saved
and counted one chunk at a time, retaining only compact joint counts for fitting.
The synthetic RNG uses condition/shot coordinates so changing chunk boundaries
does not change artificial observations.

`--mock-run-store` requires streaming. It uses the actual SpectrumRunner host
preflight/upload protocol with mock card adapters, a mock kernel dispatcher and
artificial observations, commits them through RunStore, then verifies that a
resume skips committed shots. `mock_run/offline_summary.json` records this
limited validation level: generated ARTIQ kernels and native components are
**not executed** by that mock. The journal's generic `hardware_complete` stage
therefore describes completion of the mock adapter callback, not hardware.
Real ARTIQ compilation remains a separate check. The complete native RB chain
can be compiled with `scripts/compile_native_artiq.py`, the disconnected
`native_stages/rb_device_db_offline.json` fixture and optional repeated
`--point` arguments; initialization and session finalization remain included.

## Count joint measurements without implicit postselection

```python
from taiyidsl.results.rb_results import joint_rb_counts

counts = joint_rb_counts(
    results,
    "bits",
    qubits=("q0", "q1"),
    variant_labels={0: "reference", 1: "interleaved"},
    loss_policy="count_as_failure",
    invalid_policy="error",
)
```

The input `ResultSet` contains classified zero/one outcomes indexed by the same
point, shot and qubit. Zero is the prepared/recovered logical state. `P00` is
the fraction of joint 00 outcomes; it is not `P0(q0) * P0(q1)`. The extraction
retains all 00/01/10/11 counts, lost/invalid shot counts, ordered status
combinations, the denominator and exclusions. Other varying sweep axes must
be sliced explicitly. Numeric variant coordinates require an explicit label
mapping; sequence and length must be integer-valued coordinates.

Loss and invalid-data policies independently accept `error` (default),
`count_as_failure` or `exclude`. Exclusion is never automatic. Counting a loss
as failure estimates a different quantity from survival-conditioned analysis;
either interpretation must be justified for the actual detector protocol.
Invalid status takes precedence in mixed invalid/lost shots, with the complete
status combination retained. Raw `ResultSet` data are never modified.

## Fit and report uncertainty

```python
from taiyidsl.results.rb_analysis import analyze_rb

report = analyze_rb(counts, bootstrap_samples=500, seed=7)
```

Reference and interleaved decays are fitted independently to
`A * alpha**length + B`. Both A and B are free; B is not forced to 1/4.
Sequence survival probabilities receive equal weight when forming each length
mean. Fit weighting uses observed sequence scatter with a finite-shot floor.
The report includes RMSE, reduced chi squared and diagnostic flags, alongside
the original joint counts and outcome policies. At least four distinct lengths
and two independent sequences per length are required for this bootstrap path.

For dimension d, Clifford EPC is `(d-1)/d * (1-alpha)`: d=2 for one qubit and
d=4 for two. This is an error per Clifford, not a division by the number of
primitive pulses. The CZ interleaved estimate is
`3/4 * (1-alpha_interleaved/alpha_reference)`.

The bootstrap resamples sequence IDs within each length. All counts belonging
to a chosen reference/interleaved pair stay together. It does not treat each
shot as a new independent random circuit. Failed bootstrap fits are counted;
an interval is omitted if fewer than 80% of fits succeed. The reported
percentile intervals cover alpha, EPC and the raw CZ estimate.

The model-dependent IRB systematic half-width from Eqs. (4)–(5) of
[Magesan et al., Efficient Measurement of Quantum Gate Error by Interleaved
Randomized Benchmarking](https://arxiv.org/abs/1203.4550) is reported separately.
With `p=alpha_reference`, `pc=alpha_interleaved`, it is the smaller of

```text
(d-1)/d * (abs(p - pc/p) + 1 - p)
2*(d*d-1)*(1-p)/(p*d*d) + 4*sqrt(1-p)*sqrt(d*d-1)/p
```

This bound relies on the paper's noise assumptions; it is not a bootstrap
confidence interval. Raw negative estimates and intervals extending beyond
physical error limits remain visible. Poor decay fits, loss postselection,
drift or a gate-dependent/coherent noise model require scientific review;
neither a small fit residual nor an ideal inverse verifies the physical CZ.
