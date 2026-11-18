"""Compose declared native ARTIQ routines around an already verified control body.

This module is only used by the opt-in hybrid native integration. It neither
imports the native package during export nor changes the standalone exporter.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import keyword
import math
from pathlib import Path

from taiyidsl.backends.artiq._contracts import ExportError, _devices, _point_end
from taiyidsl.backends.artiq_spectrum.backend import native_factor

from ._generated import GeneratedProgram, NativeBuildPrelude


def _identifier(value, label):
    if (
        not isinstance(value, str)
        or not value.isidentifier()
        or keyword.iskeyword(value)
    ):
        raise ExportError(f"{label} must be a Python identifier")
    return value


def _statement(source):
    return ast.parse(source).body[0]


def _is_statement(node, source):
    return ast.dump(node, include_attributes=False) == ast.dump(
        _statement(source), include_attributes=False
    )


def _call(method, arguments=()):
    _identifier(method, "Native method")
    # JSON round-trip rejects live callables and copies caller-owned containers.
    values = json.loads(json.dumps(arguments, allow_nan=False))
    return _statement(f"self.{method}({', '.join(repr(v) for v in values)})")


def _terminal_frames(compiled, point):
    """Use the one resolved frame; retain old attribute inputs only as aliases.

    Native routines should consume native_terminal_f_<uid>. Older routines that
    subtract native_terminal_v from native_terminal_h still obtain F because
    those compatibility attributes are derived as F+V and V, respectively.
    V is resettable source diagnostics, never a second live frame to subtract.
    """
    phases = compiled.frame_phases(point.index)
    virtual = compiled.virtual_z_frames(point.index)
    frames = {}
    for uid, value in phases.items():
        phase, source_z = float(value), float(virtual.get(uid, 0.0))
        frame = {
            "frame_phase": phase,
            "effective_phase": phase,
            "virtual_z": source_z,
            "hardware_phase": phase + source_z,
        }
        if not all(math.isfinite(value) for value in frame.values()):
            raise ExportError("Native terminal frame must be finite")
        frames[uid] = frame
    return frames


def _frame_suffix(frame):
    """Preserve old attribute names, encode other legal DSL identifiers safely."""
    if not isinstance(frame, str) or not frame:
        raise ExportError("Native frame identifier must be nonempty text")
    prefix = "__taiyi_frame_"
    if (
        frame.isidentifier()
        and not keyword.iskeyword(frame)
        and not frame.startswith(prefix)
    ):
        return frame
    return prefix + frame.encode("utf-8").hex()


def _pinned_sources(package):
    entries = []
    for item in package["sources"]:
        module = item["module"]
        if not isinstance(module, str) or any(
            not part.isidentifier() or keyword.iskeyword(part)
            for part in module.split(".")
        ):
            raise ExportError("Native source module is invalid")
        path = Path(item["path"]).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ExportError(f"Native source hash changed: {module}")
        entries.append({"module": module, "path": str(path), "sha256": digest})
    if len({item["module"] for item in entries}) != len(entries):
        raise ExportError("Native source modules must be unique")
    if package["module"] not in {item["module"] for item in entries}:
        raise ExportError("Native package entry module must be pinned")
    return entries


def _timed_prelude(declaration, point, logical_end):
    """Validate opaque leading calls against every generated output deadline.

    The current composition submits the compiled body after these native calls.
    An output that precedes their return would require interleaving opaque native
    operations; rejecting that case preserves the requested timing and avoids
    inventing either a padding gap or a backwards output submission contract.
    """
    result = []
    cursor = 0
    for index, call in enumerate(declaration.get("timed", ())):
        start = call.get("start_mu_from_T0")
        duration = call.get("duration_mu")
        if type(start) is not int or start < cursor or (index == 0 and start != 0):
            raise ExportError(
                "Strict native calls must begin at control T0 and be ordered "
                "without overlap"
            )
        if type(duration) is not int or duration < 0:
            raise ExportError("Strict native duration must be nonnegative native mu")
        if type(call.get("phase_sensitive")) is not bool:
            raise ExportError("Strict native calls must declare phase sensitivity")
        cursor = start + duration
        if point.origin_mu + cursor > logical_end:
            raise ExportError("Strict native call exceeds the logical control window")
        result.append(
            {
                **call,
                "start_mu_from_B": point.origin_mu + start,
                "end_mu_from_B": point.origin_mu + cursor,
            }
        )
    if result:
        earliest = min(
            point.physical_events,
            key=lambda event: event.start_mu,
            default=None,
        )
        if earliest is not None and earliest.start_mu < point.origin_mu + cursor:
            raise ExportError(
                "Generated output/trigger precedes the strict native return: "
                f"{earliest.start_mu - point.origin_mu} < {cursor} native mu "
                f"from T0 at {earliest.source}. The requested gap must cover "
                "trigger/preparation lead; opaque native interleaving is unsupported."
            )
    return result


def _loader(package, pins, reserved):
    """Verify pinned files and resolved import origins before importing user code."""
    return ast.parse(
        f"""
