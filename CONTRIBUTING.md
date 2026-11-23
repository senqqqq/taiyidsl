# Contributing to taiyidsl

This project follows the [TaiyiQ Python Coding Standards](https://github.com/TaiyiQ/standards/blob/8831cc8d10e6248f8f46c1f41b006b5f4c6338da/python.md).
The link records the standards revision used for the initial local preparation.
Organization membership may be required to read it.

Start with the [developer guide](docs/development.md),
[public API index](docs/public_api.md), and
[current design decisions](docs/design_decisions.md). The
[lab extension example](examples/lab_extension/README.md) demonstrates the
boundary between reusable library components and a laboratory-owned package.
Do not add real device addresses, calibration records, or camera credentials
to examples. Keep them in the lab package or ignored `local_config/`.

## Shared development

Use [TaiyiQ/pulse-dsl](https://github.com/TaiyiQ/pulse-dsl) for issues, code review
and shared development. Collaborators with write access work on feature branches
and open pull requests against `main`. Include focused verification and update
affected examples or guides. Keep `main` usable by the laboratory; instrument
acceptance remains separate from offline CI. The personal `senqqqq/taiyidsl`
repository is a synchronized copy, not a separate development line.

## Development environment

Use uv to manage CPython, the virtual environment, and dependencies. The project
pins CPython 3.12.15 in `.python-version`; `requires-python` defines its supported
Python series separately. Do not replace the pin with a machine-specific path.

```bash
uv sync --locked
uv run --locked python examples/tuneup_workflow.py
uv run --locked python examples/state_initialization.py
```

Install Node.js 22 to run the JavaScript syntax test as well as the Python tests.
The CI workflow provisions Node explicitly. On Windows PowerShell, setting
`$env:PYTHONIOENCODING = "utf-8"` makes command output consistent.

Use `uv add` for runtime dependencies and `uv add --dev` for development tools.
Commit `pyproject.toml` and `uv.lock` together when dependencies change. Use
`uv sync --locked` in checks to detect an out-of-date lockfile instead of silently
updating it. A Python pin change must be reviewed with the supported-version
declaration and CI configuration.

## Required checks

### During development

Select checks by the behavior affected, rather than running the full pipeline
after every conversation or edit:

- Explanations, prose documentation and comment-only edits need content review,
  not pytest or a package build. Executable examples and documentation generator
  changes still need relevant behavior checks.
- Local code changes need focused tests for the changed behavior and directly
  affected compatibility contracts. Run Ruff on the changed Python files.
- Before testing, map changed behavior to affected call paths and necessary
  assertions. Select the smallest sufficient set, down to test functions and
  parameter cases where practical. Cover relevant normal, boundary and
  compatibility behavior without repeating equivalent checks across tests,
  example exports and compilation jobs.
- Shared scheduling, time units, phase, IR or cross-backend changes do not
  automatically require a full regression run. Cover affected semantics and
  backends first. Broaden only when impact cannot be bounded reliably, focused
  checks reveal wider problems, or release-level verification is explicitly
  required. Explain the uncertainty each additional check resolves.
- Run real ARTIQ offline compilation for affected generated programs or native
  call graphs. Build packages and regenerate reports only when the change or
  requested deliverable requires them; these are not per-response requirements.
- Reuse successful checks while their inputs and assumptions remain valid.
  Repeat or broaden them only for relevant later changes, failures or unresolved
  concerns. Report focused coverage honestly; do not present an earlier full
  suite result as a new run.

Keep useful existing regression tests. Reduce routine execution scope instead
of deleting coverage or imposing an arbitrary test-count limit. New tests should
protect meaningful behavior, not mirror implementation or repeat low-risk checks.

### Cross-stage integration checkpoints

At an agreed integration checkpoint after a coordinated multi-package refactor
or tutorial migration, check the final code: execute the plain Ramsey, operation
Ramsey, Yb tune-up and transport notebooks in fresh offline kernels, compare the
two Ramsey versions' corresponding experiments, and run the full offline suite
locally or through CI. Compare events, scan order, phase, final DAC/DDS data and
acquisition indices rather than source text or provenance line numbers alone.
Use this wider scope to check interactions missed by isolated module tests;
it is not required for every subsequent local edit.

Update notebook generators and generated notebooks together. Keep execution
receipts tied to the source tested; a byte-unchanged notebook can still be
affected by changed library code. Software execution, real ARTIQ compilation,
browser interaction and hardware acceptance remain separate claims. See the
[tutorial maintenance commands](docs/tutorials/README.md).

### Before a commit or push

Apply the same impact-based selection to local pre-commit and pre-push checks;
do not automatically rerun the full suite. Reuse still-valid verification of
the final change set. Check formatting and lint only on changed Python files,
and build packages only for packaging changes or a requested release artifact.

For repository preparation and the final published source set, run full
`ruff check .` and `ruff format --check .` as required by the organization
standard. This does not require repeating all behavioral tests locally;
the existing CI retains its full offline suite.

The commands below describe the full release verification when that scope is
needed, not a mandatory local checklist for every commit or push:

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest --basetemp=.test-tmp-pytest
uv build --no-sources
```

Ruff uses an 88-character line length, Python 3.12, double quotes, space
indentation, and the `E`, `F`, `W`, `I`, and `UP` rule groups. Do not silently
disable rule groups. Document any narrowly scoped exception and its technical
reason. Generated programs, cached dependencies, test temporary directories,
and build output are excluded from source linting; their executable behavior
is covered by export and artifact tests.

The `.test-tmp-pytest` directory is disposable: pytest can clear its selected
base directory. Do not store research data there. Plain `uv run --locked pytest`
also works in environments with an accessible system temporary directory.

CI runs the non-mutating Ruff checks, tests, and package build on Windows and
Linux. It also exercises the examples. Review failures before merging; a local
pass does not establish that a new CI run or a hardware check has passed.
Pre-commit hooks are optional conveniences. If adding them, pin the hook/tool
version and keep its rules aligned with the locked development environment.

## Experiment authoring convention

Introductory tutorials target physicists who may not be experienced Python
programmers. Follow this route: **device databases -> descriptor -> LabConfig
-> Qubit -> native ARTIQ -> experiment definition -> compile and visualization
-> explicit hardware connection**. Start with the
[plain Ramsey lesson](docs/tutorials/ramsey_clock_plain.ipynb); its
[operation variant](docs/tutorials/ramsey_clock_walkthrough.ipynb) adds small
reusable gates while keeping the same experimental structure.

Show editable configuration and pulse timing directly. Prefer ordinary mappings,
explicit physical parameters and short functions. Do not introduce dynamic
imports, namespace wrappers, generic configuration frameworks or Python utility
machinery to hide a few lines of experiment code. Numerical analysis may use
NumPy and Matplotlib, but must not conceal the emitted pulse sequence. Keep
advanced native composition, chunking, recovery and camera adapters in linked
extension material. Use the recommended public `taiyidsl` imports; internal
package moves should not leak into a lesson's imports. Present hardware
connection as an explicit final step outside offline Run All validation.

Define, instantiate, and name every pulse before writing the experiment body.
Inside experiment contexts, pass named pulse objects to `play` or operations;
do not construct pulses inline, such as `play(q.rf, Square(...))`. Reuse a named
pulse for repeated gates and supply per-call amplitude, frequency, and phase
through `play` or explicit operation arguments. Do not create duplicate pulses
or use `replace` merely to change a call's phase. Keep pulse definitions separate
from timing; a scan may be supplied as a call argument without cloning the pulse.
Apply this convention to new and revised notebooks, examples, and experiment
scripts. Define experiments as functions with explicit qubit, pulse, scan/control,
preparation/readout, and execution arguments, returning an `Experiment`. Keep
compilation and hardware execution outside those functions.
For introductory examples, use one explicit sweep axis: Ramsey sweeps `tau`,
clock Ramsey sweeps `phase`; other control arguments are scalars. Do not add
axis-discovery logic, `ExitStack`, or conditional scan branches unless requested.
Omit pulse parameters supplied by established defaults. For Ramsey, explicitly compute
`frequency = q0.parameters.rf.frequency * Hz + detuning` once in the experiment
function and pass it to both RF pulses. Clock frequency and zero phase can use
defaults. Do not pass detuning again after applying it to frequency.
RF is physically a global nuclear-spin field. The introductory Ramsey examples
model only the observed q0; do not introduce unobserved atoms into the lesson. Map their RF logical signals to one explicitly shared output, emit one
pulse, record every affected target, and state which atom supplies calibration.
Infer those targets from the declared shared wiring and LabConfig registration;
do not repeat `targets` or every affected qubit in experiment function arguments.
Explicit targets remain an optional checked assertion, not a required second
source of wiring truth. Use `@experiment` as an optional teaching alternative
to ordinary functions that return an Experiment.
Distinct Qubit IDs do not imply independently addressable RF outputs. Do not
infer that clock or other optical routes are global without a declaration.

Authoring Qubit parameters can be edited directly. Read physically meaningful
calibrations explicitly when defining named pulses: use `pi2_duration` and
`pi2_amplitude` for the Ramsey half-pi pulse, and clock `pi_amplitude` for its
constant-amplitude 2pi demonstration. Do not add a redundant generic amplitude
to make defaults work. Keep detuning as an experiment argument and compute RF
frequency explicitly in that function; play inherits pulse duration/amplitude
and only overrides RF frequency and the second pulse's phase as needed.
After editing amplitude/duration, recreate the named pulse, call the experiment
function, then compile and refresh reports. Frequency-only edits need a new
experiment and compilation, but not a new pulse when its frequency is unset.
These rules apply to ordinary and decorated experiment functions alike.
Existing experiments and compiled results retain their automatically captured
parameter copies; a QubitStore is optional for versioned persistence.

Tutorial compatibility means preserving the experiment, scan, timing, phase,
and teaching structure, not freezing every import or historical call spelling.
Use the recommended public APIs and update the notebook generator together
with its output. Explain meaningful API migrations. Keep the experimental
sequence visible instead of hiding it in large helpers. Existing source
notebooks are published without execution outputs; retain useful executed
copies in ignored output directories before clearing them.

## Tests and behavior changes

Choose tests that validate externally observable behavior:

- For DSL and compiler changes, cover time ordering, resource conflicts,
  quantization, snapshot isolation, and acquisition indexing where relevant.
- For exporter changes, execute generated code with the independent mock
  drivers. Compare parameterized and unrolled exports using
  `taiyidsl.artiq.verify_equivalence` and include meaningful failure cases.
- For result and calibration changes, verify missing-data handling, source
  matching, quality gates, and transactional persistence.
- For examples, keep all defaults offline and distinguish illustrative device
  settings from laboratory calibration. Use temporary output in tests.

Formatting-only changes must not alter pulse timing, DDS parameters, averaging
order, result layout, or calibration application. Source line numbers and
provenance hashes that include source locations may change when source is
reformatted; compare the actual event and data semantics as well.

Never replace an unexpected test failure by updating expected results without
first establishing whether the behavior change is intentional.

### Generated experiment reports

- Show actual exported host/kernel code and snapshots of the runtime adapter,
  with source hashes and source ranges. Label manifest-resolved SDK call listings
  as inspection aids; do not present a handwritten approximation as actual code
  or an offline listing as a hardware execution receipt.
- Derive per-card settings, segment slots and step arguments from the exported
  contract/program. Distinguish configured values from adapter defaults. Hardware
  segment slots follow upload order, not arbitrary logical segment identifiers.
- Keep explicit clock initialization/recalibration separate from normal shots.
  Explain entry-step selection, arming, readiness and external triggering as
  distinct operations, following the current runtime source.
- For report-only changes, compare final samples, step tables, trigger timing
  and phase/frame semantics against the prior artifacts where available. Check
  source escaping and local links. Regenerate only the requested examples.
- Keep export/inspection offline. Static HTML checks are not rendered browser
  QA; mock execution, real ARTIQ compilation and physical output acceptance are
  separate claims. State what remains unverified.

## Documentation and repository contents

Write shared documentation in English, including tutorial generators, notebook
Markdown, example instructions and contributor-facing project guidance. Keep
units, defaults, supported operations and physical limits consistent with the
code. Link previous validation evidence by commit when it explains a decision.
Record new checks with the exact source identity, inputs, environment and scope.
For an import migration, collect the suite, check installed public symbols from
outside the checkout, run representative Python experiments and execute the
affected tutorials in fresh offline kernels. Compare the plain and operation
Ramsey outputs for timing, phase, acquisition indices and final device data.

The tracked project should include source, tests, examples, documentation,
`.python-version`, `pyproject.toml`, `uv.lock`, and collaboration configuration.
Do not commit virtual environments, caches, generated `output/`, built `dist/`,
or experimental data. CI artifacts and reproducible example commands provide
generated demonstrations without filling source history with large HTML files.

Reuse `.test-tmp-pytest` for ordinary focused checks. Remove dedicated temporary
directories after parallel jobs finish; do not accumulate a new directory for
every verification pass or put pytest working directories in `output/`.
After confirming a final deliverable, remove superseded intermediate outputs
while retaining its dependent data, banks, source snapshots and verification
evidence. Check references before cleanup. Age, filenames and untracked Git
status alone do not establish obsolescence; measurements, calibration data,
databases and supplied reference files are not disposable caches. Cleanup needs
scope and retained-file integrity checks, not another test-suite run.

Runtime dependencies belong in `pyproject.toml`, not in ad-hoc installation
instructions. Source modules use the `src/taiyidsl/` layout. Examples import the
installed package rather than manipulating `sys.path`.

No repository license is selected by these instructions; licensing and any
external distribution policy are organization decisions.

See [repository contents](docs/repository_contents.md) for the inclusion policy
and `scripts/repository_inventory.py` for a per-file inventory. Local history
and reference-only sources are not publication inputs. Only push explicitly
selected branches; never mirror local recovery/checkpoint refs to a remote.

## Branches and pull requests

Once an organization repository is available, work on a focused branch and open
a pull request against its default branch. Keep format cleanup separate from
unrelated functional changes where practical. Explain the concrete change,
motivation, verification, and relevant limitations in the PR description.

Wait for the required checks and reviewer approval before merging. Repository
creation, team permissions, and branch-protection settings are handled by the
organization administrators; these files do not configure remote access.

## Hardware boundary

The core package and CI must remain runnable without ARTIQ or instruments. The
reference target is ARTIQ 9 with AD9910 and TTL, but mock verification does not
validate real compiler support, RTIO submission capacity, device synchronization,
or optical output at the atoms.

Adapters must declare their clocks, preparation time, effective output timing,
shared resources, and state changes. Preserve separate statuses for offline
verification, real ARTIQ compilation, and physical hardware acceptance. Do not
silently turn an unsupported timing request into a different experiment.
