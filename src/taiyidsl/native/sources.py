"""Collect explicitly rooted native Python sources without importing them."""

from __future__ import annotations

import ast
from pathlib import Path


def package_sources(root, module, class_name, source_overrides=None):
    """Follow local static imports beneath an import root and explicit overrides.

    ``root`` contains the top-level module/package (it is the directory one
    would put on Python's import path). Third-party dependencies are not copied
    from the active Python environment. An override is an explicit module/file
    mapping for user code kept outside this root, such as an existing lab base.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Native package root must be a directory")
    overrides = dict(source_overrides or {})
    for name, path in overrides.items():
        if not isinstance(name, str) or not all(
            p.isidentifier() for p in name.split(".")
        ):
            raise ValueError("Source overrides require dotted Python module names")
        path = Path(path).resolve(strict=True)
        if path.suffix != ".py" or not path.is_file():
            raise ValueError(f"Native override must be a Python file: {path}")
        overrides[name] = path

    def locate(name):
        if name in overrides:
            return overrides[name]
        relative = Path(*name.split("."))
        for candidate in (
            root / relative.with_suffix(".py"),
            root / relative / "__init__.py",
        ):
            if candidate.is_file():
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    raise ValueError(
                        f"Native source escapes declared root: {candidate}"
                    )
                return resolved
        return None

    entry_path = locate(module)
    if entry_path is None:
        raise ValueError(f"Native entry module not found beneath root: {module}")
    sources, trees = {}, {}
    pending = [module]
    while pending:
        name = pending.pop()
        if name in sources:
            continue
        path = locate(name)
        if path is None:
            continue  # External libraries remain the ARTIQ environment's concern.
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        sources[name], trees[name] = path, tree
        components = name.split(".")
        for end in range(1, len(components)):
            parent = ".".join(components[:end])
            if locate(parent) is not None:
                pending.append(parent)
        package = components if path.name == "__init__.py" else components[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                pending.extend(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package):
                        raise ValueError(f"Relative import escapes package in {path}")
                    parent = package[: len(package) - node.level + 1]
                    base = (
                        ".".join([*parent, *node.module.split(".")])
                        if node.module
                        else ".".join(parent)
                    )
                    if node.module and locate(base) is None:
                        raise ValueError(f"Missing local native dependency: {base}")
                else:
                    base = node.module or ""
                if base:
                    pending.append(base)
                    pending.extend(
                        f"{base}.{item.name}" for item in node.names if item.name != "*"
                    )
            elif isinstance(node, ast.Call):
                dynamic = (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__"
                )
                dynamic |= (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "importlib"
                    and node.func.attr == "import_module"
                )
                if dynamic:
                    raise ValueError(
                        "Native package discovery requires static imports; "
                        "use an explicit reviewed sources mapping for dynamic loaders"
                    )
    classes = [
        node
        for node in trees[module].body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(classes) != 1:
        raise ValueError(f"Expected one class {class_name} in native module {module}")
    return sources
