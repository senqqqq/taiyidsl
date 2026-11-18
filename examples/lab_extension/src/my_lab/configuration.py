"""Illustrative image-analysis settings, separate from experiment execution."""

import json
from pathlib import Path

from taiyidsl import ImageResults, ResultSet


def image_results(*, points=1, shots=3, config_path=None):
    """Explicit result schema for one native-triggered camera image per shot.

    A native trigger does not create a DSL acquisition handle. Here the lab
    explicitly declares its result schema. For a DSL acquire() event, prefer
    ImageResults.from_compiled to bind the schema to the compiled acquisitions.
    """
    path = Path(config_path) if config_path else Path(__file__).with_name("camera.json")
    config = json.loads(path.read_text(encoding="utf-8"))
    results = ResultSet(
        axes=[],
        coordinates=[{} for _ in range(points)],
        shots=shots,
        qubits=list(config["rois"]),
        handles=["readout"],
        metadata={"synthetic": True, "schema_source": "explicit lab declaration"},
    )
    return ImageResults(
        results,
        acquisitions={"readout": {"camera": "fake_camera", "trigger": "native"}},
        **config,
    )
