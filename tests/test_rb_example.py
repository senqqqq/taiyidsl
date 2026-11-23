"""Complete offline RB construction with real Section/phase/chunk lowering."""

import json
import runpy
from pathlib import Path

import pytest

from taiyidsl import QubitOperations, Square, ns, play, qubit_operation
from taiyidsl.results.rb_results import joint_rb_counts


@pytest.fixture(scope="module")
def example():
    root = Path(__file__).resolve().parents[1]
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(root))
        yield runpy.run_path(
            str(root / "examples" / "artiq_spectrum" / "randomized_benchmarking.py")
        )


def program_signature(compiled):
    return [
        [
            (
                name,
                card["program"]["steps"],
                [s["sha256"] for s in card["program"]["segments"]],
            )
            for event in point.physical_events
            if event.kind == "spectrum_program"
            for name, card in event.params["cards"].items()
        ]
        for point in compiled.points
    ]


@pytest.mark.parametrize("num_qubits", [1, 2])
def test_rb_example_freezes_program_across_chunks_and_keeps_joint_readout(
    example, num_qubits
):
    arguments = {
        "num_qubits": num_qubits,
        "lengths": (0, 1, 3, 4),
        "sequence_count": 2,
        "shots": 4,
    }
    chunked = example["build_lab"](num_qubits).compile(
        example["build_experiment"](**arguments, chunks=2)
    )
    whole = example["build_lab"](num_qubits).compile(
        example["build_experiment"](**arguments, chunks=None)
    )
    assert chunked.metadata["rb_sequences"] == whole.metadata["rb_sequences"]
    assert program_signature(chunked) == program_signature(whole)
    assert chunked.metadata["physical_cz_verified"] is False
    assert chunked.metadata["cz_implementation"] == "demonstration"
    for point in chunked.points:
        acquisitions = [
            event for event in point.logical_events if event.kind == "acquire"
        ]
        assert len(acquisitions) == 1
        assert len(acquisitions[0].params["qubits"]) == num_qubits
        triggers = [
            event
            for event in point.physical_events
            if event.kind == "ttl_on" and event.signal.startswith("awg_trigger_")
        ]
        if point.coordinates["length"] == 0:
            assert not triggers
        else:
            assert {event.signal for event in triggers} == {
                "awg_trigger_a",
                "awg_trigger_b",
            }
            assert len({event.start_mu for event in triggers}) == 1
    results = example["synthetic_results"](chunked)
    counts = joint_rb_counts(
        results,
        "bits",
        qubits=tuple(chunked.qubits),
        variant_labels=example["VARIANTS"],
    )
    assert all(record.shots == record.denominator == 4 for record in counts)
    assert {record.variant for record in counts} == (
        {"reference"} if num_qubits == 1 else {"reference", "interleaved"}
    )
    assert (
        results.metadata["data_source"] == "synthetic_depolarizing_model_not_hardware"
    )


def test_two_qubit_cz_section_body_is_replaceable_without_changing_ideal_circuits(
    example,
):
    @qubit_operation
    def supplied_cz(q0, q1):
        play(q0.signal("clock"), Square(1600 * ns, amplitude=0.1, frequency=100e6))
        play(q1.signal("clock"), Square(1600 * ns, amplitude=0.1, frequency=110e6))

    options = {"lengths": (1,), "sequence_count": 1, "shots": 1, "chunks": None}
    original = example["build_experiment"](**options)
    replacement = example["build_experiment"](
        **options, operations=QubitOperations(cz_impl=supplied_cz)
    )
    assert original.metadata["rb_sequences"] == replacement.metadata["rb_sequences"]
    compiled = example["build_lab"]().compile(replacement)
    clocks = [
        event
        for point in compiled.points
        for event in point.logical_events
        if event.kind == "play" and event.signal.endswith(".clock")
    ]
    assert clocks
    assert all(
        event.params["requested_duration_s"] == pytest.approx(1600e-9)
        for event in clocks
    )
    assert compiled.metadata["physical_cz_verified"] is False


def test_one_qubit_example_report_exports_only_offline(example, tmp_path):
    compiled = example["build_lab"](1).compile(
        example["build_experiment"](
            num_qubits=1,
            lengths=(1,),
            sequence_count=1,
            shots=1,
            chunks=None,
        )
    )
    report = compiled.export_report(tmp_path)
    assert report["index"].is_file()
    assert compiled.metadata["hardware_verified"] is False


def test_rb_native_stream_paged_view_and_journal_resume_share_point_identity(
    example, tmp_path
):
    from examples.artiq_spectrum.rb_offline_run import run_offline_journal
    from taiyidsl import RunStore

    lab = example["build_lab"]()
    exp = example["build_experiment"](
        lengths=(0, 1), sequence_count=1, shots=2, chunks=2
    )
    eager = lab.compile(exp)
    package = eager.metadata["native_artiq"]["package"]
    assert set(package["components"]) == {"mot", "cooling", "state", "readout"}
    store = lab.compile_stream(exp, directory=tmp_path / "store")
    page = store.export_report(tmp_path / "viewer", waveform_mode="paged")
    assert page["index"].is_file()
    for chunk in store.manifest["chunks"]:
        loaded = store.load_chunk(chunk["index"])
        for local, global_point in enumerate(chunk["points"]):
            assert (
                program_signature(loaded)[local]
                == program_signature(eager)[global_point]
            )
        raw = example["synthetic_results"](loaded)
        expected = example["synthetic_results"](eager)
        for local, global_point in enumerate(chunk["points"]):
            assert (
                raw.data["bits"][local] == expected.data["bits"][global_point]
            ).all()
    summary = run_offline_journal(
        store,
        tmp_path / "run",
        lambda coordinates, shot: example["synthetic_observation"](
            coordinates, shot, num_qubits=2
        ),
    )
    assert summary["data_commits"] == len(eager.points) * eager.shots
    assert summary["new_mock_dispatches"] == summary["data_commits"]
    assert summary["dispatches_on_completed_resume"] == 0
    assert summary["hardware_executed"] is False
    journal = RunStore(tmp_path / "run")
    try:
        for event in journal.events():
            if event["stage"] == "data_committed":
                data = json.loads(
                    Path(event["payload"]["path"]).read_text(encoding="utf-8")
                )
                assert (
                    data["values"]["data_source"]
                    == "synthetic_depolarizing_model_not_hardware"
                )
    finally:
        journal.close()
