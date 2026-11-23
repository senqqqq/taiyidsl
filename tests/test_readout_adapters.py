"""Lab adapter examples preserve image identity and safe attempt recovery."""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.fixture(autouse=True)
def lab_package(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "examples/lab_extension/src"))


def test_plain_adapter_keeps_empty_and_missing_frames_distinct(tmp_path):
    from my_lab.camera import CameraAdapter, FakeCamera
    from my_lab.configuration import image_results

    from taiyidsl.runtime.readout import ResultAdapter

    images = image_results(shots=3)
    camera = FakeCamera(
        {
            (0, 0): np.full((2, 2), 5, np.uint16),
            (0, 1): np.ones((2, 2), np.uint16),
            (0, 2): None,
        }
    )
    adapter: ResultAdapter = CameraAdapter(camera, images, tmp_path / "images.h5")
    for shot in range(3):
        adapter.before_shot(0, shot)
        camera.trigger()
        adapter.after_shot(0, shot)
    adapter.finish()
    values = images.results.data["readout"][0, :, 0]
    np.testing.assert_equal(values, [1, 0, np.nan])
    assert images.results.status["readout"][0, :, 0].tolist() == [
        "valid",
        "valid",
        "missing",
    ]
    assert len(images.frames) == 2
    assert camera.trigger_count == 3
    assert (tmp_path / "images.h5").is_file()


def test_camera_requires_a_trigger_between_arm_and_read():
    from my_lab.camera import FakeCamera

    camera = FakeCamera({(0, 0): np.ones((2, 2), np.uint16)})
    camera.arm(0, 0)
    with pytest.raises(RuntimeError, match="trigger"):
        camera.read()
    camera.trigger()
    camera.read()
    with pytest.raises(RuntimeError, match="arm"):
        camera.trigger()


def test_persistent_adapter_recovers_saved_frame_without_retrigger(
    tmp_path, monkeypatch
):
    from my_lab.camera import FakeCamera, PersistentCameraAdapter
    from my_lab.configuration import image_results
    from test_spectrum_backend import lab
    from test_streaming import scan

    from taiyidsl import RunStore, StreamingRunner
    from taiyidsl.runtime.readout import (
        PersistentResultAdapter,
        RecoverableResultAdapter,
    )

    artifact = lab().compile_stream(scan(), directory=tmp_path / "compiled")
    order = list(artifact.execution_order())
    camera = FakeCamera({pair: np.full((2, 2), 5, np.uint16) for pair in order})
    images = image_results(points=4, shots=2)
    adapter: PersistentResultAdapter = PersistentCameraAdapter(camera, images)

    class Spectrum:
        def run_point(self, manifest, local, execute):
            execute(local)
            return {"status": "complete", "simulated": True}

        def invalidate(self):
            pass

    def factory(path, context):
        return SimpleNamespace(
            _validate_target=lambda: None,
            initialize_devices=lambda: None,
            _execute_point=lambda point: camera.trigger(),
        )

    runner = StreamingRunner(Spectrum(), kernel_factory=factory)
    store = RunStore(tmp_path / "run")
    try:
        commit = store.commit_observation

        def fail_commit(*args, **kwargs):
            raise OSError("simulated interruption after raw save")

        monkeypatch.setattr(store, "commit_observation", fail_commit)
        with pytest.raises(OSError, match="raw save"):
            runner.run(artifact, result_adapter=adapter, run_store=store)
        assert store.latest(0)["status"] == "hardware_complete"
        assert camera.trigger_count == 1
        monkeypatch.setattr(store, "commit_observation", commit)
        recovery: RecoverableResultAdapter = PersistentCameraAdapter(camera, images)
        runner.resume(artifact, result_adapter=recovery, run_store=store)
        assert camera.trigger_count == len(order)
        assert all(
            store.latest(i)["status"] == "data_committed" for i in range(len(order))
        )
        assert store.latest(0)["context"].attempt == 0
        receipt = store.recover_observation(store.latest(0)["context"])
        saved = json.loads(open(receipt.path, encoding="utf-8").read())
        assert saved["values"]["occupancy"] == {"q0": 1.0}
        assert len(saved["assets"]) == 1
        assert [
            e["execution_index"]
            for e in store.events()
            if e["stage"] == "data_recovered"
        ] == [0]
    finally:
        store.close()


def test_native_example_declarations_do_not_import_artiq():
    import sys

    from my_lab.declarations import build_native_program

    before = {
        name for name in sys.modules if name == "artiq" or name.startswith("artiq.")
    }
    program = build_native_program()
    assert program.to_dict()["components"]["camera"]["resources"] == ["camera_trigger"]
    assert {
        name for name in sys.modules if name == "artiq" or name.startswith("artiq.")
    } == before
