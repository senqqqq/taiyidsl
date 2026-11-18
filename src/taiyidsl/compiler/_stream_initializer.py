"""Offline source generation for a streamed artifact's union initializer."""

import ast
import copy
from pathlib import Path


def write_union_initializer(directory, chunks):
    """Union only generated control-device initialization, never native build.

    Per-chunk generated device aliases can differ. Normalize their AST references
    to one stable union so devices first used in a later chunk initialize once.
    """
    mappings, statements, names = [], [], set()
    for chunk in chunks:
        source = Path(directory) / chunk["directory"] / "artiq" / "experiment.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "GeneratedExperiment"
        )
        methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        mapping = {}
        for node in ast.walk(methods["build"]):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "get_device"
                and node.value.args
                and isinstance(node.value.args[0], ast.Constant)
            ):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        name = node.value.args[0].value
                        if name != "core":
                            mapping[target.attr] = name
                            names.add(name)
        mappings.append(mapping)
        statements.append(methods["initialize_devices"].body)
    aliases = {name: f"d{i}" for i, name in enumerate(sorted(names))}
    calls = []
    for mapping, body in zip(mappings, statements, strict=True):

        class Rename(ast.NodeTransformer):
            def visit_Attribute(self, node):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                    and node.attr in mapping
                ):
                    return ast.copy_location(
                        ast.Attribute(
                            value=node.value,
                            attr=aliases[mapping[node.attr]],
                            ctx=node.ctx,
                        ),
                        node,
                    )
                return self.generic_visit(node)

        for statement in body:
            if not any(
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
                and n.attr in mapping
                for n in ast.walk(statement)
            ):
                continue
            rendered = ast.unparse(Rename().visit(copy.deepcopy(statement)))
            if rendered not in calls:
                calls.append(rendered)
    lines = [
        "from artiq.experiment import EnvExperiment, kernel, now_mu",
        "",
        "class GeneratedExperiment(EnvExperiment):",
        "    def build(self):",
        "        self.core = self.get_device('core')",
    ]
    lines += [
        f"        self.{alias} = self.get_device({name!r})"
        for name, alias in aliases.items()
    ]
    lines += [
        "",
        "    @kernel",
        "    def initialize_devices(self):",
        "        self.core.reset()",
    ]
    for statement in calls:
        lines.append("        self.core.break_realtime()")
        lines.extend("        " + line for line in statement.splitlines())
    lines.append("        self.core.wait_until_mu(now_mu())")
    source = "\n".join(lines) + "\n"
    ast.parse(source)
    (Path(directory) / "initialize.py").write_text(source, encoding="utf-8")
