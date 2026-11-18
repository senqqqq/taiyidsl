"""Independent native objects, strict stage boundaries and shared sessions."""

import ast
import importlib
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_native_export import TTL, Clock, Core
from test_spectrum_backend import lab

from taiyidsl import (
    Experiment,
    NativeKernelPackage,
    Qubit,
    Square,
    delay,
    ns,
    play,
    shift_phase,
)
from taiyidsl.artiq import _fake_artiq, generate_source
from taiyidsl.native_components import NativeModule, NativeProgram, NativeStage
from taiyidsl.native_export import compose_native_source
from taiyidsl.spectrum_export import _epochs, align_source_epochs, artiq_subplan

SOURCE = """from artiq.experiment import HasEnvironment, kernel, delay_mu

class Routines(HasEnvironment):
    def build(self, marker=1):
        self.setattr_device("core")
        self.setattr_device("native_ttl")
        self.marker = marker
        self.value = self.get_argument("value", None)
        self.init_count = 0
        self.shot_count = 0
        self.final_count = 0
        self.prepare_count = 0
        self.observed_frame = 0.0

    def prepare(self):
        raise RuntimeError("undeclared prepare must not run")

    def host_ready(self):
        self.prepare_count += 1

    @kernel
    def initialize(self):
        self.init_count += 1
        self.native_ttl.off()

    @kernel
    def prefix(self):
        self.core.break_realtime()
        self.native_ttl.on()
        delay_mu(16)

    @kernel
    def timed(self, ticks=24):
        self.native_ttl.off()
        delay_mu(ticks)

    @kernel
    def readout(self, ticks):
        self.observed_frame = self.native_terminal_f_q
        self.native_ttl.on()
        delay_mu(ticks)
        self.native_ttl.off()

    @kernel
    def after(self):
        self.shot_count += 1

    @kernel
    def finish(self):
        self.final_count += 1
"""


def program_fixture(
    tmp_path, *, source=SOURCE, share=True, initialize_b=False, discover_sources=False
):
    modules = {}
    for index, name in enumerate(("prep", "readout")):
        module = f"native_component_{name}"
        path = tmp_path / f"{module}.py"
        path.write_text(source, encoding="utf-8")
        constructor = NativeModule.from_package if discover_sources else NativeModule
        location = (
            {"root": tmp_path, "entry": f"{module}:Routines"}
            if discover_sources
            else {"module": module, "class_name": "Routines", "sources": {module: path}}
        )
        modules[name] = constructor(
            **location,
            resources=("native_ttl",),
            build_kwargs={"marker": index + 1},
            host_prepare="host_ready",
            initialize="initialize" if index == 0 or initialize_b else None,
            after_shot="after",
            finalize="finish",
        )
    return NativeProgram(
        modules=modules,
        stages={
            "prepare_MOT": NativeStage("prep", "prefix", timing="prefix"),
            "GM_cooling": NativeStage(
                "prep", "timed", timing="timed", duration=24 * ns
            ),
            "state_prep": NativeStage(
                "readout", "timed", timing="timed", duration=24 * ns
            ),
            "readout": NativeStage(
                "readout",
                "readout",
                timing="suffix",
                duration=8 * ns,
                phase_sensitive=True,
            ),
        },
        shared_resources={"native_ttl": ("prep", "readout")} if share else None,
    )


def compile_program(program):
    q = Qubit("q")
    exp = Experiment("components", [q])
    with exp.average(2), exp.realtime():
        program.prepare_MOT()
        program.GM_cooling()
        program.state_prep(ticks=24)
        delay(1000 * ns)
        with exp.triggered_sequence(group="g"):
            shift_phase(q.frame, 0.3)
            play(q.rf, Square(400 * ns, frequency=150e6, amplitude=0.2))
        program.readout(ticks=8)
    compiled = lab().compile(exp)
    native = artiq_subplan(compiled)
    epochs = _epochs(compiled, native)
    source, report = compose_native_source(
        compiled,
        native,
        align_source_epochs(generate_source(native, mode="unrolled"), epochs),
        epochs,
    )
    return compiled, source, report


@contextmanager
def _isolated_native_modules():
    names = ("native_component_prep", "native_component_readout")
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    try:
        yield
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