import hashlib as _native_hashlib
import importlib as _native_importlib
import sys as _native_sys
from importlib.machinery import PathFinder as _NativePathFinder
from numbers import Integral as _NativeIntegral
from pathlib import Path as _NativePath
from numpy import int64 as _native_int64

_NATIVE_SOURCE_PINS = {pins!r}
for _pin in _NATIVE_SOURCE_PINS:
    _native_path = _NativePath(_pin["path"]).resolve()
    if _native_hashlib.sha256(_native_path.read_bytes()).hexdigest() != _pin["sha256"]:
        raise RuntimeError(
            "Native source changed; rebuild the experiment: " + _pin["module"]
        )
    _native_search = None
    _native_parts = _pin["module"].split(".")
    for _native_index in range(len(_native_parts)):
        _native_fullname = ".".join(_native_parts[:_native_index + 1])
        _native_spec = _NativePathFinder.find_spec(_native_fullname, _native_search)
        if _native_spec is None:
            raise ImportError("Native package is not importable: " + _native_fullname)
        _native_search = _native_spec.submodule_search_locations
    if (
        _native_spec.origin is None
        or _NativePath(_native_spec.origin).resolve() != _native_path
    ):
        raise RuntimeError(
            "Native import resolves to a different source: " + _pin["module"]
        )
    _native_cached = _native_sys.modules.get(_pin["module"])
    if _native_cached is not None and (
        getattr(_native_cached, "__file__", None) is None
        or _NativePath(_native_cached.__file__).resolve() != _native_path
        or getattr(_native_cached, "__taiyidsl_source_sha256__", None)
        != _pin["sha256"]
    ):
        raise RuntimeError(
            "Unverified or stale native module cache; restart the worker: "
            + _pin["module"]
        )

_NativePackage = getattr(
    _native_importlib.import_module({package["module"]!r}),
    {package["class_name"]!r},
)
for _pin in _NATIVE_SOURCE_PINS:
    _native_imported = _native_sys.modules.get(_pin["module"])
    if _native_imported is not None:
        if (
            getattr(_native_imported, "__file__", None) is None
            or _NativePath(_native_imported.__file__).resolve()
            != _NativePath(_pin["path"]).resolve()
        ):
            raise RuntimeError("Native imported source differs from its pin")
        _native_imported.__taiyidsl_source_sha256__ = _pin["sha256"]
if not isinstance(_NativePackage, type):
    raise TypeError("Native package entry must be a class")
for _native_reserved in {reserved!r}:
    if hasattr(_NativePackage, _native_reserved):
        raise RuntimeError(
            "Native method conflicts with generated control: " + _native_reserved
        )

def _taiyi_native_resource_keys(device):
    pending, visited, keys = [device], set(), set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)
        keys.add(("driver", identity))
        channel = getattr(current, "channel", None)
        if isinstance(channel, _NativeIntegral):
            keys.add(("rtio_channel", int(channel)))
        for attribute in ("bus", "cpld", "io_update", "sw"):
            related = getattr(current, attribute, None)
            if related is not None and not isinstance(related, (str, int, float, bool)):
                pending.append(related)
    return keys

def _taiyi_validate_native_resources(experiment, native_names, control_names):
    control = {{name: _taiyi_native_resource_keys(experiment.get_device(name))
               for name in control_names}}
    for native_name in native_names:
        native_keys = _taiyi_native_resource_keys(experiment.get_device(native_name))
        for control_name, control_keys in control.items():
            if native_keys & control_keys:
                raise RuntimeError(
                    "Native/control hardware resource alias: "
                    + native_name + " / " + control_name
                )

