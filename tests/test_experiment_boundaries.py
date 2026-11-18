"""Keep ideal sequence mathematics separate from experimental DSL construction."""

import ast
from importlib import import_module
from importlib.util import resolve_name
from pathlib import Path


def test_ideal_rb_math_depends_only_on_other_ideal_math():
    root = Path(__file__).resolve().parents[1] / "src/taiyidsl/experiments"
    allowed = {"taiyidsl.experiments._single_qubit_clifford"}
    for name in ("__init__", "_single_qubit_clifford", "rb_sequences"):
        tree = ast.parse((root / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name(
                        "." * node.level + module, "taiyidsl.experiments"
                    )
                imported = [module]
            elif isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            else:
                continue
            for module in imported:
                assert not module.startswith("taiyidsl") or module in allowed


def test_legacy_experiment_imports_and_shared_math_keep_one_owner():
    for old, new in (
        ("ramsey", "ramsey"),
        ("cooling", "cooling"),
        ("templates", "templates"),
        ("rb", "rb_sequences"),
        ("rb_dsl", "rb_sections"),
    ):
        assert import_module(f"taiyidsl.{old}") is import_module(
            f"taiyidsl.experiments.{new}"
        )

    math = import_module("taiyidsl.experiments._single_qubit_clifford")
    templates = import_module("taiyidsl.templates")
    sequences = import_module("taiyidsl.rb")
    for name in (
        "_RX",
        "_RY",
        "GATE_ROTATIONS",
        "_rotation_key",
        "clifford_group",
        "sequence_rotation",
        "rb_sequences",
    ):
        assert getattr(templates, name) is getattr(math, name)
    assert sequences.clifford_group is math.clifford_group
    assert sequences.rb_sequences is math.rb_sequences
