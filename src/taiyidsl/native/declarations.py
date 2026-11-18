"""Offline declarations of native ARTIQ calls, never live kernel invocation."""

from __future__ import annotations

import copy
import hashlib
import keyword
import math
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from ..dsl._context import _builder, _source
from ..dsl.values import SweepParameter, resolve, validate_value
from ..ir import CompileError
from ..units import Quantity


def _identifier(value, label, *, dotted=False):
    parts = value.split(".") if isinstance(value, str) else []
    if (
        not parts
        or (not dotted and len(parts) != 1)
        or any(not p.isidentifier() or keyword.iskeyword(p) for p in parts)
    ):
        raise ValueError(f"{label} must be a {'dotted ' if dotted else ''}identifier")
    return value


def _package_entry(entry):
    """Parse the shared import-free ``module:Class`` declaration syntax."""
    if not isinstance(entry, str) or entry.count(":") != 1:
        raise ValueError("Native entry must be module:Class")
    module, class_name = entry.split(":")
    return (
        _identifier(module, "module", dotted=True),
        _identifier(class_name, "class_name"),
    )


def _validate_stage(timing, duration, phase_sensitive, end_boundary):
    """Validate timing declarations without constructing a package or call."""
    if timing not in {"prefix", "timed", "suffix"}:
        raise ValueError("Native timing must be prefix, timed or suffix")
    if end_boundary is not None and (
        not isinstance(end_boundary, str) or not end_boundary.strip()
    ):
        raise ValueError("end_boundary must be a nonempty description")
    if type(phase_sensitive) is not bool:
        raise TypeError("phase_sensitive must be boolean")
    if timing == "prefix":
        if duration is not None or phase_sensitive or end_boundary is not None:
            raise ValueError("Native prefix is outside the control phase epoch")
    elif duration is None:
        raise ValueError(f"Native {timing} requires an exact duration")
    else:
        validate_value(duration, "time", "native duration")


