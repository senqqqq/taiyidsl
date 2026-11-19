"""Keep canonical source ownership and dependency boundaries explicit."""

import ast
import subprocess
import sys
from importlib import import_module
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/taiyidsl"


def test_project_imports_use_installed_modules():
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        package = ".".join(("taiyidsl", *relative.parts[:-1]))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                names = [resolve_name(module, package)]
            elif isinstance(node, ast.Import):
                names = [name.name for name in node.names]
            else:
                continue
            for name in names:
                if name.startswith("taiyidsl."):
                    target = ROOT.joinpath(*name.split(".")[1:])
                    assert (
                        target.with_suffix(".py").is_file()
                        or (target / "__init__.py").is_file()
                    ), f"{relative}:{node.lineno} imports missing project module {name}"


def test_artifact_io_has_no_dependency_on_project_layers():
    tree = ast.parse((ROOT / "_artifact_io.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert not (node.module or "").startswith("taiyidsl")
        elif isinstance(node, ast.Import):
            assert all(not name.name.startswith("taiyidsl") for name in node.names)


def test_layer_dependencies():
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
            {"backends"},
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


def test_public_execution_contracts_have_one_owner():
    import taiyidsl
    from taiyidsl.runtime import contracts, run_store

    for name in ("ObservationCommit", "RecoveryRequired", "ShotContext"):
        assert getattr(taiyidsl, name) is getattr(contracts, name)
        assert getattr(run_store, name) is getattr(contracts, name)


def test_public_experiment_and_compilation_types_have_one_owner():
    import taiyidsl
    from taiyidsl.compiler import CompiledExperiment, compile_experiment
    from taiyidsl.dsl import Experiment
    from taiyidsl.dsl.sections import Section

    assert taiyidsl.CompiledExperiment is CompiledExperiment
    assert taiyidsl.compile_experiment is compile_experiment
    assert taiyidsl.Experiment is Experiment
    assert taiyidsl.Section is Section


def test_section_definition_belongs_only_to_dsl():
    definitions = []
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and node.name == "Section":
                definitions.append(path.relative_to(ROOT).as_posix())
    assert definitions == ["dsl/sections.py"]
    assert not (ROOT / "compiler/sections.py").exists()


def test_spectrum_programs_and_validation_keep_distinct_owners():
    program = import_module("taiyidsl.backends.artiq_spectrum.awg")
    checks = import_module("taiyidsl.backends.artiq_spectrum.validation_awg")
    # Validation shares the resource budget, not its independent expected samples.
    assert checks._RENDER_WORK_BYTES is program._RENDER_WORK_BYTES
    assert checks._expected_window.__module__ == checks.__name__
    for node in ast.walk(ast.parse(Path(program.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert "validation" not in (node.module or "")


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
