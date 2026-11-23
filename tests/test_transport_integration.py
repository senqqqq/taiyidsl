"""Three-card export/runtime contracts with an independent native-DDS SDK fake."""

import copy
import json
import runpy
from dataclasses import replace
from pathlib import Path

import pytest
from test_spectrum_contracts import _exported_bridge, _LifecycleCard
from test_spectrum_dds_runtime import fixture as dds_fixture

from taiyidsl.backends.artiq_spectrum.banks import entry_program, validate_bank
from taiyidsl.backends.artiq_spectrum.export import spectrum_manifest
from taiyidsl.runtime.spectrum.awg import SpectrumRunner, SpectrumRuntimeError


@pytest.fixture(scope="module")
def compiled_transport():
    example = runpy.run_path(
        str(Path(__file__).parents[1] / "examples/artiq_spectrum/transport_dds.py")
    )

    def compile_demo(label):
        lab, experiment = example["build_demo"]()
        config = lab.setup.spectrum
        group = config.groups["qubit_control"]
        cards = {**group.cards, "C": replace(group.cards["C"], serial_number="101")}
        lab.setup.spectrum = replace(
            config, groups={"qubit_control": replace(group, cards=cards)}
        )
        experiment.metadata["integration_run"] = label
        return lab.compile(experiment)

    return compile_demo("first"), compile_demo("replacement")


def _mixed_runner(manifest):
    """Only the injected independent SDK is exempt from measured calibration."""
    adapter, native_card, _, _ = dds_fixture()
    adapter.is_hardware = False
    log = []
    contracts = manifest["points"][0]["groups"][0]["cards"]
    cards = {
        name: _LifecycleCard(name, contracts[name]["contract"], log)
        for name in ("A", "B")
    }
    cards["C"] = adapter
    runner = SpectrumRunner(
        cards,
        allow_unverified=True,
        ready_timeout_s=0.1,
        completion_timeout_s=0.1,
    )
    return runner, cards, native_card, log


def _assert_hold(card, program):
    assert card.active["source"] == 2  # CARD: the autonomous timer has stopped.
    for tone in program["initial_tones"]:
        core = tone["core"]
        assert card.active[(core, "freq")] == tone["frequency_hz"]
        assert card.active[(core, "amp")] == tone["amplitude"] > 0
        assert card.active[(core, "freq_slope")] == 0
        assert card.active[(core, "amp_slope")] == 0
    assert card.log.count("start") == 1
    assert card.log.count("reset") == 1
    assert card.log.count("stop") == 1  # Initial configure only.


def test_mixed_manifest_roundtrip_bank_replacement_and_release_preserve_hold(
    compiled_transport,
):
    first, replacement = (
        json.loads(json.dumps(spectrum_manifest(compiled)))
        for compiled in compiled_transport
    )
    assert first["schema_version"] == 3
    assert first["engine"] == "mixed_awg_dds"
    assert first["compiled_sha256"] != replacement["compiled_sha256"]
    point_cards = first["points"][0]["groups"][0]["cards"]
    bank_cards = first["banks"][0]["groups"][0]["cards"]
    assert bank_cards["C"]["program"]["kind"] == "dds_bank"
    assert "segments" not in bank_cards["C"]["program"]
    for name, item in bank_cards.items():
        assert validate_bank(item["program"])["status"] == "passed"
        assert entry_program(item["program"], 0) == point_cards[name]["program"]

    runner, cards, native_card, log = _mixed_runner(first)
    program = point_cards["C"]["program"]

    def execute(point):
        assert point == 0
        assert cards["A"].armed and cards["B"].armed and cards["C"].ready()
        native_card.run_shot()
        return "kernel_complete"

    try:
        receipts = [runner.run_point(first, 0, execute) for _ in range(2)]
        assert [receipt["uploaded"] for receipt in receipts] == [True, False]
        assert all(receipt["status"] == "complete" for receipt in receipts)
        assert all(
            receipt["transport_state"] == "planned_initial_hold"
            and receipt["dds_output"] == "continuous_until_session_close"
            for receipt in receipts
        )
        _assert_hold(native_card, program)

        assert runner.run_point(replacement, 0, execute)["uploaded"]
        for name in ("A", "B"):
            assert log.count((name, "upload")) == 2
            assert log.count((name, "stop")) == 1
        _assert_hold(native_card, program)

        runner.release_awg_banks()
        assert runner.transport_state == "planned_initial_hold"
        assert not cards["A"].armed and not cards["B"].armed
        _assert_hold(native_card, program)
        assert runner.run_point(replacement, 0, execute)["uploaded"]
        _assert_hold(native_card, program)
        assert native_card.dds.counter == 1 + 4 * program["execution_count"]
        # Importing a legacy AWG-only manifest must not release existing traps.
        legacy_point = copy.deepcopy(first["points"][0])
        legacy_point["groups"][0]["cards"].pop("C")
        runner.run_point(
            {"schema_version": 1, "points": [legacy_point]},
            0,
            lambda _: "awg_only_kernel",
        )
        _assert_hold(native_card, program)
        assert native_card.dds.counter == 1 + 4 * program["execution_count"]
    finally:
        runner.invalidate()
        cards["C"].close()
    assert native_card.log[-2:] == ["stop", "close"]


def test_exported_host_bridge_returns_with_live_hold_and_aborts_kernel_failure(
    compiled_transport, tmp_path
):
    instance = _exported_bridge(compiled_transport[0], tmp_path)
    manifest = instance.spectrum_manifest
    assert manifest["schema_version"] == 3
    assert manifest["verification"]["offline"] == "passed"
    runner, cards, native_card, log = _mixed_runner(manifest)
    program = manifest["points"][0]["groups"][0]["cards"]["C"]["program"]
    instance.spectrum_runner = runner
    instance._validate_target = instance.initialize_devices = lambda: None
    completed = []
    finished = []

    class Results:
        def before_shot(self, point, shot):
            assert not cards["A"].armed and not cards["B"].armed

        def after_shot(self, point, shot):
            assert not cards["A"].armed and not cards["B"].armed
            _assert_hold(native_card, program)
            completed.append((point, shot))

        def finish(self):
            _assert_hold(native_card, program)
            finished.append(True)

    def execute(point):
        assert point == 0
        assert cards["A"].armed and cards["B"].armed and cards["C"].ready()
        native_card.run_shot()

    instance.result_adapter = Results()
    instance._execute_point = execute
    try:
        instance.run()
        assert completed == list(compiled_transport[0].execution_order())
        assert len(completed) == 2
        assert finished == [True]
        assert len(instance.spectrum_receipts) == 2
        _assert_hold(native_card, program)

        def failed_kernel(point):
            assert cards["C"].ready()
            native_card.advance()  # Failure after the move sequence has started.
            raise RuntimeError("injected transport kernel failure")

        instance._execute_point = failed_kernel
        with pytest.raises(RuntimeError, match="injected transport kernel failure"):
            instance.run()
        assert runner.transport_state == "unknown"
        assert native_card.log[-1] == "stop"
        assert all((name, "stop") in log for name in ("A", "B"))
        assert not cards["A"].armed and not cards["B"].armed
        assert len(instance.spectrum_receipts) == 2
        assert len(completed) == 2 and finished == [True]
        native_log, awg_log = native_card.log.copy(), log.copy()
        with pytest.raises(SpectrumRuntimeError, match="unknown"):
            runner.run_point(manifest, 0, lambda _: pytest.fail("Retried the kernel"))
        assert native_card.log == native_log and log == awg_log
    finally:
        runner.invalidate()
        cards["C"].close()
