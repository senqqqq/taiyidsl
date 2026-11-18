"""Run the lab adapter contract with artificial frames and no instruments."""

import argparse
import json
import uuid
from pathlib import Path

import numpy as np

from taiyidsl import RunStore
from taiyidsl.runtime.readout import ResultAdapter

from .camera import CameraAdapter, FakeCamera, PersistentCameraAdapter
from .configuration import image_results
from .declarations import build_native_program


def main(output=Path("output/lab_extension")):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    frames = {
        (0, 0): np.full((2, 2), 5, np.uint16),  # Occupied.
        (0, 1): np.ones((2, 2), np.uint16),  # Valid image, empty site.
        (0, 2): None,  # Missing image, not a measured empty site.
    }
    native = build_native_program()
    (output / "native_declaration.json").write_text(
        json.dumps(native.to_dict(), indent=2), encoding="utf-8"
    )

    images = image_results()
    camera = FakeCamera(frames)
    adapter: ResultAdapter = CameraAdapter(camera, images, output / "images.h5")
    # This mirrors ordinary host hook order. It never loads native kernels.
    for point, shot in frames:
        adapter.before_shot(point, shot)
        camera.trigger()  # Offline stand-in for the scheduled TTL edge.
        adapter.after_shot(point, shot)
    adapter.finish()

    durable_images = image_results()
    durable_camera = FakeCamera(frames)
    persistent = PersistentCameraAdapter(durable_camera, durable_images)
    run_directory = output / f"run_{uuid.uuid4().hex[:8]}"
    store = RunStore(run_directory)
    try:
        session = store.start_session("synthetic-camera-contract-demo-v1")
        # Only this fake demonstration writes these execution states itself.
        # With real hardware, StreamingRunner owns begin/record/commit instead.
        with store.exclusive():
            for index, (point, shot) in enumerate(frames):
                context = store.begin(index, point, shot, session)
                persistent.before_shot(point, shot, context=context, run_store=store)
                durable_camera.trigger()
                store.record(
                    context,
                    "hardware_complete",
                    {"simulated": True, "hardware_executed": False},
                    status="hardware_complete",
                )
                receipt = persistent.after_shot(
                    point, shot, context=context, run_store=store
                )
                if index < len(frames) - 1:
                    store.commit(context, receipt)
                # Last receipt is on disk but not journaled: simulated interruption.
    finally:
        store.close()
    store = RunStore(run_directory)
    try:
        with store.exclusive():
            assert not store.should_execute(len(frames) - 1)
            assert durable_camera.trigger_count == len(frames)
        statuses = [store.latest(index)["status"] for index in range(len(frames))]
    finally:
        store.close()
    durable_images.save_hdf5(run_directory / "images.h5")
    summary = {
        "hardware_executed": False,
        "native_kernels_loaded": False,
        "frame_source": "artificial arrays",
        "occupancy": [
            None if np.isnan(value) else float(value)
            for value in images.results.data["readout"][0, :, 0]
        ],
        "image_status": images.results.status["readout"][0, :, 0].tolist(),
        "ordinary_mock_triggers": camera.trigger_count,
        "persistent_mock_triggers": durable_camera.trigger_count,
        "run_directory": str(run_directory),
        "journal_status": statuses,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/lab_extension"))
    main(parser.parse_args().output)
