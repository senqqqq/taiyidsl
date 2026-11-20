"""Offline hybrid export and a host-only launch bridge.

The native ARTIQ artifact remains independently compilable and verifiable.
Only its host shot boundary is wrapped by the all-cards-ready protocol.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import re
from dataclasses import replace
from pathlib import Path

from taiyidsl.backends.artiq.backend import ArtiqBackend
from taiyidsl.backends.artiq_spectrum.backend import (
    native_event,
    native_factor,
    native_mapping,
    native_setup,
)
from taiyidsl.ir import CompiledPoint, CompileError, ScheduledEvent
from taiyidsl.sample_assets import pack_sample_manifest


def artiq_subplan(compiled):
    """Recover native mu without rescaling native driver parameter payloads."""
    compiled.assert_integrity()
    factor = native_factor(compiled.setup)
    setup = native_setup(compiled.setup)
    mapping = native_mapping(compiled.signal_map, compiled.setup)
    points = []
    for point in compiled.points:
        logical = [
            native_event(e, factor)
            for e in point.logical_events
            if e.kind != "spectrum_region"
            and not e.params.get("region_id")
            and e.params.get("route") not in {"native_artiq", "logical_frame"}
        ]
        physical = []
        ending = point.duration_mu
        for event in point.physical_events:
            if event.kind == "spectrum_program":
                params = event.params
                group = compiled.setup.spectrum.groups[params["name"]]
                routes = params.get(
                    "trigger_routes",
                    [
                        {
                            "signal": f"__spectrum_trigger_{params['name']}",
                            "device": params["trigger_device"],
                            "relative_mu": params["trigger_relative_mu"],
                            "width_mu": group.trigger_width_mu,
                        }
                    ],
                )
                for route in routes:
                    mapping[route["signal"]] = route["device"]
                    logical.append(
                        ScheduledEvent(
                            "trigger",
                            route["signal"],
                            route["relative_mu"],
                            route["width_mu"],
                            {"spectrum_group": params["name"]},
                            event.source,
                            logical_ids=event.logical_ids,
                        )
                    )
                ending = max(ending, event.end_mu - point.origin_mu)
            else:
                physical.append(native_event(event, factor))
        points.append(
            CompiledPoint(
                point.index,
                copy.deepcopy(point.coordinates),
                point.axis_indices,
                sorted(logical, key=lambda e: e.start_mu),
                physical,
                (ending + factor - 1) // factor,
                point.origin_mu // factor,
            )
        )
    backend = ArtiqBackend()
    return replace(
        compiled,
        setup=setup,
        signal_map=mapping,
        points=points,
        provenance={
            **compiled.provenance,
            "backend": backend.describe(),
            "target": backend.target,
            "parent_backend": "artiq_spectrum",
        },
        _backend=backend,
    )


def _epochs(compiled, native):
    base_grid = math.lcm(
        native.setup.rtio_coarse_mu,
        *(
            math.lcm(d.grid_mu, d.config_grid_mu if d.kind == "dds" else 1)
            for d in native.setup.devices.values()
        ),
    )
    result = {}
    for point in compiled.points:
        programs = [
            e.params for e in point.physical_events if e.kind == "spectrum_program"
        ]
        grid = math.lcm(base_grid, *(p["epoch_grid_mu"] for p in programs))
        if grid > 1_000_000:
            raise CompileError("Trigger phase-class grid is too large")
        offset = next(
            (
                x
                for x in range(0, grid, base_grid)
                if all(
                    (x + p["trigger_physical_mu"]) % p["epoch_grid_mu"]
                    == p["epoch_offset_mu"]
                    for p in programs
                )
            ),
            None,
        )
        if offset is None:
            raise CompileError("Trigger phase classes conflict with native ARTIQ grids")
        result[point.index] = {"grid_mu": grid, "offset_mu": offset}
    return result


def align_source_epochs(source, epochs):
    """Rewrite only generated per-point origin assignments; preserve comments."""
    current = None
    seen = set()
    lines = []
    for line in source.splitlines():
        match = re.match(r"    def point_(\d+)\(self\):", line)
        if match:
            current = int(match.group(1))
        elif re.match(r"    def \w+\(", line):
            current = None
        if current is not None and re.match(r"        t0 = ", line):
            grid, offset = epochs[current]["grid_mu"], epochs[current]["offset_mu"]
            line = (
                f"        t0 = ((now_mu() + {grid - 1 - offset}) // {grid}) "
                f"* {grid} + {offset}"
            )
            seen.add(current)
        lines.append(line)
    if seen != set(epochs):
        raise CompileError("Generated ARTIQ source has an unknown epoch structure")
    result = "\n".join(lines) + "\n"
    ast.parse(result)
    return result


def add_trigger_resource_checks(source, compiled):
    """Protect active group triggers against device_db aliases on the host.

    Only the host target validator changes. Point kernels and their absolute
    timestamps remain byte-for-byte untouched, including native body boundaries.
    """
    pairs = set()
    for point in compiled.points:
        triggers = {
            route["device"]
            for event in point.physical_events
            if event.kind == "spectrum_program"
            for route in event.params.get(
                "trigger_routes", [{"device": event.params["trigger_device"]}]
            )
        }
        other_names = {
            event.signal
            for event in point.physical_events
            if event.kind != "spectrum_program"
        }
        pairs.update(
            (trigger, name)
            for trigger in triggers
            for name in other_names
            if name != trigger
        )
    if not pairs:
        return source
    tree = ast.parse(source)
    generated = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GeneratedExperiment"
    )
    validator = next(
        node
        for node in generated.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_target"
    )
    lines = source.splitlines()
    lines.insert(
        validator.end_lineno,
        "        _taiyi_validate_trigger_resources(self, "
        + repr(tuple(sorted(pairs)))
        + ")",
    )
    result = "\n".join(lines) + "\n" + _TRIGGER_RESOURCE_CHECKS
    ast.parse(result)
    return result


_TRIGGER_RESOURCE_CHECKS = """
def _taiyi_trigger_resource_keys(device):
    from numbers import Integral
    pending, visited, keys = [device], set(), set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)
        keys.add(("driver", identity))
        channel = getattr(current, "channel", None)
        if isinstance(channel, Integral):
            keys.add(("rtio_channel", int(channel)))
        for attribute in ("bus", "cpld", "sw", "io_update"):
            related = getattr(current, attribute, None)
            if related is not None and not isinstance(related, (str, int, float, bool)):
                pending.append(related)
    return keys