def _taiyi_check_native_build(experiment, generated_attributes):
    for attribute in generated_attributes:
        if attribute in vars(experiment):
            raise RuntimeError(
                "Native build attribute conflicts with generated control: "
                + attribute
            )
"""
    ).body


def compose_native_source(compiled, native, source, epochs):
    """Return one-device-entry native composition plus its explicit timing contract.

    ``source`` must be the verified, epoch-aligned unrolled ARTIQ subplan. Prefix
    calls precede the control epoch. The suffix starts at the logical control end,
    while final draining also covers the physical AWG tail. Native source auditing
    and resource ownership are validated by the frontend contract.
    """
    compiled.assert_integrity()
    native.assert_integrity()
    contract = copy.deepcopy(compiled.metadata["native_artiq"])
    if contract.get("schema_version") != 1:
        raise ExportError("Unsupported native ARTIQ contract version")
    package = contract["package"]
    component_program = package.get("kind") == "native_program"
    if package.get("phase_reference") != "control_epoch":
        raise ExportError("Native composition requires an explicit control epoch")
    _identifier(package["class_name"], "Native class")
    build_method = _identifier(package.get("build_method", "build"), "Native build")
    initialize_method = package.get("initialize_method")
    finalize_method = package.get("finalize_method")
    for method in (initialize_method, finalize_method):
        if method is not None:
            _identifier(method, "Native lifecycle method")
    pins = _pinned_sources(package)
    declared = {point["index"]: point for point in contract["points"]}
    if len(declared) != len(contract["points"]) or set(declared) != {
        point.index for point in native.points
    }:
        raise ExportError("Native composition points do not match the control plan")
    program = GeneratedProgram.parse(source)
    tree, generated, methods = program.tree, program.experiment, program.methods
    reserved = sorted(set(methods) - {"build", "run"})
    frame_names = sorted(
        {
            name
            for point in compiled.points
            for name in _terminal_frames(compiled, point)
        }
    )
    frame_suffixes = {name: _frame_suffix(name) for name in frame_names}
    for name in frame_names:
        reserved.extend(
            (
                f"native_terminal_f_{frame_suffixes[name]}",
                f"native_terminal_h_{frame_suffixes[name]}",
                f"native_terminal_v_{frame_suffixes[name]}",
            )
        )
    reserved.extend(("native_control_t0_mu", "native_physical_base_mu"))
    reserved.extend(f"_taiyi_body_{point.index}" for point in native.points)
    generated.bases.insert(0, ast.Name(id="_NativePackage", ctx=ast.Load()))
    methods["_validate_target"].body.append(
        _statement(
            "_taiyi_validate_native_resources(self, "
            f"{tuple(package['resources'])!r}, {tuple(sorted(_devices(native)))!r})"
        )
    )
    build = methods["build"]
    build_attributes = {
        node.attr
        for node in ast.walk(build)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr != "core"
    }
    build_attributes.update(methods)
    build_attributes.update(reserved)
    build_prelude = NativeBuildPrelude.install(build, build_method, build_attributes)
    build.body.extend(
        [
            _statement("self.native_control_t0_mu = _native_int64(0)"),
            _statement("self.native_physical_base_mu = _native_int64(0)"),
        ]
    )
    for name in frame_names:
        build.body.extend(
            [
                _statement(f"self.native_terminal_f_{frame_suffixes[name]} = 0.0"),
                _statement(f"self.native_terminal_h_{frame_suffixes[name]} = 0.0"),
                _statement(f"self.native_terminal_v_{frame_suffixes[name]} = 0.0"),
            ]
        )
    if initialize_method is not None:
        initializer = methods["initialize_devices"]
        initializer.body.extend(
            [_call(initialize_method), _statement("self.core.wait_until_mu(now_mu())")]
        )
    report_points = []
    for point in native.points:
        method = methods.get(f"point_{point.index}")
        if method is None or len(method.body) < 5:
            raise ExportError("Unknown generated point kernel structure")
        if not _is_statement(method.body[0], "self.core.reset()") or not _is_statement(
            method.body[-1], "self.core.wait_until_mu(now_mu())"
        ):
            raise ExportError("Unknown generated point reset/wait boundaries")
        epoch = method.body[1]
        if not (
            isinstance(epoch, ast.Assign)
            and len(epoch.targets) == 1
            and isinstance(epoch.targets[0], ast.Name)
            and epoch.targets[0].id == "t0"
        ):
            raise ExportError(
                "Native composition does not support per-shot DDS/RAM preload; "
                "keep the control body free of implicit preparation"
            )
        grid, offset = epochs[point.index]["grid_mu"], epochs[point.index]["offset_mu"]
        expected_epoch = (
            f"t0 = ((now_mu() + {grid - 1 - offset}) // {grid}) * {grid} + {offset}"
        )
        if not _is_statement(epoch, expected_epoch):
            raise ExportError("Native control epoch differs from the certified grid")
        body = copy.deepcopy(method)
        body.name = f"_taiyi_body_{point.index}"
        body.args.args.append(ast.arg(arg="t0"))
        body.body = body.body[2:-1]
        if not _is_statement(body.body[0], "at_mu(t0)"):
            raise ExportError("Unknown generated body origin")
        for node in ast.walk(ast.Module(body=body.body, type_ignores=[])):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"reset", "break_realtime", "wait_until_mu"}:
                    raise ExportError(
                        "Control body contains an implicit timing boundary"
                    )
        generated.body.append(body)
        declaration = declared[point.index]
        original_point = compiled.points[point.index]
        factor = native_factor(compiled.setup)
        if original_point.duration_mu % factor:
            raise ExportError("Native control end is not on the ARTIQ mu grid")
        logical_end = int(point.origin_mu + original_point.duration_mu // factor)
        physical_end = _point_end(point)
        timed_report = _timed_prelude(declaration, point, logical_end)
        if timed_report:
            # The first event already has its certified absolute timestamp.
            # Do not rewind the cursor to B after the strict native prelude.
            body.body.pop(0)
        frames = _terminal_frames(compiled, compiled.points[point.index])
        new_body = []
        if component_program:
            new_body.append(
                _statement(
                    "if not self.native_context.initialized "
                    "or self.native_context.finalized:\n"
                    "    raise RuntimeError('Native session must be "
                    "initialized and active')"
                )
            )
        new_body.append(_statement("self.core.reset()"))
        new_body += [
            _statement("self.native_control_t0_mu = _native_int64(0)"),
            _statement("self.native_physical_base_mu = _native_int64(0)"),
        ]
        for name in frame_names:
            new_body.extend(
                [
                    _statement(f"self.native_terminal_f_{frame_suffixes[name]} = 0.0"),
                    _statement(f"self.native_terminal_h_{frame_suffixes[name]} = 0.0"),
                    _statement(f"self.native_terminal_v_{frame_suffixes[name]} = 0.0"),
                ]
            )
        new_body.extend(
            _call(call["method"], call.get("args", ()))
            for call in declaration.get("prefix", ())
        )
        new_body.extend(
            [
                _statement("self.core.break_realtime()"),
                copy.deepcopy(epoch),
                _statement("self.native_physical_base_mu = t0"),
                _statement(f"self.native_control_t0_mu = t0 + {point.origin_mu}"),
            ]
        )
        if timed_report:
            for name in frame_names:
                new_body.extend(
                    [
                        _statement(
                            f"self.native_terminal_f_{frame_suffixes[name]} = 0.0"
                        ),
                        _statement(
                            f"self.native_terminal_h_{frame_suffixes[name]} = 0.0"
                        ),
                        _statement(
                            f"self.native_terminal_v_{frame_suffixes[name]} = 0.0"
                        ),
                    ]
                )
        for call in timed_report:
            new_body.extend(
                [
                    _statement(f"at_mu(t0 + {call['start_mu_from_B']})"),
                    _call(call["method"], call.get("args", ())),
                    _statement(
                        f"if now_mu() != t0 + {call['end_mu_from_B']}:\n"
                        "    raise ValueError("
                        "'Strict native duration differs from contract')"
                    ),
                ]
            )
        new_body.append(_statement(f"self._taiyi_body_{point.index}(t0)"))
        for name, frame in frames.items():
            new_body.extend(
                [
                    _statement(
                        f"self.native_terminal_f_{frame_suffixes[name]} = "
                        f"{frame['frame_phase']!r}"
                    ),
                    _statement(
                        f"self.native_terminal_h_{frame_suffixes[name]} = "
                        f"{frame['hardware_phase']!r}"
                    ),
                    _statement(
                        f"self.native_terminal_v_{frame_suffixes[name]} = "
                        f"{frame['virtual_z']!r}"
                    ),
                ]
            )
        suffix_end = logical_end
        suffix_report = []
        for call in declaration.get("suffix", ()):
            duration = call["duration_mu"]
            if type(duration) is not int or duration < 0:
                raise ExportError(
                    "Native suffix duration must be nonnegative native mu"
                )
            if type(call.get("phase_sensitive")) is not bool:
                raise ExportError("Native suffix must declare phase sensitivity")
            new_body.append(_statement(f"at_mu(t0 + {suffix_end})"))
            new_body.append(_call(call["method"], call.get("args", ())))
            suffix_report.append(
                {
                    **call,
                    "start_mu_from_B": suffix_end,
                    "end_mu_from_B": suffix_end + duration,
                }
            )
            suffix_end += duration
            new_body.append(
                _statement(
                    f"if now_mu() != t0 + {suffix_end}:\n"
                    "    raise ValueError("
                    "'Native suffix duration differs from contract')"
                )
            )
        drain_end = max(physical_end, suffix_end)
        new_body.append(
            _statement(f"if now_mu() < t0 + {drain_end}:\n    at_mu(t0 + {drain_end})")
        )
        if finalize_method is not None:
            new_body.append(_call(finalize_method))
        new_body.append(_statement("self.core.wait_until_mu(now_mu())"))
        method.body = new_body
        report_points.append(
            {
                "index": point.index,
                "prefix": declaration.get("prefix", []),
                "timed": timed_report,
                "suffix": suffix_report,
                "body_method": body.name,
                "epoch": epochs[point.index],
                "logical_origin_mu_from_B": point.origin_mu,
                "logical_end_mu_from_B": logical_end,
                "physical_end_mu_from_B": physical_end,
                "minimum_drain_end_mu_from_B": drain_end,
                "terminal_frames": frames,
                "carrier_references": {
                    event.signal: copy.deepcopy(event.params["phase_reference"])
                    for event in original_point.physical_events
                    if event.kind == "spectrum_program"
                },
            }
        )
    insertion = tree.body.index(generated)
    if component_program:
        from .component_export import (
            configure_component_export,
            isolated_component_loader,
        )

        configure_component_export(program, build_prelude)
        tree.body[insertion:insertion] = isolated_component_loader(
            package, pins, list(frame_suffixes.values()), _loader
        )
    else:
        tree.body[insertion:insertion] = _loader(package, pins, reserved)
    ast.fix_missing_locations(tree)
    composed = ast.unparse(tree) + "\n"
    ast.parse(composed)
    return composed, {
        "schema_version": 1,
        "execution": "one_top_level_kernel_per_shot",
        "time_reference": "control_epoch_after_native_prefix",
        "phase_reference": "per_triggered_sequence",
        "source_pins": pins,
        "initialize_method": initialize_method,
        "finalize_method": finalize_method,
        **(
            {
                "native_program": package,
                "session_finalize_method": "finalize_native_session",
                "shared_context": "native_context",
                "lifecycle": {
                    "initialize": "once",
                    "after_shot": "per_shot",
                    "finalize": "session_end",
                },
            }
            if component_program
            else {}
        ),
        "control_attributes": {
            "physical_base": "native_physical_base_mu",
            "time_origin": "native_control_t0_mu",
            "phase_origin": "native_control_t0_mu",
            "phase_origin_is_timeline_only": True,
            "terminal_frames": {
                name: {
                    "F": f"native_terminal_f_{frame_suffixes[name]}",
                    "H": f"native_terminal_h_{frame_suffixes[name]}",
                    "V": f"native_terminal_v_{frame_suffixes[name]}",
                }
                for name in frame_names
            },
            "terminal_frame_convention": "F = accumulated shift_phase - Rz since reset",
            "legacy_frame_attributes": (
                "H=F+V and V is resettable Rz source diagnostics; "
                "new native code consumes F directly"
            ),
        },
        "points": report_points,
        "verification": {
            "native_call_graph": "declared_contract",
            "resource_identity_check": "required_by_host_validate_target",
            "real_artiq_compile": "not_verified",
            "physical_hardware": "not_verified",
        },
    }
