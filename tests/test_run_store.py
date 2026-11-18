"""Crash boundaries distinguish completed hardware from durably acquired data."""

import inspect
import json
from types import SimpleNamespace

import pytest
from test_spectrum_backend import lab
from test_streaming import scan

from taiyidsl.run_store import RecoveryRequired, RunStore
from taiyidsl.stream_runtime import StreamingRunner


def test_observation_commit_recovers_after_journal_gap(tmp_path):
    store = RunStore(tmp_path)
    context = store.begin(0, 7, 3, store.start_session("artifact"))
    store.record(context, "hardware_complete", status="hardware_complete")
    store.commit_observation(context, {"population": 1})
    store.close()
    store = RunStore(tmp_path)
    assert not store.should_execute(0)
    assert store.latest(0)["status"] == "data_committed"
    with pytest.raises(ValueError, match="different compiled"):
        store.start_session("changed")
    store.close()


def test_unknown_attempt_requires_explicit_resolution_and_retains_history(tmp_path):
    store = RunStore(tmp_path)
    session = store.start_session("artifact")
    first = store.begin(0, 0, 0, session)
    store.record(first, "kernel_entered", status="kernel_entered")
    with pytest.raises(RecoveryRequired):
        store.should_execute(0)
    store.resolve(0, "retry", reason="operator confirmed discarded image")
    assert store.should_execute(0)
    second = store.begin(0, 0, 0, session)
    assert second.attempt == 1
    commit = store.commit_observation(second, {"population": 0})
    store.commit(second, commit)
    assert not store.should_execute(0)
    assert [e["attempt"] for e in store.events() if e["stage"] == "started"] == [0, 1]
    store.close()


@pytest.mark.parametrize("scope", ["global", "chunk"])
def test_stream_session_preserves_context_and_global_indices_across_chunks(
    tmp_path, scope
):
    artifact = lab().compile_stream(scan(scope=scope), directory=tmp_path / "artifact")
    log, identities = [], []

    class Spectrum:
        def run_point(self, manifest, local, execute):
            assert manifest["points"][local]["index"] == local
            execute(local)
            return {"status": "complete", "point": local}

        def invalidate(self):
            pass

    def factory(path, context):
        if path.name == "initialize.py":
            return SimpleNamespace(
                initialize_devices=lambda: log.append("initialize_devices")
            )
        context = context or SimpleNamespace(count=0, initialized=False)
        identities.append(id(context))
        chunk_index = int(path.parent.parent.name)

        def execute(local):
            context.count += 1
            log.append(("kernel", chunk_index, local, context.count))

        def initialize():
            assert not context.initialized
            context.initialized = True
            log.append("initialize_native")

        return SimpleNamespace(
            native_context=context,
            _execute_point=execute,
            _validate_target=lambda: None,
            initialize_native_once=initialize,
            finalize_native_session=lambda: log.append("finalize_session"),
        )

    class Detector:
        def before_shot(self, point, shot, *, context):
            assert (context.point, context.shot) == (point, shot)

        def after_shot(self, point, shot):
            return {"point": point, "shot": shot}

    store = RunStore(tmp_path / "run")
    runner = StreamingRunner(Spectrum(), kernel_factory=factory)
    runner.run(artifact, result_adapter=Detector(), run_store=store)
    assert len(set(identities)) == 1
    assert log.count("initialize_devices") == log.count("initialize_native") == 1
    assert log.count("finalize_session") == 1
    expected = list(artifact.execution_order())
    committed = [e for e in store.events() if e["stage"] == "data_committed"]
    assert len(committed) == len(expected)
    for event, pair in zip(committed, expected, strict=True):
        value = json.loads(open(event["payload"]["path"], encoding="utf-8").read())
        assert (value["values"]["point"], value["values"]["shot"]) == pair
    before = list(log)
    runner.resume(artifact, result_adapter=Detector(), run_store=store)
    assert log == before  # No initialization or kernel for an already complete run.
    store.close()


