"""Named, validated edit points in the generated ARTIQ experiment AST."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from taiyidsl.backends.artiq._contracts import ExportError


@dataclass
class GeneratedProgram:
    tree: ast.Module
    experiment: ast.ClassDef
    methods: dict[str, ast.FunctionDef]

    @classmethod
    def parse(cls, source: str) -> GeneratedProgram:
        tree = ast.parse(source)
        classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "GeneratedExperiment"
        ]
        if len(classes) != 1:
            raise ExportError("Unknown generated experiment class structure")
        generated = classes[0]
        methods = {}
        for node in generated.body:
            if isinstance(node, ast.FunctionDef):
                if node.name in methods:
                    raise ExportError(f"Duplicate generated method: {node.name}")
                methods[node.name] = node
        required = {"build", "run", "_validate_target", "initialize_devices"}
        if missing := required - methods.keys():
            raise ExportError(f"Missing generated methods: {sorted(missing)}")
        if any(name.startswith("kernel_") for name in methods):
            raise ExportError("Native composition requires unrolled point kernels")
        return cls(tree, generated, methods)


@dataclass
class NativeBuildPrelude:
    """Own the native build nodes without assuming their list positions."""

    build: ast.FunctionDef
    package_call: ast.Expr
    root_attribute_check: ast.Expr

    @classmethod
    def install(cls, build, method, attributes) -> NativeBuildPrelude:
        call = ast.parse(f"_NativePackage.{method}(self)").body[0]
        check = ast.parse(
            f"_taiyi_check_native_build(self, {tuple(sorted(attributes))!r})"
        ).body[0]
        build.body[:0] = [call, check]
        return cls(build, call, check)

    def use_shared_context(self):
        """Replace root inheritance checks with component-owned construction."""
        for node in (self.package_call, self.root_attribute_check):
            if sum(item is node for item in self.build.body) != 1:
                raise ExportError("Native build prelude changed before composition")
        self.package_call.value.args.append(
            ast.Name(id="native_context", ctx=ast.Load())
        )
        self.build.body.remove(self.root_attribute_check)
