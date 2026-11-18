"""Publish ARTIQ source, compiled plan and offline verification manifest."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from taiyidsl.backends.artiq._contracts import (
    _devices,
    _origin_grid,
    _words,
)
from taiyidsl.backends.artiq.codegen import _generate
from taiyidsl.backends.artiq.codegen_ram import (
    ram_bank_summary,
)
from taiyidsl.backends.artiq.verification import verify_export
from taiyidsl.execution import device_execution_plan
from taiyidsl.host_plan import execution_plan_for
from taiyidsl.serialization import to_jsonable as _jsonable


def export_artiq(compiled, output_dir, *, mode="auto"):
    """Write reviewable source, the compiled plan, and verification manifest."""
    from taiyidsl.backends.artiq.polar_contracts import polar_bank_summary, uses_polar

    source, code_generation = _generate(compiled, mode)
    report = verify_export(compiled, source=source)
    devices = _devices(compiled)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    source_path, plan_path, manifest_path = (
        output / "experiment.py",
        output / "compiled.json",
        output / "manifest.json",
    )
    quantization = []
    for point in compiled.points:
        for event in point.physical_events:
            if event.kind not in {"dds_config", "dds_prepare"}:
                continue
            device = devices[event.signal]
            ftw, pow_, asf = _words(event, device)
            realized_frequency = (
                (ftw & 0xFFFFFFFF)
                / (1 << 32)
                * float(getattr(device, "sysclk_hz", 1e9))
            )
            realized_amplitude = asf / 0x3FFF
            realized_phase = pow_ / (1 << 16) * 2 * math.pi
            requested_phase = float(event.params.get("phase", 0)) % (2 * math.pi)
            quantization.append(
                {
                    "point": point.index,
                    "device": event.signal,
                    "start_mu": event.start_mu,
                    "words": {"ftw": ftw, "pow": pow_, "asf": asf},
                    **(
                        {
                            "square_pow": event.params["square_pow"],
                            "effect_mu": event.params["effect_mu"],
                        }
                        if event.kind == "dds_prepare"
                        else {}
                    ),
                    "actual": {
                        "frequency": realized_frequency,
                        "amplitude": realized_amplitude,
                        "phase": realized_phase,
                    },
                    "error": {
                        "frequency": realized_frequency
                        - float(event.params["frequency"]),
                        "amplitude": realized_amplitude
                        - float(event.params["amplitude"]),
                        "phase": (realized_phase - requested_phase + math.pi)
                        % (2 * math.pi)
                        - math.pi,
                    },
                }
            )
    manifest = {
        "schema_version": 1,
        "name": compiled.name,
        "target": "ARTIQ 9 / Urukul AD9910 / TTL",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "averaging_mode": compiled.averaging_mode,
        "shots": compiled.shots,
        "host_execution_plan": _jsonable(execution_plan_for(compiled)),
        "tick_seconds": float(compiled.setup.tick_seconds),
        "origin_grid_mu": _origin_grid(
            devices, getattr(compiled.setup, "rtio_coarse_mu", 8)
        ),
        "devices": _jsonable(devices),
        "execution_resources": device_execution_plan(compiled),
        "provenance": _jsonable(compiled.provenance),
        "verification": report,
        "code_generation": code_generation,
        "dds_register_quantization": quantization,
        "ram_banks": (
            polar_bank_summary(compiled) + ram_bank_summary(compiled)
            if uses_polar(compiled)
            else ram_bank_summary(compiled)
        ),
        "result_adapter": {
            "required": True,
            "host_methods": [
                "before_shot(point, shot)",
                "after_shot(point, shot)",
                "finish()",
            ],
            "notes": (
                "External detector transfer and loading are user-configured "
                "host integrations; no detector is assumed."
            ),
        },
        "hardware_prerequisites": [
            "Confirm device_db names, ARTIQ/gateware versions "
            "and declared driver durations",
            "AD9910 configuration grid includes the driver 8-mu cursor alignment; "
            "duration remains an assumed contract",
            "Validate DDS SYNC_CLK/IO_UPDATE alignment and common phase reference",
            "Validate optical path delays separately",
            "Inspect RTIO errors and core log during hardware acceptance",
        ],
    }
    source_path.write_text(source, encoding="utf-8")
    plan_path.write_text(
        json.dumps(_jsonable(compiled), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return {
        "source": source_path,
        "compiled": plan_path,
        "manifest": manifest_path,
        "verification": report,
    }
