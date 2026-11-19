"""Resident banks preserve each point's finite playback and reuse exact bytes."""

import copy
import json

import numpy as np
import pytest
from test_spectrum_runtime import FakeAdapter, FakeSdk, contract, program

from taiyidsl.backends.artiq_spectrum.awg import build_card_program, reconstruct_window
from taiyidsl.backends.artiq_spectrum.banks import (
    build_bank,
    build_manifest_banks,
    entry_program,
    validate_bank,
)
from taiyidsl.ir import CompileError
from taiyidsl.runtime.spectrum.awg import (
    SegmentCache,
    SpcmAdapter,
    SpectrumRunner,
    SpectrumRuntimeError,
)


@pytest.mark.parametrize("shared,count", [(False, 2), (True, 1)])
def test_sequence_report_counts_distinct_trigger_lines(tmp_path, shared, count):
    from test_spectrum_trigger_routes import dual_lab, experiment

    exported = dual_lab(shared=shared).compile(experiment()).export(tmp_path)
    report = json.loads(exported["report"].read_text(encoding="utf-8"))
    assert report["points"][0]["trigger_pulses"] == count


@pytest.mark.parametrize("scope,loads", [("global", 4), ("chunk", 2)])
def test_exported_chunk_bridge_executes_the_shared_plan(tmp_path, scope, loads):
    from test_spectrum_backend import lab
    from test_spectrum_contracts import _bridge_cards, _exported_bridge

    from taiyidsl import Experiment, Qubit, Square, SweepParameter, ns, trigplay

    q = Qubit("q")
    exp = Experiment("resident_chunks", [q])
    amplitude = SweepParameter("amplitude", [0.1, 0.2, 0.3, 0.4], "dimensionless")
    with exp.average(2, scope=scope), exp.sweep(amplitude, chunks=2), exp.realtime():
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(400 * ns, frequency=30_000, amplitude=amplitude))
    compiled = lab().compile(exp)
    instance = _exported_bridge(compiled, tmp_path)
    log = []
    cards = _bridge_cards(instance, log)
    instance.spectrum_runner = SpectrumRunner(cards, allow_unverified=True)
    instance._validate_target = instance.initialize_devices = lambda: None
    observed = []

    class Results:
        def before_shot(self, point, shot):
            observed.append((point, shot))

        def after_shot(self, point, shot):
            assert not any(card.armed for card in cards.values())

        def finish(self):
            pass

    instance.result_adapter = Results()
    instance._execute_point = lambda point: None
    instance.run()
    assert observed == list(compiled.execution_order())
    assert sum(r["receipt"]["uploaded"] for r in instance.spectrum_receipts) == loads
    assert all(sum(item == (name, "upload") for item in log) == loads for name in cards)
    for bank in instance.spectrum_manifest["banks"]:
        for group in bank["groups"]:
            for name, item in group["cards"].items():
                for point in bank["points"]:
                    original = instance.spectrum_manifest["points"][point]["groups"][0][
                        "cards"
                    ][name]["program"]
                    assert entry_program(item["program"], point) == original


def test_shared_samples_and_finite_entries_preserve_final_dac_codes():
    source = program(contract())
    other = copy.deepcopy(source)
    bank = build_bank({0: source, 1: other})
    assert len(bank["segments"]) == len(source["segments"])
    assert len(bank["steps"]) == 2 * len(source["steps"])
    assert bank["entries"][1]["entry_step"] > 0
    for point in (0, 1):
        restored = entry_program(bank, point)
        assert restored == source
        np.testing.assert_array_equal(
            reconstruct_window(restored, 0, restored["physical_duration_samples"]),
            reconstruct_window(source, 0, source["physical_duration_samples"]),
        )
    bad = copy.deepcopy(bank)
    bad["steps"][0]["next_step"] = bank["entries"][1]["entry_step"]
    with pytest.raises(CompileError, match="escapes"):
        validate_bank(bad)