def test_detector_failure_cannot_silently_rerun_completed_hardware(tmp_path):
    artifact = lab().compile_stream(scan(), directory=tmp_path / "artifact")
    calls = []

    class Spectrum:
        def run_point(self, manifest, local, execute):
            calls.append(local)
            execute(local)
            return {"status": "complete"}

        def invalidate(self):
            pass

    def factory(path, context):
        return SimpleNamespace(
            _validate_target=lambda: None,
            initialize_devices=lambda: None,
            _execute_point=lambda point: None,
        )

    class Detector:
        def before_shot(self, point, shot):
            pass

        def after_shot(self, point, shot):
            raise RuntimeError("camera transfer")

    store = RunStore(tmp_path / "run")
    runner = StreamingRunner(Spectrum(), kernel_factory=factory)
    with pytest.raises(RuntimeError, match="camera transfer"):
        runner.run(artifact, result_adapter=Detector(), run_store=store)
    assert store.latest(0)["status"] == "hardware_complete"
    with pytest.raises(RecoveryRequired):
        runner.resume(artifact, result_adapter=Detector(), run_store=store)
    assert calls == [0]

    recovered = []

    class RecoveringDetector:
        def recover_shot(self, point, shot, *, context, run_store):
            assert calls == [0]  # Data lookup precedes any new hardware operation.
            recovered.append(context)
            return {"from_existing_camera_file": True}

        def after_shot(self, point, shot):
            return {"point": point, "shot": shot}

    runner.resume(artifact, result_adapter=RecoveringDetector(), run_store=store)
    assert len(calls) == 8  # The first completed hardware shot was not repeated.
    assert recovered == [store.latest(0)["context"]]
    assert recovered[0].attempt == 0
    assert store.latest(0)["status"] == "data_committed"
    store.close()


def test_run_store_excludes_concurrent_executors(tmp_path):
    first, second = RunStore(tmp_path), RunStore(tmp_path)
    try:
        with first.exclusive():
            with pytest.raises(RuntimeError, match="executor"):
                with second.exclusive():
                    pytest.fail("Concurrent executor acquired the run")
        with second.exclusive():
            pass
    finally:
        first.close()
        second.close()


def test_committed_detector_data_must_still_exist_before_resume(tmp_path):
    from pathlib import Path

    store = RunStore(tmp_path)
    try:
        context = store.begin(0, 0, 0, store.start_session("artifact"))
        receipt = store.commit_observation(context, {"counts": 4})
        store.commit(context, receipt)
        Path(receipt.path).unlink()
        with pytest.raises(ValueError, match="missing"):
            store.should_execute(0)
    finally:
        store.close()


def test_failed_kernel_stays_unknown_and_cleanup_keeps_original_error(tmp_path):
    artifact = lab().compile_stream(scan(), directory=tmp_path / "artifact")

    class Spectrum:
        def run_point(self, manifest, local, execute):
            execute(local)

        def invalidate(self):
            pass

    def fail_kernel(point):
        raise RuntimeError("kernel failed after trigger")

    def fail_cleanup():
        raise RuntimeError("cleanup failure")

    def factory(path, context):
        return SimpleNamespace(
            _validate_target=lambda: None,
            initialize_devices=lambda: None,
            _execute_point=fail_kernel,
            finalize_native_session=fail_cleanup,
        )

    store = RunStore(tmp_path / "run")
    try:
        runner = StreamingRunner(Spectrum(), kernel_factory=factory)
        with pytest.raises(RuntimeError, match="kernel failed") as caught:
            runner.run(artifact, result_adapter=object(), run_store=store)
        assert "cleanup failure" in caught.value.__notes__[0]
        assert store.latest(0)["status"] == "unknown"
        assert store.events()[-1]["stage"] == "session_finished"
        with pytest.raises(RecoveryRequired):
            store.should_execute(0)
        adapter = SimpleNamespace(
            recover_shot=lambda *args, **kwargs: pytest.fail(
                "Recovered unknown hardware"
            )
        )
        with pytest.raises(RecoveryRequired):
            runner.resume(artifact, result_adapter=adapter, run_store=store)
    finally:
        store.close()


def test_later_chunk_target_failure_precedes_union_initialization(tmp_path):
    artifact = lab().compile_stream(scan(), directory=tmp_path / "artifact")
    initialized = []

    def factory(path, context):
        def validate():
            if path.parent.parent.name == "1":
                raise ValueError("later device aliases native output")

        return SimpleNamespace(
            _validate_target=validate,
            initialize_devices=lambda: initialized.append(True),
        )

    store = RunStore(tmp_path / "run")
    try:
        runner = StreamingRunner(
            SimpleNamespace(invalidate=lambda: None), kernel_factory=factory
        )
        with pytest.raises(ValueError, match="later device"):
            runner.run(artifact, result_adapter=object(), run_store=store)
        assert initialized == []
        assert store.latest(0) is None
        assert "later device" in store.events()[-1]["payload"]["error"]
    finally:
        store.close()


