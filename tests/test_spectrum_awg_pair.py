"""Check the first sample, ARTIQ trigger and shared bank for the review example."""

import json

import pytest

from examples.spectrum_awg_pair import compile_pair
from taiyidsl import CompileError
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.backends.artiq_spectrum.banks import build_bank, entry_program


@pytest.mark.parametrize("count", [1, 2])
def test_routes_align_the_first_sample_and_export_finite_steps(count, tmp_path):
    compiled = compile_pair(count)
    manifest = json.loads(
        compiled.export(tmp_path)["manifest"].read_text(encoding="utf-8")
    )
    group = manifest["points"][0]["groups"][0]
    edges = group["trigger_routes"]
    assert [r["device"] for r in edges] == ["trig_A", "trig_B"][:count]
    assert all(r["relative_mu"] == -512 and r["width_mu"] == 64 for r in edges)
    assert manifest["artiq_verification"]["offline_export_check"] == "passed"
    for index, (name, card) in enumerate(group["cards"].items()):
        prog = card["program"]
        lead = 140 + 16 * index
        first, second = prog["pulses"]
        assert [first["start_sample"], second["start_sample"]] == [lead, lead + 1500]
        assert lead + card["contract"]["trigger_latency_samples"] == 640
        assert first["duration_samples"] == second["duration_samples"] == 500
        assert reconstruct_window(prog, lead - 1, lead + 1)[:, 0].tolist() == [0, 8192]
        bank = manifest["banks"][0]["groups"][0]["cards"][name]["program"]
        assert entry_program(bank, 0)["steps"] == prog["steps"]
        shared = build_bank({0: prog, 1: prog})
        assert len(shared["segments"]) == len(prog["segments"])
        assert len(shared["steps"]) == 2 * len(prog["steps"])
        assert entry_program(shared, 1)["steps"] == prog["steps"]


def test_capacity_rejects_a_program_that_cannot_fit_in_equal_slots():
    with pytest.raises(CompileError, match="capacity"):
        compile_pair(memory_bytes=256)