def _taiyi_validate_trigger_resources(experiment, pairs):
    keys = {}
    for trigger, other in pairs:
        for name in (trigger, other):
            if name not in keys:
                keys[name] = _taiyi_trigger_resource_keys(experiment.get_device(name))
        if keys[trigger] & keys[other]:
            raise RuntimeError(
                "Spectrum trigger hardware resource alias: " + trigger + " / " + other
            )
"""


def spectrum_manifest(compiled, epochs=None):
    from taiyidsl.backends.artiq_spectrum.banks import build_manifest_banks

    compiled.assert_integrity()
    points = []
    for point in compiled.points:
        groups = [
            copy.deepcopy(e.params)
            for e in point.physical_events
            if e.kind == "spectrum_program"
        ]
        points.append(
            {
                "index": point.index,
                "coordinates": point.coordinates,
                "groups": groups,
                "epoch": (epochs or {}).get(point.index),
            }
        )
    mixed = any(
        card["program"].get("kind") == "dds"
        for point in points
        for group in point["groups"]
        for card in group["cards"].values()
    )
    return {
        "schema_version": 3 if mixed else 2,
        "backend": "artiq_spectrum",
        "engine": "mixed_awg_dds" if mixed else "awg_sequence",
        "name": compiled.name,
        "shots": compiled.shots,
        "averaging_mode": compiled.averaging_mode,
        "host_execution_plan": copy.deepcopy(compiled.host_execution_plan),
        "points": points,
        "banks": build_manifest_banks(points, compiled.host_execution_plan),
        "compiled_sha256": compiled._integrity_hash,
        "verification": {
            "offline": "not_verified",
            "artiq_compile": "not_verified",
            "spectrum_driver": "not_verified",
            "hardware": "not_verified",
        },
    }


def _write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def export_spectrum(compiled, output_dir, *, mode="auto"):
    """Export recipes and verified native kernels; never open a card."""
    from taiyidsl.backends.artiq.export import export_artiq
    from taiyidsl.backends.artiq.verification import verify_export
    from taiyidsl.backends.artiq_spectrum.dds import validate_dds_program
    from taiyidsl.backends.artiq_spectrum.validation_awg import validate_program

    if mode not in {"auto", "unrolled"}:
        raise ValueError("Export mode must be auto or unrolled")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    native = artiq_subplan(compiled)
    epochs = _epochs(compiled, native)
    # Per-point origins can differ across a scan. The existing native unrolled
    # exporter retains resident banks and native pulse preparation semantics.
    native_result = export_artiq(native, output / "artiq", mode="unrolled")
    source_path = output / "artiq" / "experiment.py"
    source = align_source_epochs(source_path.read_text(encoding="utf-8"), epochs)
    source = add_trigger_resource_checks(source, compiled)
    native_report = verify_export(native, source=source)
    composition = None
    if compiled.metadata.get("native_artiq"):
        from taiyidsl.native.export import compose_native_source

        (output / "artiq" / "control_body_reference.py").write_text(
            source, encoding="utf-8"
        )
        source, composition = compose_native_source(compiled, native, source, epochs)
    source_path.write_text(source, encoding="utf-8")
    native_manifest_path = output / "artiq" / "manifest.json"
    native_manifest = json.loads(native_manifest_path.read_text(encoding="utf-8"))
    native_manifest.update(
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        verification=native_report,
        spectrum_epoch_classes=epochs,
    )
    if composition is not None:
        native_manifest["native_composition"] = composition
        native_manifest["verification"] = {
            "control_body_reference": native_report,
            "native_source_audit": compiled.metadata["native_artiq"]["source_audit"],
            "composition": composition["verification"],
        }
    _write_json(native_manifest_path, native_manifest)
    manifest = spectrum_manifest(compiled, epochs)
    checks = []
    for point in manifest["points"]:
        for group in point["groups"]:
            for card_name, card in group["cards"].items():
                if card["program"].get("kind") == "dds":
                    validate_dds_program(card["program"])
                    validation = {"status": "passed", "mode": "dds"}
                else:
                    validation = validate_program(card["program"])
                checks.append(
                    {
                        "point": point["index"],
                        "group": group["name"],
                        "card": card_name,
                        **validation,
                    }
                )
    manifest["verification"]["offline"] = "passed"
    manifest["offline_checks"] = checks
    manifest["artiq_verification"] = native_report
    if composition is not None:
        manifest["native_artiq"] = composition
        manifest["artiq_verification"] = native_manifest["verification"]
    manifest["requested_export_mode"] = mode
    pack_sample_manifest(manifest)
    _write_json(output / "manifest.json", manifest)
    compiled.save(output / "compiled.json")
    bridge = '''"""Host bridge; configure spectrum_runner and result_adapter first.

