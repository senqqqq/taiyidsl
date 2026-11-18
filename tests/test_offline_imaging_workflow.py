"""End-to-end offline example includes both the scan and its complete data context."""

import runpy
from pathlib import Path

from taiyidsl import ImageResults


def test_example_archive_and_figures(tmp_path):
    root = Path(__file__).resolve().parents[1]
    example = runpy.run_path(str(root / "examples/offline_imaging_workflow.py"))
    output = example["main"](tmp_path / "imaging")
    restored = ImageResults.load_hdf5(output / "results.h5")
    assert restored.context["complete"]
    assert restored.context["lab_config"]["backend"] == "artiq"
    assert restored.context["optics_config"]["schema_version"] == 2
    assert set(restored.context["qubits"]) == {"q0", "q1"}
    assert restored.context["experiment"]["metadata"]["synthetic"]
    assert len(restored.context["compiled_experiment"]["points"]) == 4
    assert restored.survival().valid_counts.min().item() > 0
    for name in (
        "survival.png",
        "envelopes.png",
        "timeline.html",
        "artiq/experiment.py",
    ):
        assert (output / name).stat().st_size > 0
