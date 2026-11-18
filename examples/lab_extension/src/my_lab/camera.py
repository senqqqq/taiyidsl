"""Two host adapters sharing image handling, with a deliberately fake camera."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import numpy as np

from taiyidsl import ImageResults, ObservationCommit, RunStore, ShotContext


class FakeCamera:
    """Preselected frames keyed by (point, shot); None means a missing frame.

    trigger() replaces a TTL edge only in the offline demonstration. A real
    adapter arms its camera and waits for the separately scheduled native/DSL
    trigger; it must not issue a second software trigger in after_shot.
    """

    def __init__(self, frames):
        self.frames = dict(frames)
        self.armed = None
        self.triggered = False
        self.trigger_count = 0

    def arm(self, point, shot):
        if self.armed is not None:
            raise RuntimeError("Previous camera frame has not been read")
        if (point, shot) not in self.frames:
            raise KeyError("No fake frame configured for this point and shot")
        self.armed = (point, shot)

    def trigger(self):
        if self.armed is None or self.triggered:
            raise RuntimeError("Camera must be armed once before each trigger")
        self.triggered = True
        self.trigger_count += 1

    def read(self):
        if not self.triggered:
            raise RuntimeError("A frame cannot be read before the trigger")
        value = self.frames[self.armed]
        self.armed, self.triggered = None, False
        return None if value is None else np.asarray(value).copy()


def _record(images, point, shot, frame):
    status = "missing" if frame is None else "valid"
    images.record_frame("readout", point, shot, frame, status=status)
    occupancy = {
        site: (
            float(images.results.data["readout"][point, shot, index])
            if status == "valid"
            else None
        )
        for index, site in enumerate(images.results.qubits)
        if site in images.sites
    }
    return {
        "status": status,
        "occupancy": occupancy,
        "classifier_version": images.classifier_version,
    }


class CameraAdapter:
    """Ordinary exported host loop: explicit two-argument callbacks."""

    def __init__(self, camera: FakeCamera, images: ImageResults, archive: Path):
        self.camera, self.images, self.archive = camera, images, archive

    def before_shot(self, point: int, shot: int) -> None:
        self.camera.arm(point, shot)

    def after_shot(self, point: int, shot: int) -> None:
        _record(self.images, point, shot, self.camera.read())

    def finish(self) -> None:
        self.images.save_hdf5(self.archive)


class PersistentCameraAdapter:
    """StreamingRunner adapter: save raw data before committing observations.

    Each attempt's raw NPZ records the full context. A partial or mismatched
    file raises during recovery instead of inventing a frame or repeating a
    shot. RunStore's exclusive execution lock prevents concurrent writers.
    finish is unnecessary: each after_shot publishes its own durable receipt.
    """

    def __init__(self, camera: FakeCamera, images: ImageResults):
        self.camera, self.images = camera, images

    @staticmethod
    def _path(point, shot, context, run_store):
        if (context.point, context.shot) != (point, shot):
            raise ValueError("Image indices differ from the shot context")
        if context.run_id != run_store.run_id:
            raise ValueError("Image context belongs to another run")
        return (
            run_store.directory
            / "raw"
            / f"{context.execution_index}_{context.attempt}.npz"
        )

    def before_shot(
        self, point: int, shot: int, *, context: ShotContext, run_store: RunStore
    ) -> None:
        self._path(point, shot, context, run_store)
        self.camera.arm(point, shot)

    def after_shot(
        self, point: int, shot: int, *, context: ShotContext, run_store: RunStore
    ) -> ObservationCommit:
        path = self._path(point, shot, context, run_store)
        frame = self.camera.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {"context": json.dumps(asdict(context)), "missing": frame is None}
        if frame is not None:
            arrays["frame"] = frame
        # Exclusive creation prevents a second invocation overwriting evidence.
        # A crash during this write leaves an invalid file, which recovery rejects.
        with path.open("xb") as stream:
            np.savez(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        values = _record(self.images, point, shot, frame)
        return run_store.commit_observation(context, values, assets=[path])

    def recover_shot(
        self, point: int, shot: int, *, context: ShotContext, run_store: RunStore
    ) -> ObservationCommit | None:
        path = self._path(point, shot, context, run_store)
        if not path.is_file():
            return None
        with np.load(path, allow_pickle=False) as saved:
            if json.loads(str(saved["context"])) != asdict(context):
                raise ValueError("Saved frame belongs to another shot attempt")
            frame = None if bool(saved["missing"]) else saved["frame"].copy()
        values = _record(self.images, point, shot, frame)
        return run_store.commit_observation(context, values, assets=[path])
