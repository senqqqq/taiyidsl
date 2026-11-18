"""Conservative, import-free checks for explicitly pinned native ARTIQ sources.

This is a boundary audit, not an ARTIQ compiler or a proof of driver timing.
Only the supplied Python files are read; their module-level code is never run.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..ir import CompileError

_ARTIQ_PRIMITIVES = {
    "at_mu",
    "delay",
    "delay_mu",
    "now_mu",
    "int32",
    "int64",
    "uint32",
    "uint64",
    "float32",
    "float64",
}
_ARTIQ_DECORATORS = {"kernel", "portable", "rpc", "host_only", "subkernel"}
_BUILTINS = {
    "abs",
    "bool",
    "enumerate",
    "float",
    "int",
    "len",
    "list",
    "max",
    "min",
    "range",
    "round",
    "tuple",
    "zip",
    "ValueError",
    "RuntimeError",
    "AssertionError",
    "IndexError",
    "ZeroDivisionError",
}
_NUMPY_FUNCTIONS = {
    "int32",
    "int64",
    "uint32",
    "uint64",
    "float32",
    "float64",
    "sin",
    "cos",
    "tan",
    "sqrt",
    "exp",
    "log",
    "floor",
    "ceil",
    "abs",
    "sign",
}
_MATH_FUNCTIONS = {
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "atan2",
    "sqrt",
    "exp",
    "log",
    "log10",
    "floor",
    "ceil",
    "fabs",
    "fmod",
    "copysign",
    "isfinite",
}
_CORE_METHODS = {
    "reset",
    "break_realtime",
    "wait_until_mu",
    "get_rtio_counter_mu",
    "seconds_to_mu",
    "mu_to_seconds",
}
_UNSAFE_CALLS = {
    "print",
    "input",
    "eval",
    "exec",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "__import__",
    "open",
}
_STRICT_TIMINGS = {"timed", "suffix"}


def _path(node):
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = _path(node.value)
        return (*prefix, node.attr) if prefix else None
    return None


def _fail(message, module=None, node=None):
    location = f" at {module.path}:{getattr(node, 'lineno', 1)}" if module else ""
    raise CompileError(f"Native ARTIQ source audit: {message}{location}")


@dataclass
class _Module:
    name: str
    path: Path
    sha256: str
    tree: ast.Module
    imports: dict
    stars: tuple
    functions: dict
    classes: dict
    assigned: set

    def resolve(self, name):
        if name in self.functions or name in self.classes:
            return f"{self.name}.{name}"
        if name in self.assigned:
            return None
        if name in self.imports:
            return self.imports[name]
        if name in _ARTIQ_PRIMITIVES | _ARTIQ_DECORATORS | {
            "EnvExperiment",
            "HasEnvironment",
        }:
            if any(star.startswith("artiq.") for star in self.stars):
                return f"artiq.experiment.{name}"
        if name in _MATH_FUNCTIONS and "math" in self.stars:
            return f"math.{name}"
        if name in _NUMPY_FUNCTIONS and "numpy" in self.stars:
            return f"numpy.{name}"
        return None


def _read_modules(package):
    modules = {}
    for source in package.get("sources", []):
        name = source.get("module")
        if not isinstance(name, str) or not all(
            part.isidentifier() for part in name.split(".")
        ):
            _fail("Each pinned source needs a valid module name")
        if name in modules:
            _fail(f"Duplicate pinned module {name}")
        expected = source.get("sha256", "")
        if not isinstance(expected, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", expected
        ):
            _fail(f"Pinned source {name} needs a SHA-256 digest")
        try:
            path = Path(source["path"]).resolve()
            raw = path.read_bytes()
        except (KeyError, TypeError, OSError) as exc:
            _fail(f"Cannot read pinned source {name}: {exc}")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected.lower():
            _fail(f"Pinned source changed: {name} ({path})")
        try:
            tree = ast.parse(raw, filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            _fail(f"Cannot parse pinned source {name}: {exc}")
        imports, stars, functions, classes, assigned = {}, [], {}, {}, set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for item in node.names:
                    imports[item.asname or item.name.split(".")[0]] = (
                        item.name if item.asname else item.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    parent = (
                        name if path.name == "__init__.py" else name.rpartition(".")[0]
                    )
                    parents = parent.split(".") if parent else []
                    if node.level > len(parents):
                        _fail("Relative import escapes pinned package", None, node)
                    target = ".".join(
                        [*parents[: len(parents) - node.level + 1], target]
                    ).rstrip(".")
                for item in node.names:
                    if item.name == "*":
                        stars.append(target)
                    else:
                        imports[item.asname or item.name] = f"{target}.{item.name}"
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                classes[node.name] = node
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    assigned.update(
                        item.id
                        for item in ast.walk(target)
                        if isinstance(item, ast.Name)
                    )
        modules[name] = _Module(
            name,
            path,
            digest,
            tree,
            imports,
            tuple(stars),
            functions,
            classes,
            assigned,
        )
    if not modules:
        _fail("At least one pinned source is required")
    return modules


class _Audit:
    def __init__(self, package):
        self.package = package
        self.modules = _read_modules(package)
        self.classes = {}
        self.functions = {}
        for module in self.modules.values():
            for name, node in module.classes.items():
                self.classes[f"{module.name}.{name}"] = (module, node)
            for name, node in module.functions.items():
                self.functions[f"{module.name}.{name}"] = (module, None, node)
        self.target = f"{package.get('module')}.{package.get('class_name')}"
        if self.target not in self.classes:
            _fail(f"Declared native class {self.target} is not in pinned sources")
        self._mros = {}
        self.mro = self._mro(self.target, set())
        self.methods = {}
        for owner in reversed(self.mro):
            if owner not in self.classes:
                continue
            module, node = self.classes[owner]
            for method in node.body:
                if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                    key = f"{owner}.{method.name}"
                    self.methods[method.name] = key
                    self.functions[key] = (module, owner, method)
        self.devices = self._device_bindings()
        self.visited, self.active = set(), set()
        self.graph, self.driver_calls = {}, set()

    def _qualified(self, module, node):
        path = _path(node)
        if not path:
            return None
        root = module.resolve(path[0])
        return ".".join((root, *path[1:])) if root else None

    def _mro(self, name, active):
        if name in self._mros:
            return self._mros[name]
        if name in active:
            _fail(f"Cyclic native class inheritance at {name}")
        if name not in self.classes:
            if (
                name == "object"
                or name.startswith("artiq.")
                and name.rsplit(".", 1)[-1] in {"EnvExperiment", "HasEnvironment"}
            ):
                return [name]
            _fail(f"Unpinned native base class {name}")
        module, node = self.classes[name]
        bases = []
        for base in node.bases:
            resolved = self._qualified(module, base)
            if isinstance(base, ast.Name) and base.id == "object":
                resolved = "object"
            if resolved is None:
                _fail("Native base class cannot be resolved statically", module, base)
            bases.append(resolved)
        seqs = [list(self._mro(base, active | {name})) for base in bases]
        seqs.append(bases[:])
        result = [name]
        while any(seqs):
            seqs = [seq for seq in seqs if seq]
            candidate = next(
                (seq[0] for seq in seqs if not any(seq[0] in s[1:] for s in seqs)),
                None,
            )
            if candidate is None:
                _fail(f"Inconsistent native class inheritance at {name}")
            result.append(candidate)
            for seq in seqs:
                if seq[0] == candidate:
                    seq.pop(0)
        self._mros[name] = result
        return result

    def _device_bindings(self):
        bindings = {"core": "core"}
        aliases = []
        for owner in self.mro:
            if owner not in self.classes:
                continue
            module, cls = self.classes[owner]
            for node in ast.walk(cls):
                if isinstance(node, ast.Call) and _path(node.func) == (
                    "self",
                    "setattr_device",
                ):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        name = node.args[0].value
                        if isinstance(name, str):
                            bindings[name] = name
                if not isinstance(node, ast.Assign | ast.AnnAssign):
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    dest = _path(target)
                    if not dest or len(dest) != 2 or dest[0] != "self":
                        continue
                    value = node.value
                    if isinstance(value, ast.Call) and _path(value.func) == (
                        "self",
                        "get_device",
                    ):
                        if value.args and isinstance(value.args[0], ast.Constant):
                            device = value.args[0].value
                            if isinstance(device, str):
                                bindings[dest[1]] = device
                    elif _path(value) and _path(value)[0] == "self":
                        aliases.append((dest[1], _path(value)[1:]))
        for _ in aliases:
            for alias, target in aliases:
                if target and target[0] in bindings:
                    bindings[alias] = bindings[target[0]]
        return bindings

    def _decorators(self, module, node):
        result = set()
        for decorator in node.decorator_list:
            value = decorator.func if isinstance(decorator, ast.Call) else decorator
            qualified = self._qualified(module, value)
            if qualified and qualified.startswith("artiq."):
                result.add(qualified.rsplit(".", 1)[-1])
            else:
                _fail("Unresolved or non-ARTIQ native decorator", module, decorator)
        if result.intersection({"rpc", "host_only", "subkernel"}):
            _fail("RPC, host-only and subkernel calls are unsupported", module, node)
        if not result.intersection({"kernel", "portable"}):
            _fail(
                "Native helper must be @kernel or @portable, not a host RPC",
                module,
                node,
            )
        return sorted(result)

    def _method(self, name, module, node, after=None):
        if after is None:
            key = self.methods.get(name)
        else:
            key = None
            start = self.mro.index(after) + 1
            for owner in self.mro[start:]:
                if f"{owner}.{name}" in self.functions:
                    key = f"{owner}.{name}"
                    break
        if key is None:
            _fail(f"Unresolved native self helper {name}", module, node)
        return key

    def check_entry(self, call, timing):
        name = call.get("method")
        if name not in self.methods:
            _fail(f"Native entry method {name!r} is not in the pinned class")
        key = self.methods[name]
        module, _, node = self.functions[key]
        args = call.get("args", [])
        required = (
            len(node.args.posonlyargs + node.args.args) - 1 - len(node.args.defaults)
        )
        maximum = len(node.args.posonlyargs + node.args.args) - 1
        if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            _fail("Native entry requires a fixed positional signature", module, node)
        if not required <= len(args) <= maximum:
            _fail(
                f"Native entry {name} argument count does not match its source",
                module,
                node,
            )
        self.visit(key, timing)
        return {"method": name, "timing": timing, "implementation": key}

    def visit(self, key, timing):
        marker = key, timing
        if marker in self.active:
            _fail(f"Recursive native helper {key} is unsupported")
        if marker in self.visited:
            return
        module, owner, node = self.functions[key]
        if isinstance(node, ast.AsyncFunctionDef):
            _fail("Async native methods are unsupported", module, node)
        decorators = self._decorators(module, node)
        self.active.add(marker)
        row = self.graph.setdefault(
            key,
            {
                "source": f"{module.path}:{node.lineno}",
                "decorators": decorators,
                "timing_roles": [],
                "calls": [],
            },
        )
        row["timing_roles"] = sorted(set(row["timing_roles"]) | {timing})
        locals_ = {
            argument.arg
            for argument in node.args.posonlyargs
            + node.args.args
            + node.args.kwonlyargs
        }
        aliases = {}
        body_nodes = [item for statement in node.body for item in ast.walk(statement)]
        for item in body_nodes:
            if timing in _STRICT_TIMINGS and isinstance(item, ast.While):
                _fail(
                    f"while loops are unsupported in a strict native {timing}",
                    module,
                    item,
                )
            if isinstance(
                item, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef
            ):
                _fail("Nested native functions/classes are unsupported", module, item)
            if isinstance(
                item, ast.Import | ast.ImportFrom | ast.Yield | ast.YieldFrom
            ):
                _fail("Runtime imports and generators are unsupported", module, item)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                locals_.add(item.id)
            if isinstance(item, ast.Assign):
                source = _path(item.value)
                for target in item.targets:
                    if isinstance(target, ast.Name) and source:
                        aliases[target.id] = source
        for item in body_nodes:
            if not isinstance(item, ast.Call):
                continue
            child = self._check_call(module, owner, item, timing, locals_, aliases)
            if child:
                row["calls"] = sorted(set(row["calls"]) | {child})
                self.visit(child, timing)
        self.active.remove(marker)
        self.visited.add(marker)

    def _check_call(self, module, owner, node, timing, locals_, aliases):
        path = _path(node.func)
        if path:
            seen = set()
            while path[0] in aliases and path[0] not in seen:
                seen.add(path[0])
                path = (*aliases[path[0]], *path[1:])
        if isinstance(node.func, ast.Attribute) and isinstance(
            node.func.value, ast.Call
        ):
            value = node.func.value
            if isinstance(value.func, ast.Name) and value.func.id == "super":
                if value.args or value.keywords or owner is None:
                    _fail(
                        "Only zero-argument native super() is supported", module, node
                    )
                return self._method(node.func.attr, module, node, after=owner)
        if path is None:
            _fail("Dynamic native call cannot be audited", module, node)
        if path[-1] == "reset" and timing != "initialize":
            _fail(
                "reset is only allowed in initialization, not native shot stages",
                module,
                node,
            )
        if timing in _STRICT_TIMINGS and path[-1] in {
            "break_realtime",
            "wait_until_mu",
            "get_rtio_counter_mu",
        }:
            _fail(f"{path[-1]} is forbidden in a strict native {timing}", module, node)
        if path[0] in _UNSAFE_CALLS:
            _fail(f"Host/dynamic call {path[0]} is unsupported", module, node)
        if path == ("super",):
            return None  # The containing super().method() is checked separately.
        if path[0] == "self":
            if len(path) == 2:
                return self._method(path[1], module, node)
            if len(path) < 3 or path[1] not in self.devices:
                _fail("Undeclared native device or dynamic object call", module, node)
            device = self.devices[path[1]]
            if device == "core":
                if len(path) != 3 or path[2] not in _CORE_METHODS:
                    _fail("Unsupported core call in native stage", module, node)
            elif device not in self.package.get("resources", []):
                _fail(
                    f"Native device {device!r} is missing from package resources",
                    module,
                    node,
                )
            self.driver_calls.add(".".join(path))
            return None
        qualified = self._qualified(module, node.func)
        if len(path) == 1:
            name = path[0]
            if name in locals_:
                _fail(f"Dynamic local callable {name} is unsupported", module, node)
            if name in _BUILTINS and name not in (
                module.assigned
                | module.imports.keys()
                | module.functions.keys()
                | module.classes.keys()
            ):
                return None
        if qualified in self.functions:
            return qualified
        if qualified:
            prefix, _, name = qualified.rpartition(".")
            if prefix in self.classes and qualified in self.functions:
                return qualified
            if qualified.startswith("artiq.") and name in _ARTIQ_PRIMITIVES:
                return None
            if prefix == "math" and name in _MATH_FUNCTIONS:
                return None
            if prefix == "numpy" and name in _NUMPY_FUNCTIONS:
                return None
        _fail(f"Unresolved or unpinned native call {'.'.join(path)}", module, node)


def audit_native_sources(package: dict, calls: list[dict]) -> dict:
    """Validate source pins and native control boundaries without importing code.

    ``prefix`` permits runtime cursor recovery. ``timed`` and ``suffix`` reject
    reset, break and blocking waits through the pinned helper call graph. A timed
    cooling entry does not make an earlier MOT prefix strict: timing roles are
    audited independently, even when they share helper methods. Durations remain
    declared contracts, verified by emitted runtime assertions and hardware tests.
    """
    if package.get("kind") == "native_program":
        return _audit_program(package, calls)
    audit = _Audit(package)
    entries = []
    initialize = package.get("initialize_method")
    if initialize:
        entries.append(
            audit.check_entry({"method": initialize, "args": []}, "initialize")
        )
    finalize = package.get("finalize_method")
    if finalize:
        entries.append(audit.check_entry({"method": finalize, "args": []}, "finalize"))
    for call in calls:
        timing = call.get("timing")
        if timing not in {"prefix", "timed", "suffix"}:
            _fail(f"Unsupported native call timing {timing!r}")
        entries.append(audit.check_entry(call, timing))
    return {
        "status": "passed",
        "sources": [
            {"module": module.name, "path": str(module.path), "sha256": module.sha256}
            for module in audit.modules.values()
        ],
        "entries": entries,
        "methods": audit.graph,
        "driver_calls": sorted(audit.driver_calls),
        "device_bindings": dict(sorted(audit.devices.items())),
        "limitations": [
            "Pinned source AST audit; no module was imported or executed.",
            "Not a Python sandbox, duration proof, or ARTIQ compiler validation.",
            "A cursor-duration assertion does not prove every internal "
            "output position.",
            "Driver internals, device state, slack and analog settling "
            "require separate verification.",
            "Declared timed/suffix durations require runtime cursor assertions "
            "and physical acceptance.",
        ],
    }


def _audit_program(program, calls):
    """Audit each component on its own self; no generated dynamic dispatch."""
    reports, combined = {}, {}
    for name, component in program["components"].items():
        audit = _Audit(component)
        if not any(
            owner.startswith("artiq.")
            and owner.rsplit(".", 1)[-1] in {"HasEnvironment", "EnvExperiment"}
            for owner in audit.mro
        ):
            _fail(
                f"Native component {name} must inherit HasEnvironment or EnvExperiment"
            )
        if "__init__" in audit.methods:
            _fail(f"Native component {name} must use HasEnvironment construction")
        entries = []
        for role in ("initialize", "after_shot", "finalize"):
            if name in program["lifecycle"][role]:
                entries.append(
                    audit.check_entry(
                        {"method": component[role], "args": []},
                        "initialize_component" if role == "initialize" else role,
                    )
                )
        for call in calls:
            public_name = call["method"].removeprefix("_taiyi_stage_")
            stage = program["stages"].get(public_name)
            if stage is None:
                _fail(f"Unknown native stage {public_name}")
            if stage["component"] != name:
                continue
            if stage["timing"] != call["timing"]:
                _fail(f"Native stage timing changed for {public_name}")
            entry = audit.check_entry(
                {**call, "method": stage["method"]}, call["timing"]
            )
            entries.append({**entry, "stage": public_name, "component": name})
        reports[name] = {
            "status": "passed",
            "entries": entries,
            "methods": audit.graph,
            "driver_calls": sorted(audit.driver_calls),
            "device_bindings": dict(sorted(audit.devices.items())),
        }
        combined.update({f"{name}:{key}": value for key, value in audit.graph.items()})
    return {
        "status": "passed",
        "sources": program["sources"],
        "components": reports,
        "methods": combined,
        "entries": [entry for value in reports.values() for entry in value["entries"]],
        "driver_calls": sorted(
            {call for r in reports.values() for call in r["driver_calls"]}
        ),
        "limitations": [
            "Pinned component ASTs only; no user module imported or executed.",
            "Build and explicitly selected host_prepare are trusted host code.",
            "Cursor duration is checked at runtime, not proven by source audit.",
            "Shared native resources require explicit initialization ownership.",
        ],
    }