def test_default_loader_reuses_root_environment_without_retaining_children(
    tmp_path, monkeypatch
):
    import gc
    import sys
    import weakref

    from taiyidsl.stream_runtime import StreamSessionRoot

    class Environment:
        def __init__(self, parent):
            self.children = []
            self.managers = parent if isinstance(parent, tuple) else parent.managers
            if not isinstance(parent, tuple):
                parent.register_child(self)
            self.build()

        def build(self):
            pass

        def register_child(self, child):
            self.children.append(child)

    monkeypatch.setitem(
        sys.modules, "artiq.experiment", SimpleNamespace(HasEnvironment=Environment)
    )
    source = tmp_path / "program.py"
    source.write_text(
        "from artiq.experiment import HasEnvironment\n"
        "class GeneratedExperiment(HasEnvironment):\n    pass\n"
    )
    runner = StreamingRunner(object(), managers=(object(), object(), object(), {}))
    runner.root = StreamSessionRoot(runner.managers)
    first = runner._load(source)
    reference = weakref.ref(first)
    environment = runner.root.environment
    runner._unload()
    del first
    second = runner._load(source)
    gc.collect()
    assert reference() is None
    assert runner.root.environment is environment
    assert environment.children == []
    assert second.managers is runner.managers
    runner._unload()


def test_actual_generated_components_keep_one_session_across_chunks(
    tmp_path, monkeypatch
):
    import gc
    import weakref

    from test_native_components import component_runtime, program_fixture
    from test_spectrum_contracts import _LifecycleCard

    from taiyidsl import Experiment, Qubit, Square, SweepParameter, delay, ns, play
    from taiyidsl.spectrum_runtime import SpectrumRunner

    program = program_fixture(tmp_path)
    q = Qubit("q")
    value = SweepParameter("phase", [0.1, 0.2, 0.3, 0.4], "rad")
    exp = Experiment("native_chunks", [q])
    with exp.average(2), exp.sweep(value, chunks=2), exp.realtime():
        program.prepare_MOT()
        program.GM_cooling()
        program.state_prep(ticks=24)
        delay(1000 * ns)
        with exp.triggered_sequence(group="g"):
            play(q.rf, Square(400 * ns, frequency=150e6, phase=value))
        program.readout(ticks=8)
    artifact = lab().compile_stream(exp, directory=tmp_path / "artifact")
    source = artifact.directory / "chunks/0/artiq/experiment.py"
    manifest = json.loads((source.parent.parent / "manifest.json").read_text())
    cards = {
        name: _LifecycleCard(name, spec["contract"], [])
        for name, spec in manifest["points"][0]["groups"][0]["cards"].items()
    }
    contexts, instances, roots = [], [], []

    def factory(path, context, *, root):
        roots.append(root)
        namespace = {"__file__": str(path), "__name__": "stream_native_test"}
        exec(compile(path.read_text(), str(path), "exec"), namespace)
        instance = namespace["GeneratedExperiment"]()
        if "native_context" in inspect.signature(instance.build).parameters:
            instance.build(native_context=context)
            contexts.append(instance.native_context)
            instances.append(weakref.ref(instance))
        else:
            instance.build()
        return instance

    class Detector:
        def after_shot(self, point, shot):
            gc.collect()
            assert sum(reference() is not None for reference in instances) == 1
            return {"point": point, "shot": shot}

    with component_runtime(source.read_text(), tmp_path, monkeypatch):
        store = RunStore(tmp_path / "run")
        try:
            StreamingRunner(
                SpectrumRunner(cards, allow_unverified=True), kernel_factory=factory
            ).run(artifact, result_adapter=Detector(), run_store=store)
        finally:
            store.close()
    # Two host-only preflights, then global cyclic averaging revisits each chunk.
    assert len(contexts) == 6
    assert all(value is contexts[0] for value in contexts)
    assert all(root is roots[0] for root in roots)
    assert roots[0].native_context is contexts[0]
    assert roots[0].program is None
    context = contexts[0]
    assert "GeneratedExperiment" not in type(context).__init__.__globals__
    gc.collect()
    assert all(reference() is None for reference in instances)
    assert context.prep.init_count == 1
    assert context.prep.prepare_count == context.readout.prepare_count == 1
    assert context.prep.shot_count == context.readout.shot_count == 8
    assert context.prep.final_count == context.readout.final_count == 1
