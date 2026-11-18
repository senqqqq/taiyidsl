"""Persistent execution records shot outcomes even when cleanup also fails."""

from types import SimpleNamespace

import pytest
from test_spectrum_backend import lab
from test_streaming import scan

from taiyidsl.runtime.run_store import RunStore
from taiyidsl.runtime.streaming import StreamingRunner


@pytest.mark.parametrize(
    ("failure_phase", "expected_status"),
    [("kernel", "unknown"), ("detector", "hardware_complete")],
)
def test_abort_failure_preserves_shot_error_and_finishes_session(
    tmp_path, monkeypatch, failure_phase, expected_status
):
    artifact = lab().compile_stream(scan(), directory=tmp_path / "artifact")
    original_error = (
        ValueError("kernel failed after trigger")
        if failure_phase == "kernel"
        else RuntimeError("camera transfer failed")
    )
    log = []
    original_cache = object()

    class Spectrum:
        cache = original_cache

        def run_point(self, manifest, point, execute):
            execute(point)
            return {"status": "complete"}

        def invalidate(self):
            pytest.fail("An available abort hook was ignored")

        def abort(self):
            log.append("abort")
            raise RuntimeError("abort failed")

    def kernel(point):
        if failure_phase == "kernel":
            raise original_error

    def finalize():
        log.append("finalize")
        raise RuntimeError("native finalize failed")

    def factory(path, context):
        return SimpleNamespace(
            _validate_target=lambda: None,
            initialize_devices=lambda: None,
            _execute_point=kernel,
            finalize_native_session=finalize,
        )

    class Detector:
        def after_shot(self, point, shot):
            raise original_error

    spectrum = Spectrum()
    runner = StreamingRunner(spectrum, kernel_factory=factory)
    unload = runner._unload

    def fail_final_unload():
        unload()
        if "finalize" in log:
            log.append("unload")
            raise RuntimeError("unload failed")

    monkeypatch.setattr(runner, "_unload", fail_final_unload)
    store = RunStore(tmp_path / "run")
    try:
        with pytest.raises(type(original_error)) as caught:
            runner.run(artifact, result_adapter=Detector(), run_store=store)
        assert caught.value is original_error
        assert store.latest(0)["status"] == expected_status
        failed = next(event for event in store.events() if event["stage"] == "failed")
        assert failed["payload"] == {
            "error": str(original_error),
            "type": type(original_error).__name__,
        }
        finished = store.events()[-1]
        assert finished["stage"] == "session_finished"
        assert finished["payload"]["error"] == (
            f"{type(original_error).__name__}: {original_error}"
        )
        notes = "\n".join(caught.value.__notes__)
        assert all(
            message in notes
            for message in ("abort failed", "native finalize failed", "unload failed")
        )
        assert log.count("finalize") == log.count("unload") == 1
        assert spectrum.cache is original_cache
        assert runner.root.program is None
    finally:
        store.close()
