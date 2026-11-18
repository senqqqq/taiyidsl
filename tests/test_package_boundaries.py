"""Prevent migrated implementations from depending on compatibility forwarders."""

import ast
import subprocess
import sys
from importlib import import_module
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/taiyidsl"
COMPAT = ROOT / "_compat"


def test_implementations_do_not_import_compatibility_forwarders():
    forbidden = {f"taiyidsl.{path.stem}" for path in COMPAT.glob("*.py")}
    assert forbidden, "No compatibility modules discovered"
    violations = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if path.is_relative_to(COMPAT):
            continue
        package = ".".join(("taiyidsl", *relative.parts[:-1]))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, package)
                imported = [module, *(f"{module}.{a.name}" for a in node.names)]
            else:
                continue
            if set(imported) & forbidden or any(
                name.startswith("taiyidsl._compat") for name in imported
            ):
                violations.append(f"{relative}:{node.lineno}: {imported}")
    assert not violations, "\n".join(violations)


def test_artifact_io_has_no_dependency_on_project_layers():
    tree = ast.parse((ROOT / "_artifact_io.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert not (node.module or "").startswith("taiyidsl")
        elif isinstance(node, ast.Import):
            assert all(not name.name.startswith("taiyidsl") for name in node.names)


def test_phase_two_layer_dependencies():
    """Results do not execute/compile; compilation does not load host runners."""
    groups = [
        (list((ROOT / "results").glob("*.py")), {"runtime", "compiler"}),
        (
            list((ROOT / "compiler").rglob("*.py"))
            + list((ROOT / "backends").rglob("*.py")),
            {"runtime"},
        ),
        (
            list((ROOT / "config").glob("*.py")),
            {"dsl", "compiler", "backends", "runtime", "reporting", "lab"},
        ),
        (
            list((ROOT / "dsl").glob("*.py")),
            {"compiler", "backends", "runtime", "reporting"},
        ),
        (
            [
                ROOT / "compiler" / name
                for name in (
                    "scheduling.py",
                    "section_schedule.py",
                    "resources.py",
                )
            ],
            {"backends", "spectrum_schedule", "spectrum_config"},
        ),
        (
            [ROOT / "host_plan.py"],
            {"dsl", "compiler", "config", "backends", "runtime", "reporting", "lab"},
        ),
    ]
    violations = []
    for paths, forbidden in groups:
        for path in paths:
            relative = path.relative_to(ROOT)
            package = ".".join(("taiyidsl", *relative.parts[:-1]))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        module = resolve_name("." * node.level + module, package)
                    names = [module, *(f"{module}.{a.name}" for a in node.names)]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                else:
                    continue
                for name in names:
                    parts = name.split(".")
                    if (
                        len(parts) > 1
                        and parts[0] == "taiyidsl"
                        and parts[1] in forbidden
                    ):
                        violations.append(f"{relative}:{node.lineno}: {name}")
    assert not violations, "\n".join(violations)


def test_phase_two_legacy_modules_share_implementation_identity():
    aliases = {
        "session": "runtime.session",
        "session_worker": "runtime.worker",
        "stream_runtime": "runtime.streaming",
        "run_store": "runtime.run_store",
        "readout": "runtime.readout",
        "spectrum_runtime": "runtime.spectrum.awg",
        "spectrum_dds_runtime": "runtime.spectrum.dds",
        "analysis": "results.analysis",
        "imaging": "results.imaging",
        "rb_analysis": "results.rb_analysis",
        "rb_results": "results.rb_results",
    }
    for old, new in aliases.items():
        assert import_module(f"taiyidsl.{old}") is import_module(f"taiyidsl.{new}")

    import taiyidsl
    from taiyidsl.runtime import contracts, run_store

    for name in ("ObservationCommit", "RecoveryRequired", "ShotContext"):
        assert getattr(taiyidsl, name) is getattr(contracts, name)
        assert getattr(run_store, name) is getattr(contracts, name)


def test_phase_three_public_types_and_legacy_imports_keep_one_owner():
    for old, target in {
        **{name: f"dsl.{name}" for name in ("authoring", "sections", "operations")},
        **{
            name: f"config.{name}"
            for name in (
                "models",
                "parameters",
                "role_parameters",
                "editable_parameters",
                "optics",
                "descriptor",
                "pair_calibration",
            )
        },
        **{
            name: f"compiler.{name}"
            for name in (
                "scheduling",
                "section_schedule",
                "shared_fields",
                "streaming",
                "_stream_initializer",
            )
        },
    }.items():
        assert import_module(f"taiyidsl.{old}") is import_module(f"taiyidsl.{target}")

    for old, target, symbols in (
        (
            "hardware",
            "config.hardware",
            ("DeviceSetup", "SignalMap", "TriggerSequence"),
        ),
        ("hardware", "lab", ("reference_setup",)),
        ("spectrum_config", "config.spectrum", ("SpectrumConfig", "SAMPLE_RATE_HZ")),
        ("spectrum_config", "compiler.trigger_routes", ("resolve_trigger_signal",)),
        ("transport", "config.transport", ("MoveProfile", "IZ", "TransportConfig")),
        ("transport", "compiler.spatial", ("SpatialState",)),
        ("chunking", "host_plan", ("execution_order", "point_execution_layout")),
        ("streaming", "compiler.store", ("CompilationStore", "restore_compiled")),
        ("compiler", "compiler.artifact", ("CompiledExperiment",)),
        ("compiler", "compiler.pipeline", ("compile_experiment",)),
        ("dsl", "dsl._context", ("_active", "_source")),
    ):
        legacy, implementation = (
            import_module(f"taiyidsl.{old}"),
            import_module(f"taiyidsl.{target}"),
        )
        for name in symbols:
            assert getattr(legacy, name) is getattr(implementation, name)


def test_section_definition_belongs_only_to_dsl():
    definitions = []
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and node.name == "Section":
                definitions.append(path.relative_to(ROOT).as_posix())
    assert definitions == ["dsl/sections.py"]
    assert not (ROOT / "compiler/sections.py").exists()


def test_spectrum_programs_and_validation_keep_distinct_owners():
    legacy = import_module("taiyidsl.spectrum_waveforms")
    program = import_module("taiyidsl.backends.artiq_spectrum.awg")
    checks = import_module("taiyidsl.backends.artiq_spectrum.validation_awg")
    assert legacy.build_card_program is program.build_card_program
    assert legacy.validate_program is checks.validate_program
    # Validation shares the resource budget, not its independent expected samples.
    assert checks._RENDER_WORK_BYTES is program._RENDER_WORK_BYTES
    assert checks._expected_window.__module__ == checks.__name__
    for node in ast.walk(ast.parse(Path(program.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert "validation" not in (node.module or "")


def test_spectrum_legacy_modules_share_implementation_identity():
    for old, owner in {
        "spectrum_schedule": "scheduling",
        "spectrum_phase": "phase",
        "spectrum_export": "export",
        "spectrum_banks": "banks",
        "spectrum_dds": "dds",
    }.items():
        assert import_module(f"taiyidsl.{old}") is import_module(
            f"taiyidsl.backends.artiq_spectrum.{owner}"
        )


def test_spectrum_data_import_does_not_load_backend_implementation():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from taiyidsl.backends.artiq_spectrum import awg; "
            "assert callable(awg.build_card_program); "
            "assert 'taiyidsl.backends.artiq_spectrum.backend' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
