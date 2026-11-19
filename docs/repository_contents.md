# Repository contents and packaging

The wheel installs `src/taiyidsl/`, including the HTML and JavaScript used by report viewers. The source distribution also includes tests, examples, documentation, tutorial inputs, scripts, CI configuration, project metadata and the dependency lock. Install the source checkout to run tutorials.

| Files | How to handle them |
| --- | --- |
| Source, frozen test fixtures, simulated examples, tutorial generators and unexecuted notebooks | Track in Git. Keep tests with the fixtures they read. |
| `.venv/`, `.uv-cache/` and `.toolchains/` | Local environments, package cache and separate ARTIQ installation. Share dependency and installation instructions. |
| `output/` | Generated reports, banks, source snapshots, raw observations and compilation receipts. Preserve the inputs and assets for any retained experiment. |
| `dist/`, `build/` and test or linter caches | Reproducible local or CI build outputs. |
| `artiq_demo/`, third-party RB reference, local memory and design drafts | Local reference materials. Current contracts live in the [design decisions](design_decisions.md). |
| Local environment files, laboratory configuration and `dataset_db.mdb` | Keep with the laboratory's configuration and data. Use simulated configurations for shared examples. |

A Git ignore rule does not remove a tracked file or authorize deletion. Check dependencies before cleaning up a report or discarding a bank, calibration, database or measurement. Use explicit file classes so valid JSON fixtures remain visible.

## Installation

- Run `uv sync --locked` for core development. Add `--group notebooks` for the tutorials. Run the workshops from the repository root.
- The optional `spectrum` extra installs the Python SDK. The supported range and exact locked version are recorded in `pyproject.toml` and `uv.lock`. The native card driver is installed separately.
- ARTIQ and its compiler run in a separate environment. Configure the interpreter via `LabConfig.artiq_python`. See the [ARTIQ toolchain guide](artiq_toolchain.md).
- Laboratory native packages and camera SDKs belong in the environment that executes them. The [external lab template](../examples/lab_extension/README.md) illustrates this boundary.

For a release inventory, run `uv run --locked python scripts/repository_inventory.py`. It writes paths, sizes, Git status, inclusion reasons and hashes under `output/release_prep/`. Review flagged files before publication. The inventory performs local inspection and leaves staging and publication to Git.

Check that a wheel installed away from the checkout can import its public API and read its packaged resources. For a source distribution, check documentation links, runnable examples and tutorial inputs. Push only the intended branches. The repository owners determine licensing, external distribution and access.

The [fixed laboratory preview](https://github.com/TaiyiQ/pulse-dsl/tree/8bc6ad820a4be72bc5eae36bdffd900318837d78) preserves the earlier Chinese introduction, historical notebook, validation log, roadmap and historical ARTIQ hashes. Their evidence applies to their recorded source and environment.