def test_byte_identical_recipes_keep_each_points_source_provenance():
    first = program(contract())
    second = copy.deepcopy(first)
    second["pulses"][0]["source"] = "other operation, same physical samples"
    for segment in second["segments"]:
        for fragment in segment["recipe"].get("fragments", []):
            fragment["pulse"]["source"] = second["pulses"][0]["source"]
    bank = build_bank({0: first, 1: second})
    assert len(bank["segments"]) == len(first["segments"])
    assert entry_program(bank, 0) == first
    assert entry_program(bank, 1) == second
    bad = copy.deepcopy(bank)
    bad["segments"][0]["recipe"] = {"kind": "zero"}
    with pytest.raises(CompileError, match="bank packed segment hash"):
        validate_bank(bad)


def test_bank_checks_joint_step_and_equal_slot_memory_limits():
    source = program({**contract(), "max_steps": 1})
    with pytest.raises(CompileError, match="step limit"):
        build_bank({0: source, 1: source})
    spec = {**contract(), "memory_bytes": 4096}
    programs = {
        index: build_card_program(
            spec,
            [
                {
                    "channel": 0,
                    "start_sample": 0,
                    "duration_samples": 500,
                    "frequency_hz": 100e6,
                    "amplitude": 0.2 + 0.1 * index,
                    "phase_rad": 0.0,
                }
            ],
            500,
        )
        for index in range(3)
    }
    with pytest.raises(CompileError, match="slot capacity"):
        build_bank(programs)


def test_compile_preflights_joint_chunk_capacity_without_replaying_samples(monkeypatch):
    from dataclasses import replace

    from test_spectrum_backend import lab

    from taiyidsl import Experiment, Qubit, Square, SweepParameter, ns, trigplay
    from taiyidsl.backends.artiq_spectrum.banks import validate_chunk_capacity

    profile = lab(memory_bytes=8192)
    config = profile.setup.spectrum
    group = config.groups["g"]
    group = replace(
        group, cards={**group.cards, "A": replace(group.cards["A"], memory_bytes=4096)}
    )
    profile.setup.spectrum = replace(config, groups={"g": group})
    q = Qubit("q")
    amplitude = SweepParameter("amplitude", [0.1, 0.2, 0.3], "dimensionless")

    def experiment(chunks):
        exp = Experiment("bank_capacity", [q])
        with exp.average(1), exp.sweep(amplitude, chunks=chunks), exp.realtime():
            with exp.triggered_sequence(group="g"):
                trigplay(q.rf, Square(400 * ns, frequency=30_000, amplitude=amplitude))
        return exp

    independent = profile.compile(experiment(None))
    with pytest.raises(CompileError, match="Chunk 0, card A.*slot capacity"):
        profile.compile(experiment(1))
    monkeypatch.setattr(
        "taiyidsl.backends.artiq_spectrum.banks.materialize_segment",
        lambda *args: pytest.fail("Budget check rendered samples"),
    )
    validate_chunk_capacity(independent.points, independent.host_execution_plan)


class ResidentAdapter(FakeAdapter):
    def select_entry(self, step):
        self._call("select")
        self.log.append((self.name, "entry", step))


def resident_fixture(*, chunks=None, empty=False):
    log = []
    specs = {"a": contract("101"), "b": contract("102")}
    adapters = {name: ResidentAdapter(name, spec, log) for name, spec in specs.items()}
    groups = [
        {
            "name": "g",
            "calibration": {},
            "cards": {
                name: {"contract": spec, "program": program(spec)}
                for name, spec in specs.items()
            },
        }
    ]
    points = [
        {
            "index": point,
            "groups": copy.deepcopy(groups) if not (empty and point == 1) else [],
        }
        for point in range(2)
    ]
    plan = {"chunks": chunks or [{"index": 0, "points": [0, 1]}]}
    manifest = {
        "schema_version": 2,
        "compiled_sha256": "test",
        "points": points,
        "banks": build_manifest_banks(points, plan),
    }
    return log, adapters, manifest


