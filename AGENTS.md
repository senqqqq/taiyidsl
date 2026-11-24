# Working in TaiyiDSL

Start with the [design decisions](docs/design_decisions.md), [developer guide](docs/development.md) and [contribution guide](CONTRIBUTING.md). The [source map](docs/source_layout.md) identifies the owning module. Import from that module inside the library. The DSL defines Section and its composition API. The compiler places sections through `section_schedule`.

Use the supported [public API](docs/public_api.md) in experiments. Local notes in an ignored `MEMORY.md` can provide context. Check the current code and evidence before relying on a historical conclusion.

## Reports and tutorials

Follow the generated experiment report rules in CONTRIBUTING. Distinguish exported source, parameter previews, execution receipts and evidence from physical devices. Preserve samples, timing and phase when changing presentation. Relevant guides cover [authoring and triggers](docs/authoring_and_triggers.md), [native ARTIQ](docs/native_artiq_integration.md) and [compiled viewers](docs/compiled_viewers.md).

## Verification

- Choose tests by changed behavior, affected callers and the assertions that distinguish a regression. Cover the normal path, relevant boundaries and existing consumers. Reuse passing evidence until a change affects its conclusion.
- Inspect prose and links for documentation edits. Exercise executable examples and documentation generators when their behavior changes. Run Ruff on changed Python files.
- For shared scheduling, time units, phase, IR or backends, check the affected semantics and targets. Expand coverage when failures or uncertain dependencies warrant it or when preparing a release. Use real ARTIQ compilation for affected generated programs and native call chains. Report checks actually performed.
- Use `.test-tmp-pytest` for focused checks. Remove temporary directories created for parallel jobs when those checks finish.
- For Spectrum AWG changes, preserve the [stopped-card sequence commit order](docs/spectrum_awg_sequence.md#when-the-driver-validates-the-start-step) in both standalone and hybrid paths. Exercise incomplete uploads on the SDK double; do not use a live trigger to test setup.

## Files and evidence

Keep final reports, dependent banks, data, source snapshots and verification receipts together under ignored `output/`. Verify paths, references and intended use before removing intermediate artifacts. Retain measurements, calibration records, databases and supplied reference material according to their purpose. A clean-up needs a file-integrity check. Hardware operations require explicit authorization.
