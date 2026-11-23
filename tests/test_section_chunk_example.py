"""The combined example preserves RB words while changing scheduling/upload units."""

import hashlib
import json
import runpy
from pathlib import Path

import numpy as np
import pytest

from taiyidsl.backends.artiq_spectrum.export import spectrum_manifest
from taiyidsl.experiments._single_qubit_clifford import rb_sequences, sequence_rotation


@pytest.fixture(scope="module")
def example():
    path = (
        Path(__file__).resolve().parents[1]
        / "examples/artiq_spectrum/section_chunk_rb.py"
    )
    return runpy.run_path(str(path))


@pytest.fixture(scope="module")
def compiled(example):
    return example["build_lab"]().compile(example["build_experiment"]())


def test_example_uses_fixed_seed_words_and_explicit_two_card_routes(example, compiled):
    assert compiled.metadata["rb_sequences"]["q0"] == rb_sequences(
        (1, 2, 3, 4), sequence_count=1, seed=101
    )
    assert compiled.metadata["rb_sequences"]["q1"] == rb_sequences(
        (1, 2, 3, 4), sequence_count=1, seed=102
    )
    assert example["build_experiment"]().metadata == compiled.metadata
    for words in compiled.metadata["rb_sequences"].values():
        for word in words:
            np.testing.assert_array_equal(sequence_rotation(word["gates"]), np.eye(3))
    assert compiled.setup.spectrum.groups["qubit_awg"].card_trigger_devices == {
        "A": "awg_start_a",
        "B": "awg_start_b",
    }
    assert compiled.metadata["hardware_verified"] is False


def test_each_qubit_word_is_intact_and_independent_words_overlap(compiled):
    observed_overlap = False
    for point in compiled.points:
        duration_by_qubit = []
        for uid in ("q0", "q1"):
            word = compiled.metadata["rb_sequences"][uid][point.index]
            events = [
                e
                for e in point.logical_events
                if e.kind == "play" and e.signal == f"{uid}.rf"
            ]
            assert len(events) == len(word["gates"])
            durations = [800 if gate.endswith("180") else 400 for gate in word["gates"]]
            assert [e.duration_mu for e in events] == [
                duration * 5 for duration in durations
            ]
            if events:
                assert events[0].start_mu == 100000  # the 20 us preparation marker
                assert [e.start_mu for e in events[1:]] == [
                    e.end_mu for e in events[:-1]
                ]
                assert all(e.params["operation_path"][-1] == "rb_body" for e in events)
                duration_by_qubit.append(events[-1].end_mu - events[0].start_mu)
            else:
                duration_by_qubit.append(0)
        observed_overlap |= all(duration_by_qubit)
        acquisition = next(e for e in point.logical_events if e.kind == "acquire")
        assert acquisition.start_mu == 100000 + max(duration_by_qubit)
        assert acquisition.params["qubits"] == ["q0", "q1"] or tuple(
            acquisition.params["qubits"]
        ) == ("q0", "q1")
        edges = {
            name: [
                (e.kind, e.start_mu) for e in point.physical_events if e.signal == name
            ]
            for name in ("awg_start_a", "awg_start_b")
        }
        assert edges["awg_start_a"] == edges["awg_start_b"]
        assert [kind for kind, _ in edges["awg_start_a"]] == ["ttl_on", "ttl_off"]
    assert observed_overlap


def test_example_chunks_preserve_coordinates_and_reuse_banks(compiled):
    assert [p.coordinates["length"] for p in compiled.points] == [1, 2, 3, 4]
    assert compiled.host_execution_plan["chunks"] == [
        {"index": 0, "points": [0, 1]},
        {"index": 1, "points": [2, 3]},
    ]
    assert list(compiled.execution_order()) == [
        (0, 0),
        (1, 0),
        (0, 1),
        (1, 1),
        (2, 0),
        (3, 0),
        (2, 1),
        (3, 1),
    ]
    manifest = spectrum_manifest(compiled)
    assert manifest["schema_version"] == 2
    assert len(manifest["banks"]) == 2
    for bank in manifest["banks"]:
        assert bank["points"] in ([0, 1], [2, 3])
        assert set(bank["groups"][0]["cards"]) == {"A", "B"}


def test_combined_report_exports_actual_source_and_two_trigger_edges(
    example, compiled, tmp_path
):
    result = compiled.export_report(tmp_path, source_path=example["__file__"])
    assert result["index"].is_file()
    assert (tmp_path / "dsl_source.py").read_bytes() == Path(
        example["__file__"]
    ).read_bytes()
    report = json.loads(
        (tmp_path / "export/sequence_report.json").read_text(encoding="utf-8")
    )
    assert [point["trigger_pulses"] for point in report["points"]] == [2] * 4
    assert report["hardware"] == "not_verified"
    decoded = json.loads(result["report"].read_text(encoding="utf-8"))
    assert decoded["host_execution_plan"] == compiled.host_execution_plan
    assert len(decoded["banks"]) == 2
    manifest = json.loads(result["export"]["manifest"].read_text(encoding="utf-8"))
    for bank, original in zip(decoded["banks"], manifest["banks"], strict=True):
        page = tmp_path / f"chunk_{bank['index']}_page_0.html"
        assert page.is_file()
        assert "Finite point entries" in page.read_text(encoding="utf-8")
        for card in bank["cards"]:
            program = original["groups"][0]["cards"][card["card"]]["program"]
            assert [e["entry_step"] for e in card["entries"]] == [
                e["entry_step"] for e in program["entries"]
            ]
            for segment in program["segments"]:
                packed = (
                    tmp_path / card["directory"] / f"segment_{segment['id']}.bin"
                ).read_bytes()
                assert hashlib.sha256(packed).hexdigest() == segment["sha256"]
    pulse = decoded["points"][0]["groups"][0]["cards"]["A"]["pulses"][0]
    assert pulse["operation_path"][-1] == "rb_body"
    assert pulse["phase_evolution"]["carrier_mode"] == "epoch"
