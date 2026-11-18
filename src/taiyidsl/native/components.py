"""Import-free declarations for independently instantiated ARTIQ components."""

from __future__ import annotations

import ast
import copy
import inspect
from collections.abc import Mapping
from dataclasses import dataclass

from ..dsl._context import _builder, _source
from .declarations import (
    NativeKernelPackage,
    _argument,
    _identifier,
    _NativeCall,
    _package_entry,
    _validate_stage,
)


def _host_argument(value):
    """Copy static host configuration without accepting live objects or sweeps."""
    from ..dsl.values import SweepParameter

    if isinstance(value, SweepParameter):
        raise TypeError("Native build arguments cannot contain sweeps")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Native build configuration keys must be strings")
        return {key: _host_argument(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_host_argument(item) for item in value]
    return _argument(value, {})


class NativeModule:
    """One pinned HasEnvironment/EnvExperiment class, instantiated per session.

    Build arguments are static host configuration. Native stage arguments may
    vary by sweep. Lifecycle methods are opt-in: initialize runs once,
    after_shot after every shot, and finalize once at session end.
    """

    @classmethod
    def from_package(
        cls,
        *,
        root,
        entry,
        resources,
        source_overrides=None,
        build_kwargs=None,
        host_prepare=None,
        initialize=None,
        after_shot=None,
        finalize=None,
    ):
        """Pin ``module:Class`` and its local static dependencies without import.

        ``root`` contains the importable top-level package. Explicit
        ``source_overrides`` map module names to user files outside that root;
        ARTIQ and third-party dependencies stay in the separate runtime
        environment. Use the constructor's explicit ``sources`` mapping for
        reviewed code that cannot be discovered through static imports.

        Hooks keep the NativeModule lifecycle: host_prepare and initialize
        once, after_shot per completed shot, and finalize once per session.
        No hook runs while this factory reads and pins the source files.
        """
        from .sources import package_sources

        module, class_name = _package_entry(entry)
        return cls(
            module=module,
            class_name=class_name,
            sources=package_sources(root, module, class_name, source_overrides),
            resources=resources,
            build_kwargs=build_kwargs,
            host_prepare=host_prepare,
            initialize=initialize,
            after_shot=after_shot,
            finalize=finalize,
        )

    def __init__(
        self,
        *,
        module,
        class_name,
        sources,
        resources,
        build_kwargs=None,
        host_prepare=None,
        initialize=None,
        after_shot=None,
        finalize=None,
    ):
        declaration = NativeKernelPackage(
            module=module, class_name=class_name, sources=sources, resources=resources
        ).to_dict()
        for method in (host_prepare, initialize, after_shot, finalize):
            if method is not None:
                _identifier(method, "native lifecycle method")
        arguments = {} if build_kwargs is None else dict(build_kwargs)
        for name in arguments:
            _identifier(name, "native build argument")
        declaration.update(
            build_kwargs={k: _host_argument(v) for k, v in arguments.items()},
            host_prepare=host_prepare,
            initialize=initialize,
            after_shot=after_shot,
            finalize=finalize,
        )
        self._descriptor = declaration

    def to_dict(self):
        return copy.deepcopy(self._descriptor)


@dataclass(frozen=True)
class NativeStage:
    """A named method and its fixed preparation/control/readout boundary."""

    component: str
    method: str
    timing: str
    duration: object = None
    phase_sensitive: bool = False
    end_boundary: str | None = None

    def __post_init__(self):
        _identifier(self.component, "native component")
        _identifier(self.method, "native stage method")
        _validate_stage(
            self.timing, self.duration, self.phase_sensitive, self.end_boundary
        )


def _signature(component, method):
    from .audit import _Audit

    audit = _Audit(component)
    if method not in audit.methods:
        raise ValueError(f"Native method {method!r} is absent from pinned sources")
    _, _, node = audit.functions[audit.methods[method]]
    if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
        raise TypeError("Native stages require a fixed positional method signature")
    parameters = node.args.posonlyargs + node.args.args
    defaults = [inspect.Parameter.empty] * (len(parameters) - len(node.args.defaults))
    for value in node.args.defaults:
        try:
            defaults.append(ast.literal_eval(value))
        except (ValueError, TypeError):
            raise TypeError(
                "Native stage defaults must be literals; move computed defaults "
                "into build or pass an explicit stage argument"
            ) from None
    return inspect.Signature(
        [
            inspect.Parameter(
                parameter.arg,
                inspect.Parameter.POSITIONAL_ONLY
                if index < len(node.args.posonlyargs)
                else inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=defaults[index],
            )
            for index, parameter in enumerate(parameters)
            if index
        ]
    )


class _ProgramCall:
    def __init__(self, descriptor, name, stage, signature):
        self._signature = signature
        self._call = _NativeCall(
            descriptor,
            f"_taiyi_stage_{name}",
            stage.timing,
            stage.duration,
            stage.phase_sensitive,
            stage.end_boundary,
        )

    def __call__(self, *args, **kwargs):
        bound = self._signature.bind(*args, **kwargs)
        bound.apply_defaults()
        self._call(*bound.args)
        # The package proxy has already returned; omit this extra component
        # forwarding frame so provenance still names the user's experiment.
        exp = _builder()
        exp._stack[-1].children[-1].source = _source(skip=(__file__,))


class NativeProgram:
    """Compose independent native components around one compiled control body.

    Component and stage mappings preserve declaration order. Lifecycle defaults
    select each explicitly declared hook in that order; pass an explicit tuple
    to select a subset or order. Shared resources must name all participating
    components. They remain disjoint from generated control hardware.
    """

    def __init__(
        self,
        *,
        modules,
        stages,
        shared_resources=None,
        initialize=None,
        after_shot=None,
        finalize=None,
        host_prepare=None,
    ):
        if not isinstance(modules, Mapping) or not modules:
            raise ValueError("NativeProgram requires named native modules")
        components, sources = {}, {}
        for name, module in modules.items():
            _identifier(name, "native component name")
            if name in {
                "core",
                "signature",
                "initialized",
                "prepared",
                "finalized",
                "initialization_started",
                "finalization_started",
            } or name.startswith("_"):
                raise ValueError(f"Native component name {name!r} is reserved")
            if not isinstance(module, NativeModule):
                raise TypeError("NativeProgram modules must be NativeModule objects")
            component = module.to_dict()
            components[name] = component
            for source in component["sources"]:
                old = sources.get(source["module"])
                if old is not None and old != source:
                    raise ValueError(f"Conflicting native source {source['module']}")
                sources[source["module"]] = source
        sharing = {} if shared_resources is None else dict(shared_resources)
        owners = {}
        for name, component in components.items():
            for resource in component["resources"]:
                owners.setdefault(resource, []).append(name)
        for resource, participants in sharing.items():
            if isinstance(participants, str) or set(participants) != set(
                owners.get(resource, ())
            ):
                raise ValueError(
                    f"Shared resource {resource} must list all its components"
                )
            if len(set(participants)) < 2:
                raise ValueError("Shared resources require at least two components")
            sharing[resource] = list(participants)
        for resource, participants in owners.items():
            if len(participants) > 1 and resource not in sharing:
                raise ValueError(f"Native shared resource {resource} must be declared")
        lifecycle = {}
        for role, requested in (
            ("host_prepare", host_prepare),
            ("initialize", initialize),
            ("after_shot", after_shot),
            ("finalize", finalize),
        ):
            selected = (
                [name for name, item in components.items() if item.get(role)]
                if requested is None
                else list(requested)
            )
            if len(selected) != len(set(selected)) or any(
                name not in components or not components[name].get(role)
                for name in selected
            ):
                raise ValueError(f"Invalid native {role} component order")
            lifecycle[role] = selected
        for resource, participants in sharing.items():
            if len(set(participants) & set(lifecycle["initialize"])) > 1:
                raise ValueError(
                    f"Shared resource {resource} needs one initialization owner"
                )
        if not isinstance(stages, Mapping) or not stages:
            raise ValueError("NativeProgram requires named stages")
        stage_descriptors, signatures = {}, {}
        for name, stage in stages.items():
            _identifier(name, "native stage name")
            if name.startswith("_") or hasattr(type(self), name):
                raise ValueError(f"Native stage name {name!r} is reserved")
            if not isinstance(stage, NativeStage) or stage.component not in components:
                raise ValueError("A native stage must select a declared component")
            signatures[name] = _signature(components[stage.component], stage.method)
            stage_descriptors[name] = {
                "component": stage.component,
                "method": stage.method,
                "timing": stage.timing,
                "arguments": list(signatures[name].parameters),
            }
        first = next(iter(components.values()))
        self._descriptor = {
            "kind": "native_program",
            "schema_version": 2,
            "module": first["module"],
            "class_name": first["class_name"],
            "sources": list(sources.values()),
            "resources": sorted(owners),
            "components": components,
            "stages": stage_descriptors,
            "shared_resources": sharing,
            "lifecycle": lifecycle,
            "build_method": "build",
            "initialize_method": "initialize_native_once",
            "finalize_method": "_taiyi_native_after_shot",
            "session_finalize_method": "finalize_native_session",
            "phase_reference": "control_epoch",
        }
        self._calls = {
            name: _ProgramCall(self.to_dict(), name, stage, signatures[name])
            for name, stage in stages.items()
        }

    def __getattr__(self, name):
        calls = self.__dict__.get("_calls", {})
        if name in calls:
            return calls[name]
        raise AttributeError(name)

    def to_dict(self):
        return copy.deepcopy(self._descriptor)
