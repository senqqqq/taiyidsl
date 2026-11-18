"""Static source for native component instances and their session lifecycle."""

from __future__ import annotations

import ast
import hashlib
import json

from ._generated import GeneratedProgram, NativeBuildPrelude


def component_loader(program, pins, frame_names, legacy_loader):
    """Reuse verified imports, then generate a facade over independent objects."""
    nodes = legacy_loader(program, pins, [])
    signature = hashlib.sha256(
        json.dumps(
            {"program": program, "frame_attributes": frame_names},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    lines = [
        "from artiq.experiment import "
        "HasEnvironment as _NativeEnvironment, kernel, now_mu"
    ]
    for name, component in program["components"].items():
        lines += [
            f"_NativeClass_{name} = getattr(_native_importlib.import_module("
            f"{component['module']!r}), {component['class_name']!r})",
            f"if not issubclass(_NativeClass_{name}, _NativeEnvironment):",
            "    raise TypeError('Native components must inherit HasEnvironment')",
            f"class _NativeComponent_{name}(_NativeClass_{name}):",
            "    def get_argument(self, key, processor, group=None, tooltip=None):",
            f"        return super().get_argument({name + '.'!r} + key, "
            "processor, group, tooltip)",
        ]
    lines += [
        "for _pin in _NATIVE_SOURCE_PINS:",
        "    _module = _native_sys.modules.get(_pin['module'])",
        "    if _module is not None:",
        "        if _NativePath(_module.__file__).resolve() != "
        "_NativePath(_pin['path']).resolve():",
        "            raise RuntimeError('Native component import "
        "differs from its source pin')",
        "        _module.__taiyidsl_source_sha256__ = _pin['sha256']",
        "class _TaiyiNativeContext:",
        "    def __init__(self, core):",
        "        self.core = core",
        f"        self.signature = {signature!r}",
        "        self.initialized = False",
        "        self.initialization_started = False",
        "        self.prepared = False",
        "        self.finalized = False",
        "        self.finalization_started = False",
        "class _NativePackage:",
        "    def build(self, native_context=None):",
        "        self.setattr_device('core')",
        "        if native_context is None:",
        "            native_context = _TaiyiNativeContext(self.core)",
    ]
    context_attributes = ["native_control_t0_mu", "native_physical_base_mu"]
    context_attributes += [
        f"native_terminal_{kind}_{frame}"
        for frame in frame_names
        for kind in ("f", "h", "v")
    ]
    for name, component in program["components"].items():
        kwargs = ", ".join(
            f"{key}={value!r}" for key, value in component["build_kwargs"].items()
        )
        lines += [
            f"            native_context.{name} = _NativeComponent_{name}("
            f"self{', ' if kwargs else ''}{kwargs})",
            f"            component = native_context.{name}",
            "            if hasattr(component, 'core') "
            "and component.core is not self.core:",
            "                raise RuntimeError('Native components "
            "must use the same core')",
            "            component.core = self.core",
        ]
        for attribute in context_attributes:
            initial = "_native_int64(0)" if attribute.endswith("_mu") else "0.0"
            lines += [
                f"            if hasattr(component, {attribute!r}):",
                "                raise RuntimeError("
                f"'Native component reserves {attribute}')",
                f"            component.{attribute} = {initial}",
            ]
    lines += [
        f"        if native_context.signature != {signature!r}:",
        "            raise RuntimeError('Native context belongs "
        "to a different program')",
        "        if native_context.core is not self.core:",
        "            raise RuntimeError('Native context uses "
        "a different core/device manager')",
        "        if native_context.finalized:",
        "            raise RuntimeError('Native context is already finalized')",
        "        if native_context.initialization_started "
        "and not native_context.initialized:",
        "            raise RuntimeError('Native context initialization failed')",
        "        if native_context.finalization_started:",
        "            raise RuntimeError('Native context finalization has started')",
        "        self.native_context = native_context",
    ]
    for name in program["components"]:
        lines.append(f"        self._native_{name} = native_context.{name}")
    invariant_names = {
        "native_context",
        *(f"_native_{name}" for name in program["components"]),
    }
    lines += [
        "        self.kernel_invariants = "
        "getattr(self, 'kernel_invariants', set()) | "
        f"set({tuple(sorted(invariant_names))!r})",
        "    def prepare(self):",
        "        if not self.native_context.prepared:",
    ]
    for name in program["lifecycle"]["host_prepare"]:
        method = program["components"][name]["host_prepare"]
        lines.append(f"            self._native_{name}.{method}()")
    lines += [
        "            self.native_context.prepared = True",
        "    @kernel",
        "    def initialize_native_once(self):",
        "        if self.native_context.finalized:",
        "            raise RuntimeError('Native session is finalized')",
        "        if not self.native_context.initialized:",
        "            if self.native_context.initialization_started:",
        "                raise RuntimeError('Native initialization cannot be retried')",
        "            self.native_context.initialization_started = True",
    ]
    for name in program["lifecycle"]["initialize"]:
        method = program["components"][name]["initialize"]
        lines.append(f"            self._native_{name}.{method}()")
    lines += [
        "            self.native_context.initialized = True",
        "    @kernel",
        "    def _taiyi_native_after_shot(self):",
    ]
    for name in program["lifecycle"]["after_shot"]:
        lines.append(
            f"        self._native_{name}.{program['components'][name]['after_shot']}()"
        )
    if not program["lifecycle"]["after_shot"]:
        lines.append("        pass")
    lines += [
        "    @kernel",
        "    def finalize_native_session(self):",
        "        if self.native_context.initialized "
        "and not self.native_context.finalized:",
        "            if self.native_context.finalization_started:",
        "                raise RuntimeError('Native finalization cannot be retried')",
        "            self.native_context.finalization_started = True",
        "            self.core.break_realtime()",
    ]
    for name in program["lifecycle"]["finalize"]:
        method = program["components"][name]["finalize"]
        lines.append(f"            self._native_{name}.{method}()")
    lines += [
        "            self.core.wait_until_mu(now_mu())",
        "            self.native_context.finalized = True",
    ]
    for name, stage in program["stages"].items():
        arguments = ", ".join(stage["arguments"])
        component = stage["component"]
        lines += [
            "    @kernel",
            f"    def _taiyi_stage_{name}(self{', ' if arguments else ''}{arguments}):",
        ]
        for attribute in context_attributes:
            lines.append(
                f"        self._native_{component}.{attribute} = self.{attribute}"
            )
        lines.append(f"        self._native_{component}.{stage['method']}({arguments})")
    nodes.extend(ast.parse("\n".join(lines) + "\n").body)
    return nodes


def component_resource_check(program):
    """Runtime identity checks include aliases that names alone cannot reveal."""
    lines = [
        "def _taiyi_validate_component_resources(experiment):",
        "    seen = []",
        "    shared = []",
    ]
    shared = program["shared_resources"]
    for resource, participants in shared.items():
        lines.append(
            f"    shared.append(({tuple(participants)!r}, "
            f"_taiyi_native_resource_keys(experiment.get_device({resource!r}))))"
        )
    for name, component in program["components"].items():
        for resource in component["resources"]:
            lines += [
                "    keys = _taiyi_native_resource_keys("
                f"experiment._native_{name}.get_device({resource!r}))",
                "    for prior_component, prior_resource, prior_keys in seen:",
                f"        if prior_component != {name!r}:",
                "            overlap = keys & prior_keys",
                "            for participants, shared_keys in shared:",
                "                if prior_component in participants "
                f"and {name!r} in participants:",
                "                    overlap -= shared_keys",
                "            if overlap:",
                "                raise RuntimeError('Undeclared shared "
                "native hardware alias')",
                f"    seen.append(({name!r}, {resource!r}, keys))",
            ]
    return ast.parse("\n".join(lines) + "\n").body


def isolated_component_loader(program, pins, frame_names, legacy_loader):
    """Keep persistent component classes independent of every chunk namespace.

    The support source is static exported Python, not instrument-provided code.
    A separate namespace prevents method globals from retaining point methods
    and per-chunk tables after the runner releases an old GeneratedExperiment.
    """
    support = ast.Module(
        body=(
            component_loader(program, pins, frame_names, legacy_loader)
            + component_resource_check(program)
        ),
        type_ignores=[],
    )
    ast.fix_missing_locations(support)
    source = ast.unparse(support) + "\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    name = f"_taiyidsl_native_support_{digest}"
    return ast.parse(f"""
import sys as _native_sys
import hashlib as _native_hashlib
import linecache as _native_linecache
from pathlib import Path as _NativePath
from types import ModuleType as _NativeSupportModule
_NATIVE_SUPPORT_NAME = {name!r}
_native_support = _native_sys.modules.get(_NATIVE_SUPPORT_NAME)
if _native_support is None:
    _native_support = _NativeSupportModule(_NATIVE_SUPPORT_NAME)
    _native_sys.modules[_NATIVE_SUPPORT_NAME] = _native_support
    _native_support_file = "<" + _NATIVE_SUPPORT_NAME + ">"
    _native_support_source = {source!r}
    _native_support.__file__ = _native_support_file
    _native_linecache.cache[_native_support_file] = (
        len(_native_support_source), None,
        _native_support_source.splitlines(keepends=True), _native_support_file,
    )
    try:
        exec(compile(_native_support_source, _native_support_file, "exec"),
             _native_support.__dict__)
        _native_support.__taiyi_support_hash__ = {digest!r}
    except BaseException:
        _native_sys.modules.pop(_NATIVE_SUPPORT_NAME, None)
        raise
elif getattr(_native_support, "__taiyi_support_hash__", None) != {digest!r}:
    raise RuntimeError("Native support module differs from the exported program")
for _pin in _native_support._NATIVE_SOURCE_PINS:
    _native_source_hash = _native_hashlib.sha256(
        _NativePath(_pin['path']).read_bytes()).hexdigest()
    if _native_source_hash != _pin['sha256']:
        raise RuntimeError("Native source changed; rebuild the experiment")
    _cached = _native_sys.modules.get(_pin['module'])
    if _cached is not None and (
        _NativePath(_cached.__file__).resolve() != _NativePath(_pin['path']).resolve()
        or getattr(_cached, '__taiyidsl_source_sha256__', None) != _pin['sha256']
    ):
        raise RuntimeError("Unverified or stale native component module")
_NativePackage = _native_support._NativePackage
_native_int64 = _native_support._native_int64
_taiyi_validate_native_resources = _native_support._taiyi_validate_native_resources
_taiyi_validate_component_resources = (
    _native_support._taiyi_validate_component_resources
)
""").body


def configure_component_export(
    program: GeneratedProgram, build_prelude: NativeBuildPrelude
):
    """Add explicit shared-context construction and session-level finalization."""
    methods = program.methods
    build = methods["build"]
    build.args.args.append(ast.arg(arg="native_context"))
    build.args.defaults.append(ast.Constant(value=None))
    build_prelude.use_shared_context()
    methods["_validate_target"].body.append(
        ast.parse("_taiyi_validate_component_resources(self)").body[0]
    )
    run = methods["run"]
    run.body = [
        ast.Try(
            body=run.body,
            handlers=[],
            orelse=[],
            finalbody=ast.parse(
                "if self.native_context.initialized "
                "and not self.native_context.finalized:\n"
                "    self.finalize_native_session()"
            ).body,
        )
    ]
