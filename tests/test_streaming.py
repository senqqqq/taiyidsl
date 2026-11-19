"""Streamed artifacts preserve eager point semantics and reject partial stores."""

import json
from dataclasses import replace

import numpy as np
import pytest
from test_spectrum_backend import lab

from taiyidsl import Experiment, Qubit, Square, SweepParameter, ns, play
from taiyidsl.compiler.store import CompilationStore
from taiyidsl.ir import CompileError


def scan(chunks=2, scope="global"):
    q = Qubit("q")
    exp = Experiment("stream", [q])
    value = SweepParameter("amplitude", [0.1, 0.2, 0.3, 0.4], "dimensionless")
    with exp.average(2, scope=scope), exp.sweep(value, chunks=chunks), exp.realtime():
        with exp.triggered_sequence(group="g"):
            play(q.rf, Square(400 * ns, frequency=11e6, amplitude=value))
    return exp


@pytest.mark.parametrize("scope", ["global", "chunk"])
def test_streamed_chunks_reopen_and_match_eager_output(tmp_path, scope):
    profile = lab()
    exp = scan(scope=scope)
    eager = profile.compile(exp)
    store = profile.compile_stream(exp, directory=tmp_path / "store")
    reopened = CompilationStore.open(store.directory)
    assert not hasattr(store, "points")
    assert list(reopened.execution_order()) == list(eager.execution_order())
    for entry in store.manifest["chunks"]:
        chunk = reopened.load_chunk(entry["index"])
        chunk.assert_integrity()
        for local, global_index in enumerate(entry["points"]):
            assert (
                chunk.points[local].coordinates
                == eager.points[global_index].coordinates
            )
            left = chunk.simulate_output(local, 0, 300e-9)
            right = eager.simulate_output(global_index, 0, 300e-9)
            for a, b in zip(left["traces"], right["traces"], strict=True):
                np.testing.assert_array_equal(a["dac_codes"], b["dac_codes"])
    path = store.directory / "points" / "0.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(CompileError, match="changed"):
        CompilationStore.open(store.directory)


def test_budget_failure_never_publishes_complete_store(tmp_path):
    with pytest.raises(CompileError, match="budget"):
        lab().compile_stream(scan(), directory=tmp_path, host_memory_budget_bytes=1)
    assert not (tmp_path / "store.json").exists()
    with pytest.raises(CompileError, match="incomplete"):
        CompilationStore.open(tmp_path)


def test_auto_host_budget_splits_at_axis_values(tmp_path):
    profile = lab()
    exp = scan(chunks="auto")
    initial = profile.compile_stream(exp, directory=tmp_path / "initial")
    sizes = [
        len((initial.directory / "points" / f"{i}.json").read_bytes()) for i in range(4)
    ]
    # Two native minimum tiles on the widest card plus two point records.
    budget = 768 * 2 * 128 + max(sizes) * 2
    split = profile.compile_stream(
        exp, directory=tmp_path / "split", host_memory_budget_bytes=budget
    )
    assert [len(c["points"]) for c in split.manifest["chunks"]] == [2, 2]
    assert split.manifest["managed_peak_bytes"] <= budget
    assert split.manifest["chunks"][0]["boundary_reason"] == "managed_host_budget"
    assert (
        json.loads((split.directory / "store.json").read_text())["status"] == "complete"
    )


@pytest.mark.parametrize("amplitude", [0.2, 1e-10])
def test_sample_workspace_bounds_long_single_shot_and_persists_unique_bytes(
    tmp_path, monkeypatch, amplitude
):
    import hashlib

    import taiyidsl.backends.artiq_spectrum.awg as waveforms
    from taiyidsl.compiler.store import ArtifactSegments

    q = Qubit("q")
    exp = Experiment("long_tile", [q])
    repeat = SweepParameter("repeat", [0, 1], "dimensionless")
    with exp.average(1), exp.sweep(repeat, chunks=2), exp.realtime():
        with exp.triggered_sequence(group="g"):
            play(q.rf, Square(10000 * ns, frequency=11e6, amplitude=amplitude))
    largest = 0
    original = waveforms._render

    def observed(recipe, samples, card):
        nonlocal largest
        largest = max(largest, samples * len(card["active_channels"]) * 128)
        return original(recipe, samples, card)

    monkeypatch.setattr(waveforms, "_render", observed)
    store = lab().compile_stream(
        exp, directory=tmp_path / "store", host_memory_budget_bytes=512 * 1024
    )
    assert 0 < largest <= store.manifest["sample_workspace_bytes"]
    assert store.manifest["managed_peak_bytes"] <= 512 * 1024
    first, second = store.load_chunk(0), store.load_chunk(1)
    p0 = next(
        e.params
        for e in first.points[0].physical_events
        if e.kind == "spectrum_program"
    )
    p1 = next(
        e.params
        for e in second.points[0].physical_events
        if e.kind == "spectrum_program"
    )
    assert p0["cards"]["A"]["program"] == p1["cards"]["A"]["program"]
    hashes = {
        s["sha256"]
        for card in p0["cards"].values()
        for s in card["program"]["segments"]
    }
    assert set(store.manifest["segments"]) == hashes
    assert len(list((store.directory / "segments").glob("*.bin"))) == len(hashes)
    monkeypatch.setattr(
        waveforms,
        "materialize_segment",
        lambda *args: pytest.fail("Runtime re-rendered compiled samples"),
    )
    loader = ArtifactSegments(store)
    for card in p0["cards"].values():
        for segment in card["program"]["segments"]:
            values = loader.load(card["program"], segment["id"])
            assert hashlib.sha256(values.tobytes()).hexdigest() == segment["sha256"]


