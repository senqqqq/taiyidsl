"""Host-only chunk loading, persistent native context, and journaled shots."""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import uuid
from dataclasses import dataclass

from ._cleanup import run_cleanup
from .contracts import ObservationCommit, RecoveryPolicy, ShotContext


def __getattr__(name):
    if name == "write_union_initializer":
        # The former stream_runtime entry remains available without making
        # offline compilation import the execution layer.
        from ..compiler._stream_initializer import write_union_initializer

        return write_union_initializer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class _RunState:
    session_id: str
    previous_cache: object
    program: object = None
    chunk_index: int | None = None
    manifest: dict | None = None
    initialized: bool = False
    preflight_complete: bool = False


@dataclass
class _AttemptState:
    context: ShotContext
    entered_kernel: bool = False
    hardware_complete: bool = False

    @property
    def failure_status(self):
        if self.hardware_complete:
            return "hardware_complete"
        return "unknown" if self.entered_kernel else "failed_before_hardware"


def _hook(adapter, name, point, shot, context, store):
    method = getattr(adapter, name, None)
    if method is None:
        return None
    parameters = inspect.signature(method).parameters
    keywords = {}
    if "context" in parameters:
        keywords["context"] = context
    if "run_store" in parameters:
        keywords["run_store"] = store
    return method(point, shot, **keywords)


class StreamSessionRoot:
    """Stable host owner; chunk kernels are replaceable children, not new sessions."""

    def __init__(self, managers):
        self.managers = managers
        self.native_context = None
        self.program = None
        self.environment = None

    def parent_environment(self):
        if self.environment is None:
            from artiq.experiment import HasEnvironment

            class SessionEnvironment(HasEnvironment):
                def register_child(self, child):
                    # Explicit lifecycle belongs to this runner. ARTIQ's default
                    # children list would otherwise retain every discarded chunk.
                    pass

            self.environment = SessionEnvironment(self.managers)
        return self.environment

    def bind(self, program):
        self.program = program
        self.native_context = getattr(program, "native_context", self.native_context)

    def execute(self, local_point):
        return self.program._execute_point(local_point)


