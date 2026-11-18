"""DDS packets survive streamed chunk storage without becoming AWG segments."""

import json
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from test_transport_integration import _assert_hold, _mixed_runner

from taiyidsl import Experiment, Square, SweepParameter, move, play, us
from taiyidsl.run_store import RunStore
from taiyidsl.stream_runtime import StreamingRunner
from taiyidsl.streaming import CompilationStore


def test_streamed_transport_reopens_switches_chunks_and_returns_with_hold(tmp_path):
    example = runpy.run_path(
        str(Path(__file__).parents[1] / "examples/artiq_spectrum/transport_dds.py")
    )
    lab, q0, q1, iz = example["build_lab"]()
    config = lab.setup.spectrum
    group = config.groups["qubit_control"]
    lab.setup.spectrum = replace(
        config,
        groups={
            "qubit_control": replace(
                group,
                cards={
                    **group.cards,
                    "C": replace(group.cards["C"], serial_number="101"),
                },
            )
        },
    )
    duration = SweepParameter("travel", [100, 200], "us")
    rf = Square(100 * us, amplitude=0.2)
    exp = Experiment("streamed_transport", [q0, q1])
    with exp.average(2), exp.sweep(duration, chunks=2), exp.realtime():
        with exp.triggered_sequence(group="qubit_control"):
            play(q0.rf, rf)
            move([q0, q1], to=[iz[0], iz[1]], duration=duration)
            move([q0, q1], to=[q0.position, q1.position], duration=duration)
    artifact = lab.compile_stream(exp, directory=tmp_path / "artifact")
    reopened = CompilationStore.open(artifact.directory)
    manifests = []
    for chunk in reopened.manifest["chunks"]:
        reopened.load_chunk(chunk["index"]).assert_integrity()
        manifest = json.loads(
            (reopened.directory / chunk["directory"] / "manifest.json").read_text()
        )
        manifests.append(manifest)
        assert manifest["schema_version"] == 3
        assert (
            manifest["banks"][0]["groups"][0]["cards"]["C"]["program"]["kind"]
            == "dds_bank"
        )
    assert len(manifests) == 2
    runner, cards, native_card, log = _mixed_runner(manifests[0])
    program = manifests[0]["points"][0]["groups"][0]["cards"]["C"]["program"]

    def factory(path, context):
        return SimpleNamespace(
            _validate_target=lambda: None,
            initialize_devices=lambda: None,
            _execute_point=lambda point: native_card.run_shot(),
        )

    class Detector:
        def after_shot(self, point, shot):
            _assert_hold(native_card, program)
            return {"point": point, "shot": shot}

    store = RunStore(tmp_path / "run")
    try:
        StreamingRunner(runner, kernel_factory=factory).run(
            reopened, result_adapter=Detector(), run_store=store
        )
        _assert_hold(native_card, program)
        assert runner.transport_state == "planned_initial_hold"
        assert log.count(("A", "upload")) == log.count(("B", "upload")) == 4
        assert log.count(("A", "stop")) == log.count(("B", "stop")) == 4
        assert len([e for e in store.events() if e["stage"] == "data_committed"]) == 4
    finally:
        store.close()
        runner.invalidate()
        cards["C"].close()
