"""Long sequences must reuse sampled waveforms and execute explicit call data."""

import ast
import importlib.util
from pathlib import Path

import pytest

from taiyidsl.artiq import generate_source, verify_equivalence, verify_export

SCRIPT = Path(__file__).parents[1] / "examples" / "ram_gate_sequence.py"
spec = importlib.util.spec_from_file_location("ram_gate_sequence_example", SCRIPT)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def launches(compiled):
    return [e for e in compiled.points[0].physical_events if e.kind == "dds_ram_start"]


def test_long_gate_sequence_reuses_two_banks_and_matches_mock():
    compiled = example.build_sequence()
    gates = launches(compiled)
    assert len(gates) == 1024
    assert [e.params["ram_profile"] for e in gates[:8]] == [0, 0, 1, 1] * 2
    profiles = {e.params["ram_profile"]: e.params for e in gates}
    assert sum(len(p["ram_asf"]) for p in profiles.values()) == 802
    assert max(profiles[0]["ram_asf"]) == round(0.5 * 0x3FFF)
    assert max(profiles[1]["ram_asf"]) == round(0.25 * 0x3FFF)
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_gate_phase_changes_do_not_allocate_more_waveforms():
    compiled = example.build_sequence(cycles=2)
    gates = launches(compiled)
    assert [e.params["pow"] for e in gates[:4]] == [0, 16384, 0, 16384]
    assert [e.params["ram_profile"] for e in gates[:4]] == [0, 0, 1, 1]
    assert verify_equivalence(compiled)["status"] == "passed"


def test_long_table_keeps_kernel_code_bounded_and_preloads_only_two_waveforms():
    short = generate_source(example.build_sequence(cycles=1))
    long = generate_source(example.build_sequence(cycles=256))

    def point_kernel(source):
        tree = ast.parse(source)
        return next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "point_0"
        )

    assert len(list(ast.walk(point_kernel(short)))) == len(
        list(ast.walk(point_kernel(long)))
    )
    assert long.count(".write_ram(") == 2
    assert "for event_index in range(len(events))" in long


@pytest.mark.parametrize(
    "old,new",
    [
        ("matched_latency_enable=1", "matched_latency_enable=0"),
        (
            "self.result_adapter = None",
            "self.result_adapter = None\n        self.device_0.bus.ref_period_mu = 16",
        ),
        ("int32(events[event_index, 8])", "int32(7)"),
        (
            "                self.device_0.set_cfr1(ram_enable=0)",
            "                pass  # Removed required disable",
        ),
        (
            "self.device_0.set_pow(int32(events[event_index, 5]))",
            "self.device_0.set_pow(0)",
        ),
        (
            "            device_id = events[event_index, 0]",
            "            self.device_0.write_ram([int32(0), int32(0)])\n"
            "            device_id = events[event_index, 0]",
        ),
    ],
)
def test_replay_mutations_are_rejected(old, new):
    compiled = example.build_sequence(cycles=1)
    source = generate_source(compiled)
    assert old in source
    with pytest.raises(ValueError):
        verify_export(compiled, source=source.replace(old, new))


def test_rearm_requires_parameter_commit_before_enable():
    compiled = example.build_sequence(cycles=1)
    source = generate_source(compiled)
    old = (
        "                self.device_0.set_pow(int32(events[event_index, 5]))\n"
        "                self.device_0.io_update.pulse_mu(8)"
    )
    assert old in source
    with pytest.raises(ValueError, match="commit phase/frequency"):
        verify_export(compiled, source=source.replace(old, old.splitlines()[0]))


def test_manifest_discloses_bank_usage_and_gap(tmp_path):
    import json

    compiled = example.build_sequence(cycles=2)
    artifact = compiled.export_artiq(tmp_path)
    manifest = json.loads(artifact["manifest"].read_text())
    (bank,) = manifest["ram_banks"]
    assert bank["plays"] == 8
    assert bank["resident_words"] == 802
    assert len(bank["profiles"]) == 2
    assert bank["min_gap_mu"] == 4112
    assert manifest["code_generation"]["ram_execution"] == "event_table"