@contextmanager
def component_runtime(source, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    clock = Clock(0)
    devices = {
        "core": Core(clock),
        "awg_trigger": TTL(clock, "trigger"),
        "native_ttl": TTL(clock, "native"),
    }
    with _fake_artiq(clock, devices), _isolated_native_modules():

        class HasEnvironment:
            def __init__(self, parent, **kwargs):
                # Real HasEnvironment copies managers, never the parent itself.
                self._devices = devices
                self.build(**kwargs)

            def get_device(self, key):
                return self._devices[key]

            def setattr_device(self, key):
                setattr(self, key, self.get_device(key))

            def get_argument(self, key, processor, group=None, tooltip=None):
                return {"prep.value": 7, "readout.value": 9}[key]

        sys.modules["artiq.experiment"].HasEnvironment = HasEnvironment
        sys.modules["artiq.experiment"].delay_mu = lambda value: clock.at_mu(
            clock.cursor + value
        )
        sys.modules["artiq.experiment"].EnvExperiment.setattr_device = (
            lambda self, key: setattr(self, key, self.get_device(key))
        )
        namespace = {"__name__": "component_export"}
        exec(compile(source, "<component-export>", "exec"), namespace)
        yield namespace["GeneratedExperiment"], clock, devices


def test_components_keep_state_arguments_frames_and_session_lifecycle(
    tmp_path, monkeypatch
):
    program = program_fixture(tmp_path)
    compiled, source, report = compile_program(program)
    assert "native_component_prep" not in sys.modules
    assert compiled.metadata["native_artiq"]["source_audit"]["status"] == "passed"
    assert report["session_finalize_method"] == "finalize_native_session"
    with component_runtime(source, tmp_path, monkeypatch) as (generated, clock, _):
        first = generated()
        first.build()
        first.prepare()
        first.prepare()
        first._validate_target()
        first.initialize_devices()
        first.initialize_native_once()
        first.point_0()
        context = first.native_context
        assert context.prep is not context.readout
        assert "GeneratedExperiment" not in type(context).__init__.__globals__
        assert "GeneratedExperiment" not in type(context.prep).get_argument.__globals__
        assert (context.prep.value, context.readout.value) == (7, 9)
        assert (context.prep.marker, context.readout.marker) == (1, 2)
        assert context.prep.init_count == 1
        assert context.readout.init_count == 0
        assert context.readout.observed_frame == pytest.approx(0.3)
        assert context.prep.prepare_count == context.readout.prepare_count == 1
        # A separately imported chunk defines another generated root/facade
        # class, but must retain the original component objects and their state.
        second_namespace = {"__name__": "component_export_second_chunk"}
        exec(compile(source, "<next-component-chunk>", "exec"), second_namespace)
        second = second_namespace["GeneratedExperiment"]()
        second.build(native_context=context)
        second.prepare()
        second._validate_target()
        second.initialize_native_once()
        second.point_0()
        assert second._native_prep is first._native_prep
        assert context.prep.init_count == 1
        assert context.prep.shot_count == context.readout.shot_count == 2
        second.finalize_native_session()
        first.finalize_native_session()
        assert context.prep.final_count == context.readout.final_count == 1
        with pytest.raises(RuntimeError, match="finalized"):
            generated().build(native_context=context)
        with pytest.raises(RuntimeError, match="initialized and active"):
            first.point_0()
        assert any(item[0] == "trigger.on" for item in clock.trace)


def test_package_factory_preserves_descriptors_and_export_without_import(tmp_path):
    # Importing either file would fail. Declaration and export only inspect AST.
    source = SOURCE + "\nraise RuntimeError('native source was imported')\n"
    explicit = program_fixture(tmp_path, source=source)
    discovered = program_fixture(tmp_path, source=source, discover_sources=True)
    assert discovered.to_dict() == explicit.to_dict()
    _, explicit_source, explicit_report = compile_program(explicit)
    _, discovered_source, discovered_report = compile_program(discovered)
    assert discovered_source == explicit_source
    assert discovered_report == explicit_report
    assert "native_component_prep" not in sys.modules
    (tmp_path / "native_component_prep.py").write_text(source + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        compile_program(discovered)


def test_package_factory_discovers_local_imports_and_explicit_overrides(tmp_path):
    root = tmp_path / "src"
    package = root / "my_lab"
    package.mkdir(parents=True)
    paths = {
        "my_lab": package / "__init__.py",
        "my_lab.cooling": package / "cooling.py",
        "my_lab.helpers": package / "helpers.py",
        "existing_lab": tmp_path / "existing_lab.py",
    }
    paths["my_lab"].write_text("raise RuntimeError('no import')\n", encoding="utf-8")
    paths["my_lab.cooling"].write_text(
        "from artiq.experiment import HasEnvironment\n"
        "from .helpers import hold\nfrom existing_lab import DEFAULT\n"
        "class Cooling(HasEnvironment):\n    pass\n",
        encoding="utf-8",
    )
    paths["my_lab.helpers"].write_text("def hold():\n    pass\n", encoding="utf-8")
    paths["existing_lab"].write_text("DEFAULT = 1\n", encoding="utf-8")
    discovered = NativeModule.from_package(
        root=root,
        entry="my_lab.cooling:Cooling",
        resources=("cooling_ttl",),
        source_overrides={"existing_lab": paths["existing_lab"]},
    )
    explicit = NativeModule(
        module="my_lab.cooling",
        class_name="Cooling",
        sources=paths,
        resources=("cooling_ttl",),
    )
    assert discovered.to_dict() == explicit.to_dict()
    assert "my_lab.cooling" not in sys.modules


@pytest.mark.parametrize("factory", [NativeModule, NativeKernelPackage])
@pytest.mark.parametrize(
    "entry", [None, "Cooling", "my_lab:Cooling:other", "bad-name:C"]
)
def test_package_factory_rejects_invalid_entry_before_reading_sources(factory, entry):
    with pytest.raises(ValueError, match="entry|identifier"):
        factory.from_package(root="missing-root", entry=entry, resources=("ttl",))


def test_components_require_declared_sharing_and_one_initializer(tmp_path):
    with pytest.raises(ValueError, match="must be declared"):
        program_fixture(tmp_path, share=False)
    with pytest.raises(ValueError, match="one initialization owner"):
        program_fixture(tmp_path, initialize_b=True)


def test_strict_component_and_lifecycle_calls_are_audited(tmp_path):
    source = SOURCE.replace(
        "delay_mu(ticks)", "self.core.break_realtime()\n        delay_mu(ticks)"
    )
    with pytest.raises(ValueError, match="forbidden|strict"):
        compile_program(program_fixture(tmp_path, source=source))
    source = SOURCE.replace("self.init_count += 1", "self.core.reset()")
    with pytest.raises(ValueError, match="reset"):
        compile_program(program_fixture(tmp_path, source=source))


def test_source_mutation_and_argument_mismatch_fail_before_export(tmp_path):
    program = program_fixture(tmp_path)
    with pytest.raises(TypeError, match="unexpected"):
        program.GM_cooling(unknown=2)
    path = tmp_path / "native_component_prep.py"
    path.write_text(SOURCE + "\nraise RuntimeError('no import')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        compile_program(program)


def test_native_session_finalizes_once_after_host_shot_failure(tmp_path, monkeypatch):
    _, source, _ = compile_program(program_fixture(tmp_path))
    with component_runtime(source, tmp_path, monkeypatch) as (generated, _, _):
        instance = generated()
        instance.build()

        class FailingAdapter:
            def before_shot(self, point, shot):
                raise RuntimeError("detector unavailable")

        instance.result_adapter = FailingAdapter()
        with pytest.raises(RuntimeError, match="detector unavailable"):
            instance.run()
        assert instance.native_context.prep.final_count == 1
        assert instance.native_context.readout.final_count == 1
        assert instance.native_context.prep.shot_count == 0


def test_native_context_rejects_other_program_and_device_alias(tmp_path, monkeypatch):
    _, source, _ = compile_program(program_fixture(tmp_path))
    with component_runtime(source, tmp_path, monkeypatch) as (generated, _, devices):
        first = generated()
        first.build()
        first.native_context.signature = "different"
        with pytest.raises(RuntimeError, match="different program"):
            generated().build(native_context=first.native_context)
        devices["native_ttl"] = devices["awg_trigger"]
        with pytest.raises(RuntimeError, match="resource alias"):
            first._validate_target()


def test_frame_attribute_encoding_preserves_valid_names_without_collisions():
    from taiyidsl.native_export import _frame_suffix

    assert _frame_suffix("q") == "q"
    assert _frame_suffix("q__phase__clock") == "q__phase__clock"
    encoded = _frame_suffix("q-1")
    assert encoded.isidentifier()
    assert _frame_suffix(encoded) != encoded


def test_example_declarations_support_normal_package_import_without_artiq(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root))
    declarations = importlib.import_module(
        "examples.artiq_spectrum.native_stages.declarations"
    )
    adapter = importlib.import_module("examples.artiq_spectrum.greenmot_native.adapter")
    program = declarations.build_native_program()
    assert set(program.to_dict()["components"]) == {
        "mot",
        "cooling",
        "state",
        "readout",
    }
    assert (
        adapter.GreenMOTAdapter().package.to_dict()["class_name"] == "GreenMOTFixture"
    )
    assert "native_stages.mot" not in sys.modules
    assert "greenmot_fixture" not in sys.modules


def test_component_build_keeps_additional_generated_statements():
    from taiyidsl.native._generated import GeneratedProgram, NativeBuildPrelude
    from taiyidsl.native.component_export import configure_component_export

    program = GeneratedProgram.parse("""
events = []
class _NativePackage:
    def build(self, context):
        events.append(("native", context))
class GeneratedExperiment:
    def build(self):
        events.append(("control", None))
    def _validate_target(self):
        pass
    def initialize_devices(self):
        pass
    def run(self):
        pass
""")
    build = program.methods["build"]
    prelude = NativeBuildPrelude.install(build, "build", ())
    # A future exporter may insert setup before the native prelude. Composition
    # must keep that setup and still configure the intended native call.
    build.body.insert(0, ast.parse('events.append(("additional", None))').body[0])
    configure_component_export(program, prelude)
    ast.fix_missing_locations(program.tree)
    namespace = {}
    exec(compile(program.tree, "<component-build>", "exec"), namespace)
    namespace["GeneratedExperiment"]().build(native_context="session")
    assert namespace["events"] == [
        ("additional", None),
        ("native", "session"),
        ("control", None),
    ]


def test_component_calls_record_the_experiment_source(tmp_path):
    program = program_fixture(tmp_path)
    exp = Experiment("native_provenance", [Qubit("q")])
    with exp.average(1), exp.realtime():
        line = sys._getframe().f_lineno + 1
        program.prepare_MOT()
    call = exp.root.children[0].children[0].children[0]
    assert call.source == f"{__file__}:{line}"