def test_resident_points_select_entries_without_repeating_dma():
    log, adapters, manifest = resident_fixture()
    runner = SpectrumRunner(adapters, allow_unverified=True)
    receipts = [
        runner.run_point(manifest, point, lambda p: log.append((p, "artiq")))
        for point in (0, 1, 0)
    ]
    assert [r["uploaded"] for r in receipts] == [True, False, False]
    assert sum(item == ("a", "upload") for item in log) == 1
    assert sum(item == ("b", "upload") for item in log) == 1
    assert [r["cards"]["a"]["entry_step"] for r in receipts] == [0, 1, 0]
    launches = [i for i, item in enumerate(log) if item[-1] == "artiq"]
    start = 0
    for launch in launches:
        assert all((name, "ready") in log[start:launch] for name in adapters)
        start = launch + 1


def test_modified_point_is_not_silently_replayed_from_an_old_resident_bank():
    log, adapters, manifest = resident_fixture()
    runner = SpectrumRunner(adapters, allow_unverified=True)
    runner.run_point(manifest, 0, lambda p: None)
    manifest["points"][1]["groups"][0]["cards"]["a"]["program"]["pulses"][0][
        "phase_rad"
    ] += 0.1
    with pytest.raises(SpectrumRuntimeError, match="differs"):
        runner.run_point(
            manifest, 1, lambda p: pytest.fail("Changed point was launched")
        )
    assert runner._resident_key is None
    assert log[-2:] == [("b", "stop"), ("a", "stop")]


@pytest.mark.parametrize(
    "failure", ["configure", "upload", "select", "arm", "ready", "complete"]
)
def test_bank_failure_stops_all_touched_cards_and_invalidates_residency(failure):
    log, adapters, manifest = resident_fixture()
    runner = SpectrumRunner(adapters, allow_unverified=True)
    adapters["b"].failure = failure
    with pytest.raises(SpectrumRuntimeError, match=failure):
        runner.run_point(manifest, 0, lambda p: log.append((p, "artiq")))
    assert runner._resident_key is None
    assert all((name, "stop") in log for name in adapters)
    if failure != "complete":
        assert (0, "artiq") not in log
    adapters["b"].failure = None
    receipt = runner.run_point(manifest, 0, lambda p: None)
    assert receipt["uploaded"] is True


def test_empty_point_keeps_stopped_bank_and_switch_reloads_only_at_boundary():
    log, adapters, manifest = resident_fixture(empty=True)
    runner = SpectrumRunner(adapters, allow_unverified=True)
    runner.run_point(manifest, 0, lambda p: None)
    before = list(log)
    receipt = runner.run_point(manifest, 1, lambda p: None)
    assert receipt["cards"] == {} and log == before
    assert runner.run_point(manifest, 0, lambda p: None)["uploaded"] is False
    log, adapters, manifest = resident_fixture(
        chunks=[{"index": 0, "points": [0]}, {"index": 1, "points": [1]}]
    )
    runner = SpectrumRunner(adapters, allow_unverified=True)
    assert all(
        runner.run_point(manifest, point, lambda p: None)["uploaded"]
        for point in (0, 1, 0)
    )


def test_sdk_resident_entry_selection_and_rearm_do_not_dma_again():
    sdk = FakeSdk()
    adapter = SpcmAdapter("101", sdk=sdk)
    spec = contract()
    bank = build_bank({0: program(spec), 1: program(spec)})
    adapter.configure(spec)
    adapter.upload(bank, SegmentCache().load)
    initial = len([item for item in sdk.log if item[0] == "dma"])
    for point in (1, 0):
        entry = bank["entries"][point]["entry_step"]
        adapter.select_entry(entry)
        adapter.arm()
        sdk.card.state = sdk.M2STAT_CARD_TRIGGER
        adapter.wait_complete(1)
    assert len([item for item in sdk.log if item[0] == "dma"]) == initial
    assert ("sequence", "start_step", (1,), {}) in sdk.log