def test_renderer_rejects_work_buffer_before_allocating(monkeypatch):
    import taiyidsl.backends.artiq_spectrum.awg as waveforms

    monkeypatch.setattr(
        waveforms.np,
        "zeros",
        lambda *args, **kwargs: pytest.fail("Allocated over-budget array"),
    )
    with waveforms.render_work_budget(1024):
        with pytest.raises(CompileError, match="sample work budget"):
            waveforms._render({"kind": "zero"}, 384, {"active_channels": [0]})


@pytest.mark.parametrize("constraint", ["memory_bytes", "max_steps"])
def test_auto_respects_real_card_slots_and_steps(tmp_path, constraint, monkeypatch):
    profile = lab(memory_bytes=8192)
    config = profile.setup.spectrum
    group = config.groups["g"]
    card = replace(group.cards["A"], memory_bytes=4096)
    if constraint == "max_steps":
        card = group.cards["A"]
        monkeypatch.setattr("taiyidsl.backends.artiq_spectrum.awg._MAX_STEPS", 2)
    profile.setup.spectrum = replace(
        config, groups={"g": replace(group, cards={**group.cards, "A": card})}
    )
    eager = profile.compile(scan(chunks="auto"))
    store = profile.compile_stream(scan(chunks="auto"), directory=tmp_path / "store")
    assert len(store.manifest["chunks"]) > 1
    assert store.host_execution_plan["chunks"] == eager.host_execution_plan["chunks"]
    with pytest.raises(CompileError, match="capacity|step"):
        profile.compile(scan(chunks=1))


def test_auto_keeps_inner_product_atomic_and_outer_prefix_separate(
    tmp_path, monkeypatch
):
    q = Qubit("q")
    exp = Experiment("nested_stream", [q])
    outer = SweepParameter("outer", [0, 1])
    middle = SweepParameter("amplitude", [0.1, 0.2, 0.3], "dimensionless")
    inner = SweepParameter("frequency", [11e6, 12e6], "Hz")
    with exp.average(1), exp.sweep(outer), exp.sweep(middle, chunks="auto"):
        with exp.sweep(inner), exp.realtime(), exp.triggered_sequence(group="g"):
            play(q.rf, Square(400 * ns, frequency=inner, amplitude=middle))
    monkeypatch.setattr("taiyidsl.backends.artiq_spectrum.awg._MAX_STEPS", 3)
    store = lab().compile_stream(exp, directory=tmp_path / "store")
    assert [chunk["points"] for chunk in store.manifest["chunks"]] == [
        [0, 1],
        [2, 3],
        [4, 5],
        [6, 7],
        [8, 9],
        [10, 11],
    ]
    monkeypatch.setattr("taiyidsl.backends.artiq_spectrum.awg._MAX_STEPS", 1)
    with pytest.raises(CompileError, match="One auto axis value"):
        lab().compile_stream(exp, directory=tmp_path / "too_small")
    assert not (tmp_path / "too_small/store.json").exists()


def test_paged_viewer_shards_decode_exact_final_dac(tmp_path):
    import shutil
    import subprocess

    from test_output_simulator import _node

    store = lab().compile_stream(scan(), directory=tmp_path / "store")
    exported = store.export_report(tmp_path / "viewer")
    output = exported["index"].parent
    assert 'src="paged.js"' in exported["index"].read_text(encoding="utf-8")
    assert "fetch(" not in (output / "paged.js").read_text(encoding="utf-8")
    if shutil.which("node"):
        subprocess.run(["node", "--check", str(output / "paged.js")], check=True)

    def shard(path):
        source = path.read_text(encoding="utf-8")
        return json.loads(source.split(",", 1)[1].rsplit(");", 1)[0])

    payload = shard(output / "data/chunk_1.js")
    assert [point["index"] for point in payload["points"]] == [2, 3]
    payload["packed"] = {
        path.stem.removeprefix("segment_"): shard(path)
        for path in (output / "data").glob("segment_*.js")
    }
    actual = _node(
        payload,
        [
            {
                "point": 0,
                "card": 0,
                "channel": 0,
                "start_s": "0",
                "duration_s": "0.0000003",
            }
        ],
    )
    assert actual.returncode == 0, actual.stderr
    expected = store.load_chunk(1).simulate_output(0, 0, 300e-9)["traces"][0]
    np.testing.assert_array_equal(
        json.loads(actual.stdout)[0]["codes"], expected["dac_codes"]
    )
    # The small metadata shard references packed bytes; it embeds no RF recipe renderer.
    assert all(
        "data" not in segment
        for segment in payload["points"][0]["cards"][0]["segments"]
    )
