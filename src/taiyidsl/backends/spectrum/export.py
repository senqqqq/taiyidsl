"""Export the single-card playback program and its offline verification."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from taiyidsl.backends.artiq_spectrum.validation_awg import validate_program


def export_spectrum(compiled, output_dir):
    """Verify each complete card program before publishing the playback manifest."""
    compiled_hash = compiled.sha256
    points = []
    for point in compiled.points:
        (shot,) = point.physical_events
        program = shot.params["program"]
        if shot.params["contract"]["mode"] == "dds":
            from .dds import validate_square_program

            validate_square_program(program)
            check = {"status": "passed", "method": "dds_command_validation"}
        else:
            check = {"method": "awg_sample_replay", **validate_program(program)}
        points.append(
            {
                "index": point.index,
                "coordinates": point.coordinates,
                "card": shot.signal,
                "logical_duration_mu": point.duration_mu,
                "physical_duration_mu": shot.duration_mu,
                **shot.params,
                "offline_check": check,
            }
        )
    verification = {"offline": "passed", "hardware": "not_verified"}
    manifest = {
        "schema_version": 1,
        "backend": "spectrum",
        "compiled_sha256": compiled_hash,
        "tick_seconds": compiled.setup.tick_seconds,
        "phase_reference": "card_playback_start",
        "host_execution_plan": compiled.host_execution_plan,
        "points": points,
        "verification": verification,
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output) as temporary:
        stage = Path(temporary)
        compiled.save(stage / "compiled.json")
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        (stage / "compiled.json").replace(output / "compiled.json")
        (stage / "manifest.json").replace(output / "manifest.json")
    return {
        "plan": output / "compiled.json",
        "manifest": output / "manifest.json",
        "verification": verification,
    }