class NativeKernelPackage:
    """Pin an external class without importing ARTIQ or executing its code.

    ``sources`` maps import module names to local Python files. Include the
    class's user-code dependencies and package initializers. ``resources`` lists
    physical device and shared-bus names owned by this native program. The first
    version requires these to be disjoint from compiled control resources.

    The class's host ``build(self)`` binds its devices and fixed parameters.
    Optional initialization/finalization hooks are native kernel methods.
    """

    @classmethod
    def from_package(
        cls,
        *,
        root,
        entry,
        resources,
        initialize_method=None,
        finalize_method=None,
        source_overrides=None,
    ):
        """Register ``module:Class`` and its local static dependencies once.

        ``root`` is the directory containing the importable top-level package.
        Sources are read and pinned, never imported or instantiated. Existing
        user files outside that root can be supplied as explicit module/file
        ``source_overrides``. External ARTIQ/third-party packages are not copied.
        Lifecycle semantics are unchanged: initialize once, finalize per shot.
        """
        from .sources import package_sources

        module, class_name = _package_entry(entry)
        return cls(
            module=module,
            class_name=class_name,
            sources=package_sources(root, module, class_name, source_overrides),
            resources=resources,
            initialize_method=initialize_method,
            finalize_method=finalize_method,
        )

    def __init__(
        self,
        *,
        module,
        class_name,
        sources,
        resources,
        initialize_method=None,
        finalize_method=None,
    ):
        _identifier(module, "module", dotted=True)
        _identifier(class_name, "class_name")
        if module not in sources:
            raise ValueError("Native package module must have a pinned source")
        pins = []
        for name, value in sorted(sources.items()):
            _identifier(name, "source module", dotted=True)
            path = Path(value).resolve()
            if path.suffix != ".py" or not path.is_file():
                raise ValueError(f"Native source is not a Python file: {path}")
            pins.append(
                {
                    "module": name,
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        resources = tuple(resources)
        if not resources or len(set(resources)) != len(resources):
            raise ValueError("Native resources must be nonempty and unique")
        for resource in resources:
            _identifier(resource, "native resource")
        for name in (initialize_method, finalize_method):
            if name is not None:
                _identifier(name, "native lifecycle method")
        self._descriptor = {
            "module": module,
            "class_name": class_name,
            "sources": pins,
            "resources": list(resources),
            "build_method": "build",
            "initialize_method": initialize_method,
            "finalize_method": finalize_method,
            "phase_reference": "control_epoch",
        }

    def kernel(
        self, method, *, timing, duration=None, phase_sensitive=False, end_boundary=None
    ):
        """Return a DSL-callable proxy; its invocation never calls the kernel.

        Prefix calls execute before the control epoch and may have variable
        duration. Timed calls form a fixed-duration prelude inside control T0.
        Suffix calls begin at the logical control end, require an exact
        cursor duration and must not reset/break/wait. Native code can consume
        the exported control epoch and terminal frame attributes explicitly.
        """
        _identifier(method, "kernel method")
        _validate_stage(timing, duration, phase_sensitive, end_boundary)
        return _NativeCall(
            self.to_dict(), method, timing, duration, phase_sensitive, end_boundary
        )

    def to_dict(self):
        return copy.deepcopy(self._descriptor)


class _NativeCall:
    def __init__(
        self, package, method, timing, duration, phase_sensitive, end_boundary
    ):
        self._params = {
            "package": package,
            "method": method,
            "timing": timing,
            "duration": duration,
            "phase_sensitive": phase_sensitive,
            "end_boundary": end_boundary,
        }

    def __call__(self, *args):
        exp = _builder()
        if not any(node.kind == "realtime" for node in exp._stack):
            raise ValueError("Native kernel calls must be inside realtime")
        if any(node.kind == "triggered_sequence" for node in exp._stack):
            raise ValueError("Native kernel calls cannot be inside triggered_sequence")
        for arg in args:
            _argument(arg, None)
        exp._append("native_call", **self._params, args=list(args))
        exp._stack[-1].children[-1].source = _source(skip=(__file__,))


def _argument(value, coordinates):
    if isinstance(value, SweepParameter):
        return value if coordinates is None else float(coordinates[value.name])
    if isinstance(value, (Quantity, Fraction)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_argument(item, coordinates) for item in value]
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise TypeError("Native arguments must be finite literals, quantities or sweeps")


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.children)


def split_native_body(body):
    """Keep native provenance in source IR, extract the static control timeline."""
    calls = [node for node in _walk(body) if node.kind == "native_call"]
    if not calls:
        return body, None
    if len(body) != 1 or body[0].kind != "realtime":
        raise CompileError("Native composition requires one enclosing realtime block")
    children = body[0].children
    direct = [node for node in children if node.kind == "native_call"]
    if len(direct) != len(calls):
        raise CompileError(
            "Native calls must be direct realtime children, outside "
            "parallel/repeat/case/serial and triggered_sequence"
        )
    package = calls[0].params["package"]
    if any(node.params["package"] != package for node in calls):
        raise CompileError("One native package per shot is supported")
    prefix, timed, control, suffix = [], [], [], []
    state = "prefix"
    for node in children:
        if node.kind != "native_call":
            if state == "suffix":
                raise CompileError("Compiled operations cannot follow a native suffix")
            if node.kind == "native_delay":
                if state != "timed":
                    raise CompileError(
                        "Native timeline delay must follow a timed prelude"
                    )
            else:
                state = "control"
            control.append(node)
        elif node.params["timing"] == "prefix":
            if state != "prefix":
                raise CompileError("Native prefix must precede all compiled operations")
            prefix.append(node)
        elif node.params["timing"] == "timed":
            if state not in {"prefix", "timed"}:
                raise CompileError(
                    "Native timed calls must precede compiled operations"
                )
            state = "timed"
            control.append(
                replace(
                    node,
                    kind="native_timed",
                    params={
                        "duration": node.params["duration"],
                        "method": node.params["method"],
                        "native_call_index": len(timed),
                        "end_boundary": node.params.get("end_boundary"),
                    },
                )
            )
            timed.append(node)
        else:
            if not control:
                raise CompileError("Native suffix requires a preceding control body")
            state = "suffix"
            suffix.append(node)
    if not any(node.kind not in {"native_timed", "native_delay"} for node in control):
        raise CompileError("Native composition requires a compiled control body")
    from .audit import audit_native_sources

    entries = [{**node.params, "source": node.source} for node in calls]
    audit = audit_native_sources(package, entries)
    return [replace(body[0], children=control)], {
        "schema_version": 1,
        "execution": "single_top_level_kernel",
        "package": copy.deepcopy(package),
        "source_audit": audit,
        "prefix": prefix,
        "timed": timed,
        "suffix": suffix,
    }


def bind_native_point(contract, coordinates, index, setup, logical=()):
    """Bind native arguments and strict suffix durations before source emission."""
    tick = Fraction(str(setup.spectrum.artiq_tick_seconds))
    point = {"index": index, "prefix": [], "timed": [], "suffix": []}
    timed_events = {
        event.params["native_call_index"]: event
        for event in logical
        if event.kind == "native_timed"
    }
    for kind in ("prefix", "timed", "suffix"):
        for call_index, node in enumerate(contract.get(kind, ())):
            p = node.params
            entry = {
                "method": p["method"],
                "args": [_argument(arg, coordinates) for arg in p["args"]],
                "timing": kind,
                "phase_sensitive": p["phase_sensitive"],
                "source": node.source,
            }
            if contract["package"].get("kind") == "native_program":
                name = p["method"].removeprefix("_taiyi_stage_")
                stage = contract["package"]["stages"][name]
                entry.update(
                    stage=name,
                    component=stage["component"],
                    component_method=stage["method"],
                )
            if p.get("end_boundary") is not None:
                entry["end_boundary"] = p["end_boundary"]
            if kind != "prefix":
                duration = resolve(p["duration"], coordinates, "time") / tick
                if duration.denominator != 1 or duration < 0:
                    raise CompileError(
                        f"Native {kind} duration is off RTIO grid: {node.source}"
                    )
                entry["duration_mu"] = int(duration)
            if kind == "timed":
                event = timed_events[call_index]
                start = event.start_mu * Fraction(str(setup.tick_seconds)) / tick
                if start.denominator != 1:
                    raise CompileError("Native timed start is off RTIO grid")
                entry["start_mu_from_T0"] = int(start)
            point[kind].append(entry)
    return point


def validate_native_timed_window(point, setup, physical):
    """Opaque native preludes cannot overlap generated preparation or triggers."""
    if not point["timed"]:
        return
    tick = Fraction(str(setup.tick_seconds))
    native_tick = Fraction(str(setup.spectrum.artiq_tick_seconds))
    end = max(c["start_mu_from_T0"] + c["duration_mu"] for c in point["timed"])
    threshold = end * native_tick
    for event in physical.events:
        if event.kind == "spectrum_program":
            start = event.params["trigger_relative_mu"] * native_tick
        else:
            start = (event.start_mu - physical.origin_mu) * tick
        if start < threshold:
            raise CompileError(
                "Generated output/trigger overlaps the strict native preparation tail; "
                "the preparation-to-gate gap must cover trigger lead and preparation. "
                "No automatic gap or trigger shift is allowed. "
                f"At {event.source}"
            )


def validate_native_resources(contract, setup, physical):
    """Opaque native calls cannot share outputs/buses with compiled control."""
    used = set()
    for event in physical.events:
        if event.kind == "spectrum_program":
            used.update(
                event.params.get("trigger_devices", {}).values()
                or [event.params["trigger_device"]]
            )
        else:
            used.add(event.signal)
            used.update(event.resources)
    for name in tuple(used):
        device = setup.devices.get(name)
        if device is not None:
            used.update(value for value in (device.bus, device.update_group) if value)
    overlap = used.intersection(contract["package"]["resources"])
    if overlap:
        raise CompileError(
            "Native and compiled control resources must be disjoint: "
            + ", ".join(sorted(overlap))
        )
