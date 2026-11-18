"""Keep source generation and independent executable verification separate."""

import ast
from pathlib import Path


def implementation_dependencies():
    package = Path(__file__).resolve().parents[1] / "src/taiyidsl/backends/artiq"
    prefix = "taiyidsl.backends.artiq."
    dependencies = {}
    for path in package.glob("*.py"):
        dependencies[path.stem] = {
            node.module.removeprefix(prefix)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith(prefix)
        }
    return dependencies


def test_artiq_generator_has_no_dependency_on_executable_verification():
    dependencies = implementation_dependencies()
    pending = ["codegen", "codegen_polar", "_contracts", "polar_contracts"]
    visited = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        pending.extend(dependencies[module])
    assert not visited & {
        "mock_drivers",
        "verification",
        "verification_common",
        "verification_polar",
        "validation_polar_output",
    }


def test_artiq_implementation_dependencies_have_no_internal_cycle():
    dependencies = implementation_dependencies()

    def visit(module, ancestors):
        assert module not in ancestors, " -> ".join((*ancestors, module))
        for dependency in dependencies[module]:
            visit(dependency, (*ancestors, module))

    for module in dependencies:
        visit(module, ())