class StreamingRunner:
    """Execute a complete CompilationStore with one native session context.

    managers is an ARTIQ managers tuple or parent environment. kernel_factory is
    an optional integration/test hook accepting (source_path, native_context).
    Loading/building a chunk binds shared devices; initialization is separate.
    """

    def __init__(self, spectrum_runner, *, managers=None, kernel_factory=None):
        self.spectrum_runner = spectrum_runner
        self.managers = managers
        self.kernel_factory = kernel_factory
        self.root = None
        self._module_names = []

    def _load(self, path, context=None):
        if self.kernel_factory is not None:
            options = (
                {"root": self.root}
                if "root" in inspect.signature(self.kernel_factory).parameters
                else {}
            )
            return self.kernel_factory(path, context, **options)
        if self.managers is None:
            raise ValueError("ARTIQ managers or a kernel_factory is required")
        name = "_taiyi_stream_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self._module_names.append(name)
        try:
            spec.loader.exec_module(module)
            cls = module.GeneratedExperiment
            options = (
                {"native_context": context}
                if "native_context" in inspect.signature(cls.build).parameters
                else {}
            )
            return cls(self.root.parent_environment(), **options)
        except BaseException:
            sys.modules.pop(name, None)
            raise

    def _unload(self):
        for name in self._module_names:
            sys.modules.pop(name, None)
        self._module_names.clear()

    def run(self, artifact, *, result_adapter, run_store, unresolved="error"):
        with run_store.exclusive():
            return self._run(
                artifact,
                result_adapter=result_adapter,
                run_store=run_store,
                unresolved=unresolved,
            )

    def _run(
        self,
        artifact,
        *,
        result_adapter,
        run_store,
        unresolved: RecoveryPolicy = "error",
    ):
        """Resume only journal-safe shots; unknown physical outcomes stop by default."""
        artifact.verify()
        state = _RunState(
            session_id=run_store.start_session(artifact.sha256),
            previous_cache=getattr(self.spectrum_runner, "cache", None),
        )
        self.root = StreamSessionRoot(self.managers)
        try:
            if state.previous_cache is not None and "segments" in artifact.manifest:
                from ..compiler.store import ArtifactSegments

                self.spectrum_runner.cache = ArtifactSegments(artifact)
            for execution_index, (point, shot) in enumerate(artifact.execution_order()):
                if not self._recover_attempt(
                    execution_index, point, shot, result_adapter, run_store, unresolved
                ):
                    continue
                self._preflight_chunks(artifact, state)
                local = self._activate_chunk(artifact, state, point)
                self._execute_attempt(
                    state,
                    execution_index,
                    point,
                    shot,
                    local,
                    result_adapter,
                    run_store,
                )
            finish = getattr(result_adapter, "finish", None)
            if finish is not None:
                finish()
        finally:
            self._finish_session(state, run_store, sys.exception())

    def _recover_attempt(
        self, execution_index, point, shot, adapter, store, unresolved
    ):
        """Look up original data before loading or arming a shot's program."""
        previous = store.latest(execution_index)
        if (
            previous is not None
            and previous["status"] == "hardware_complete"
            and hasattr(adapter, "recover_shot")
            and store.recover_observation(previous["context"]) is None
        ):
            context = previous["context"]
            recovered = _hook(adapter, "recover_shot", point, shot, context, store)
            if isinstance(recovered, dict):
                recovered = store.commit_observation(context, recovered)
            if recovered is not None:
                store.commit(context, recovered)
                store.record(context, "data_recovered")
        return store.should_execute(execution_index, unresolved=unresolved)

    def _preflight_chunks(self, artifact, state):
        """Validate every target/native alias before union device initialization."""
        if state.preflight_complete:
            return
        for candidate in artifact.manifest["chunks"]:
            source = artifact.check_file(
                candidate["directory"] + "/artiq/experiment.py"
            )
            state.program = self._load(source, self.root.native_context)
            self.root.bind(state.program)
            state.program._validate_target()
            state.program = None
            self.root.program = None
            self._unload()
        state.preflight_complete = True

    def _activate_chunk(self, artifact, state, point):
        """Replace chunk code while retaining one native context and initialization."""
        chunk = artifact.chunk_for_point(point)
        if state.chunk_index != chunk["index"]:
            self._unload()
            directory = artifact.directory / chunk["directory"]
            source = artifact.check_file(chunk["directory"] + "/artiq/experiment.py")
            artifact.check_file(chunk["directory"] + "/manifest.json")
            state.program = self._load(source, self.root.native_context)
            self.root.bind(state.program)
            state.program._validate_target()
            if not state.initialized:
                if hasattr(state.program, "prepare"):
                    state.program.prepare()
                initializer = self._load(artifact.directory / "initialize.py")
                initializer.initialize_devices()
                del initializer
                if hasattr(state.program, "initialize_native_once"):
                    state.program.initialize_native_once()
                state.program._validate_target()
                state.initialized = True
            state.chunk_index = chunk["index"]
            state.manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8")
            )
        return chunk["points"].index(point)

    def _execute_attempt(
        self, state, execution_index, point, shot, local, adapter, store
    ):
        """Keep physical completion separate from a durable observation commit."""
        attempt = _AttemptState(
            store.begin(execution_index, point, shot, state.session_id)
        )

        def execute(local_point):
            store.record(attempt.context, "kernel_entered", status="kernel_entered")
            attempt.entered_kernel = True
            return self.root.execute(local_point)

        try:
            _hook(adapter, "before_shot", point, shot, attempt.context, store)
            store.record(attempt.context, "prepared", status="prepared")
            receipt = self.spectrum_runner.run_point(state.manifest, local, execute)
            receipt.update(local_point=local, point=point, chunk=state.chunk_index)
            attempt.hardware_complete = True
            store.record(
                attempt.context,
                "hardware_complete",
                receipt,
                status="hardware_complete",
            )
            observation = _hook(
                adapter, "after_shot", point, shot, attempt.context, store
            )
            if isinstance(observation, dict):
                observation = store.commit_observation(attempt.context, observation)
            if not isinstance(observation, ObservationCommit):
                raise TypeError(
                    "Persistent execution requires after_shot to return "
                    "observations or an ObservationCommit"
                )
            store.commit(attempt.context, observation)
        except BaseException as error:
            # A failed abort must not hide the shot error or prevent recording
            # whether its hardware outcome is unknown or already complete.
            failure = {"error": str(error), "type": type(error).__name__}
            run_cleanup(
                [
                    ("abort failed shot", lambda: self._spectrum_cleanup("abort")),
                    (
                        "record failed attempt",
                        lambda: store.record(
                            attempt.context,
                            "failed",
                            failure,
                            status=attempt.failure_status,
                        ),
                    ),
                ],
                primary_error=error,
            )
            raise

    def _spectrum_cleanup(self, name):
        method = getattr(self.spectrum_runner, name, None)
        if method is None:
            method = self.spectrum_runner.invalidate
        method()

    def _release_outputs(self, active_error):
        method = "release_awg_banks" if active_error is None else "abort"
        try:
            self._spectrum_cleanup(method)
        except BaseException as error:
            if method != "abort":
                run_cleanup(
                    [
                        (
                            "abort after bank release",
                            lambda: self._spectrum_cleanup("abort"),
                        )
                    ],
                    primary_error=error,
                )
            raise

    def _finalize_native(self, state):
        if state.program is None or not hasattr(
            state.program, "finalize_native_session"
        ):
            return
        try:
            state.program.finalize_native_session()
        except BaseException as error:
            run_cleanup(
                [
                    (
                        "abort after native finalization",
                        lambda: self._spectrum_cleanup("abort"),
                    )
                ],
                primary_error=error,
            )
            raise

    def _finish_session(self, state, store, active_error):
        """Attempt all owned cleanup and record the original or first cleanup error."""
        cleanup_error = None

        def tracked(action):
            def run():
                nonlocal cleanup_error
                try:
                    action()
                except BaseException as error:
                    if cleanup_error is None:
                        cleanup_error = error
                    raise

            return run

        def clear_program():
            self.root.program = None

        def restore_cache():
            if state.previous_cache is not None:
                self.spectrum_runner.cache = state.previous_cache

        def finish_record():
            error = active_error or cleanup_error
            store.finish_session(
                state.session_id,
                error=None if error is None else f"{type(error).__name__}: {error}",
            )

        actions = [
            ("release hardware", lambda: self._release_outputs(active_error)),
            ("finalize native session", lambda: self._finalize_native(state)),
            ("release chunk owner", clear_program),
            ("restore segment cache", restore_cache),
            ("unload chunk modules", self._unload),
            ("record session completion", finish_record),
        ]
        run_cleanup(
            [(name, tracked(action)) for name, action in actions],
            primary_error=active_error,
        )

    def resume(self, artifact, **options):
        return self.run(artifact, **options)
