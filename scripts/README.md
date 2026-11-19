# Repository maintenance tools

Experiment authors import the [library](../src/taiyidsl/) and can start with the [plain Ramsey tutorial](../docs/tutorials/ramsey_clock_plain.ipynb). The scripts below use the source checkout to build lessons, generate reports and inspect compiled output. The wheel installs the library and the source distribution also includes these tools.

| Task | Entry points |
|---|---|
| Edit a lesson | Four `build_*.py` scripts generate five notebooks. The two introductory Ramsey variants share a generator. See the [tutorial index](../docs/tutorials/README.md) for inputs and commands. |
| Execute or compare tutorials | [execute_hybrid_tutorial.py](execute_hybrid_tutorial.py) starts a fresh project kernel. [tutorial_semantics.py](tutorial_semantics.py) compares selected compiled experiments. CI calls these scripts directly. |
| Compile generated ARTIQ | [compile_native_artiq.py](compile_native_artiq.py) compiles exports with an independent ARTIQ environment. [artiq-python.ps1](artiq-python.ps1) selects the Windows interpreter. See [toolchain setup](../docs/artiq_toolchain.md). |
| Diagnose driver timing | Select the [square](verify_square_driver.py), [RAM](verify_ram_driver.py), [polar](verify_polar_driver.py) or [mixed polar](verify_mixed_polar_driver.py) path. The checks use real driver code with virtual RTIO. [audit_phase_paths.py](audit_phase_paths.py) audits driver phase and [verify_native_preparation_tail.py](verify_native_preparation_tail.py) checks the Zotino boundary. |
| Compare scheduling changes | [verify_section_plan_baseline.py](verify_section_plan_baseline.py) compiles examples and compares timing, phases, AWG sample summaries and shot order with a specified JSON baseline. |
| Analyze Spectrum exports and data | [Export hybrid examples](spectrum/export_hybrid_examples.py), [inspect an exported Ramsey sequence](spectrum/explain_spectrum_ramsey.py), [compare phase descriptions](spectrum/verify_spectrum_phase_equivalence.py) or [analyze a saved calibration](spectrum/analyze_spectrum_calibration.py). |
| Inventory files before publishing | [repository_inventory.py](repository_inventory.py) reports Git status and distribution rules. See [repository contents](../docs/repository_contents.md). |

Existing laboratory commands for those four Spectrum scripts can use the `scripts/spectrum/` directory with the same arguments and output destinations. Put shared importable behavior in its owning library module, runnable experiments in [examples](../examples/) and repeatable assertions in [tests](../tests/).
