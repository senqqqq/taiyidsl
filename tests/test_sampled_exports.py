"""Reopen public sampled-pulse exports and recover their independently predicted RF."""

import copy
import json
from contextlib import nullcontext
from dataclasses import replace

import numpy as np
import pytest
from test_spectrum_backend import lab as hybrid_lab
from test_standalone_spectrum_backend import card_lab

from taiyidsl import (
    CompilationStore,
    Experiment,
    Pulse,
    Qubit,
    SweepParameter,
    ns,
    play,
)
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.backends.artiq_spectrum.banks import entry_program
from taiyidsl.compiler.store import restore_compiled
from taiyidsl.sample_assets import expand_sample_manifest

SAMPLES = [0.125, -0.625, 0.25, 0.875, -0.75, -0.125, 0.625, 0.375]
AMPLITUDES = (0.25, 0.75)


def sampled_scan(hybrid=False):
    q = Qubit("q")
    exp = Experiment("sampled_exports", [q])
    source = Pulse.from_array(
        SAMPLES, kind="waveform", sample_interval=ns, resample="linear"
    )
    amp = SweepParameter("amplitude", AMPLITUDES, "dimensionless")
    with exp.average(2), exp.sweep(amp, chunks=2 if hybrid else None), exp.realtime():
        with exp.triggered_sequence(group="g") if hybrid else nullcontext():
            with exp.repeat(2):
                play(q.rf, source, amplitude=amp)
    return exp, source


def verify_pulses(program, amplitude):
    # Source samples at 0,1,...,7 ns become DAC samples at 0,0.8,...,7.2 ns.
    # The right-hand interval holds the last source sample until the declared end.
    target_times = np.arange(10) * 0.8
    interpolated = np.interp(target_times, np.arange(len(SAMPLES)), SAMPLES)
    expected = np.rint(np.tile(interpolated, 2) * amplitude * 32767).astype("<i2")
    first, second = program["pulses"]
    start = first["start_sample"]
    assert second["start_sample"] == start + 10
    np.testing.assert_array_equal(
        reconstruct_window(program, start, start + 20)[:, 0], expected
    )


def load_single_source(path, pulse):
    text = path.read_text(encoding="utf-8")
    assert text.count(pulse.sample_asset()["data"]) == 1
    data = json.loads(text)
    assert data["sample_assets"] == {pulse.sample_digest: pulse.sample_asset()}
    return data


def test_standalone_scans_roundtrip_compiled_and_manifest(tmp_path):
    lab = card_lab()
    exp, source = sampled_scan()
    compiled = lab.compile(exp)
    exports = compiled.export(tmp_path)
    packed = load_single_source(exports["manifest"], source)
    expanded = expand_sample_manifest(copy.deepcopy(packed))
    serialized = load_single_source(exports["plan"], source)
    restored = restore_compiled(serialized, lab.to_dict())
    assert json.dumps(serialized).count(source.sample_asset()["data"]) == 1
    assert restored.sha256 == compiled.sha256
    assert list(restored.execution_order()) == list(compiled.execution_order())
    for index, amplitude in enumerate(AMPLITUDES):
        verify_pulses(expanded["points"][index]["program"], amplitude)
        (shot,) = restored.points[index].physical_events
        verify_pulses(shot.params["program"], amplitude)

    invalid = copy.deepcopy(packed)
    invalid["sample_assets"][source.sample_digest]["data"] = Pulse.from_array(
        SAMPLES[::-1], kind="waveform", sample_interval=ns
    ).sample_asset()["data"]
    with pytest.raises(ValueError, match="digest"):
        program = expand_sample_manifest(invalid)["points"][0]["program"]
        reconstruct_window(program, 0, 10)


def test_hybrid_banks_and_streamed_chunks_replay_sample_assets(tmp_path):
    lab = hybrid_lab()
    exp, source = sampled_scan(hybrid=True)
    compiled = lab.compile(exp)
    files = compiled.export(tmp_path / "export")
    manifest = expand_sample_manifest(load_single_source(files["manifest"], source))
    for bank in manifest["banks"]:
        program = bank["groups"][0]["cards"]["A"]["program"]
        for index in bank["points"]:
            verify_pulses(entry_program(program, index), AMPLITUDES[index])
    for index, point in enumerate(manifest["points"]):
        verify_pulses(point["groups"][0]["cards"]["A"]["program"], AMPLITUDES[index])

    directory = tmp_path / "store"
    lab.compile_stream(exp, directory=directory)
    store = CompilationStore.open(directory)
    assert list(store.execution_order()) == list(compiled.execution_order())
    for row in store.manifest["chunks"]:
        chunk = store.load_chunk(row["index"])
        chunk.assert_integrity()
        for local, global_index in enumerate(row["points"]):
            group = next(
                e
                for e in chunk.points[local].physical_events
                if e.kind == "spectrum_program"
            )
            verify_pulses(
                group.params["cards"]["A"]["program"], AMPLITUDES[global_index]
            )


def test_streamed_source_can_span_many_small_render_tiles(tmp_path):
    lab = hybrid_lab()
    config = lab.setup.spectrum
    lab.setup.spectrum = replace(
        config, groups={"g": replace(config.groups["g"], max_tile_samples=512)}
    )
    q = Qubit("q")
    exp = Experiment("small_tiles", [q])
    samples = np.linspace(-0.75, 0.75, 12000)
    pulse = Pulse.from_array(samples, kind="waveform", sample_interval=0.8 * ns)
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        play(q.rf, pulse)
    store = lab.compile_stream(exp, directory=tmp_path, host_memory_budget_bytes=2**20)
    point = store.load_chunk(0).points[0]
    group = next(e for e in point.physical_events if e.kind == "spectrum_program")
    program = group.params["cards"]["A"]["program"]
    start = program["pulses"][0]["start_sample"]
    np.testing.assert_array_equal(
        reconstruct_window(program, start, start + len(samples))[:, 0],
        np.rint(32767 * samples),
    )
