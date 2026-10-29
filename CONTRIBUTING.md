# Contributing to taiyidsl

This project follows the [TaiyiQ Python Coding Standards](https://github.com/TaiyiQ/standards/blob/8831cc8d10e6248f8f46c1f41b006b5f4c6338da/python.md).
The link records the standards revision used for the initial local preparation.
Organization membership may be required to read it.

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

Run from the project root before a commit or push:

```bash
uv run --locked ruff format .
uv run --locked ruff check --fix .
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

## Documentation and repository contents

Keep the root `README.md` in English without CJK characters. The existing Chinese
guide is in `docs/README.zh-CN.md`; additional language-specific tutorials may
live under `docs/`. Keep units, defaults, unsupported capabilities, and hardware
limitations consistent across the code and documentation.

The tracked project should include source, tests, examples, documentation,
`.python-version`, `pyproject.toml`, `uv.lock`, and collaboration configuration.
Do not commit virtual environments, caches, generated `output/`, built `dist/`,
or experimental data. CI artifacts and reproducible example commands provide
generated demonstrations without filling source history with large HTML files.

Runtime dependencies belong in `pyproject.toml`, not in ad-hoc installation
instructions. Source modules use the `src/taiyidsl/` layout. Examples import the
installed package rather than manipulating `sys.path`.

No repository license is selected by these instructions; licensing and any
external distribution policy are organization decisions.

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