No card is opened by importing or building this experiment. The separately
exported artiq/experiment.py contains all kernel code and is compile-only safe.
"""
import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "_taiyidsl_native_artiq", _ROOT / "artiq" / "experiment.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

class GeneratedExperiment(_MODULE.GeneratedExperiment):
    def build(self):
        super().build()
        self.spectrum_runner = None
        self.spectrum_manifest = json.loads(
            (_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        self.spectrum_receipts = []

    def _run_shot(self, point, shot):
        active = bool(self.spectrum_manifest["points"][point]["groups"])
        if active and self.spectrum_runner is None:
            raise RuntimeError("Configure SpectrumRunner before a hybrid shot")
        # Host preparation precedes card mutation/arming. It is not an embedded
        # native kernel or part of the compiled shot's deterministic timeline.
        self.result_adapter.before_shot(point, shot)
        if active:
            receipt = self.spectrum_runner.run_point(
                self.spectrum_manifest, point, self._execute_point
            )
        else:
            self._execute_point(point)
            receipt = {"point": point, "status": "complete", "cards": {},
                       "uploaded": False, "physical_acceptance": "not_verified"}
        # Check card completion before detector transfer or any next-shot setup.
        self.result_adapter.after_shot(point, shot)
        self.spectrum_receipts.append(
            {"point": point, "shot": shot, "receipt": receipt,
             "execution_index": len(self.spectrum_receipts)}
        )
'''
    (output / "experiment.py").write_text(bridge, encoding="utf-8")
    ast.parse(bridge)
    report_path = output / "sequence_report.json"
    _write_json(
        report_path,
        {
            "name": compiled.name,
            "tick_seconds": compiled.setup.tick_seconds,
            "hardware": "not_verified",
            "points": [
                {
                    "index": point["index"],
                    "coordinates": point["coordinates"],
                    "trigger_pulses": sum(
                        len(group.get("trigger_routes", [group["trigger_device"]]))
                        for group in point["groups"]
                    ),
                    "logical_regions": [
                        {
                            "region_id": event.params["region_id"],
                            "group": event.params["group"],
                            "start_ticks": event.start_mu,
                            "duration_ticks": event.duration_mu,
                            "terminal_frames": event.params.get("terminal_frames", {}),
                        }
                        for event in compiled.points[point["index"]].logical_events
                        if event.kind == "spectrum_region"
                    ],
                    "groups": [
                        {
                            "name": group["name"],
                            "region_id": group["region_id"],
                            "phase_reference": group["phase_reference"],
                            "calibration_revision": group["calibration"].get(
                                "revision"
                            ),
                            "cards": {
                                name: _program_summary(card["program"])
                                for name, card in group["cards"].items()
                            },
                        }
                        for group in point["groups"]
                    ],
                }
                for point in manifest["points"]
            ],
        },
    )
    return {
        "source": output / "experiment.py",
        "plan": output / "compiled.json",
        "manifest": output / "manifest.json",
        "report": report_path,
        "artiq": native_result,
        "verification": manifest["verification"],
    }


def _program_summary(program):
    if program.get("kind") == "dds":
        return {
            "mode": "dds",
            "frames": len(program["frames"]),
            "command_count": program["command_count"],
            "duration_mu": program["duration_mu"],
            "terminal_state": "continuous_hold",
        }
    return {
        "segments": len(program["segments"]),
        "steps": len(program["steps"]),
        "memory": program["memory"],
        "logical_duration_samples": program["logical_duration_samples"],
        "physical_duration_samples": program["physical_duration_samples"],
    }
