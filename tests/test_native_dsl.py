"""Native calls declare device-side composition without executing user code."""

import json
import math
import sys
from pathlib import Path

import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    NativeKernelPackage,
    Qubit,
    SignalMap,
    Square,
    SweepParameter,
    compile_experiment,
    delay,
    play,
    rz,
    trigplay,
    us,
)

LAB = Path(__file__).parents[1] / "examples/artiq_spectrum/lab_simulated.json"
SOURCE = """from artiq.experiment import kernel, delay, us

class NativeStages:
    def build(self):
        self.setattr_device("native_ttl")

    @kernel
    def prepare_MOT(self, duration):
        self.core.break_realtime()
        self.native_ttl.on()
        delay(duration)

    @kernel
    def execute_readout(self):
        self.native_ttl.pulse(8 * us)

    @kernel
    def finish_preparation(self, duration_s):
        delay(duration_s)
        self.native_ttl.off()
"""


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "native_fixture.py"
    source.write_text(SOURCE, encoding="utf-8")
    return NativeKernelPackage(
        module="native_fixture",
        class_name="NativeStages",
        sources={"native_fixture": source},
        resources=("native_ttl",),
    )


def _experiment(package=None, *, nesting=None):
    q = Qubit("q")
    exp = Experiment("native_ramsey", [q])
    pulse = Square(200 * us, amplitude=0.2, frequency=30000)
    dark = SweepParameter("dark", [10, 1000], "us")
    with exp.average(2, mode="cyclic"):
        with exp.sweep(dark):
            with exp.realtime():
                if package:
                    prepare_MOT = package.kernel("prepare_MOT", timing="prefix")
                    if nesting:
                        with getattr(exp, nesting)():
                            prepare_MOT(dark)
                    else:
                        prepare_MOT(dark)
                with exp.triggered_sequence(group="qubit_awg"):
                    trigplay(q.rf, pulse)
                    delay(dark)
                    rz(q, math.pi / 2)
                    trigplay(q.rf, pulse)
                if package:
                    package.kernel(
                        "execute_readout", timing="suffix", duration=8 * us
                    )()
    return exp


def test_native_prefix_does_not_change_control_phase_samples_or_sweep_order(package):
    lab = LabConfig.load(LAB)
    plain = lab.compile(_experiment())
    native = lab.compile(_experiment(package))
    assert "native_fixture" not in sys.modules
    assert list(native.execution_order()) == list(plain.execution_order())
    contract = native.metadata["native_artiq"]
    assert contract["execution"] == "single_top_level_kernel"
    assert "test_native_dsl.py:" in contract["points"][0]["prefix"][0]["source"]
    assert [p["prefix"][0]["args"][0] for p in contract["points"]] == [1e-5, 0.001]
    assert all(p["suffix"][0]["duration_mu"] == 8000 for p in contract["points"])
    assert "native_call" in json.dumps(native.ir)
    for before, after in zip(plain.points, native.points, strict=True):
        assert before.duration_mu == after.duration_mu
        assert before.origin_mu == after.origin_mu
        for left, right in zip(
            before.physical_events, after.physical_events, strict=True
        ):
            assert (left.kind, left.start_mu, left.duration_mu) == (
                right.kind,
                right.start_mu,
                right.duration_mu,
            )
            if left.kind == "spectrum_program":
                for card in left.params["cards"]:
                    p1 = left.params["cards"][card]["program"]
                    p2 = right.params["cards"][card]["program"]
                    assert p1["steps"] == p2["steps"]
                    assert [s["sha256"] for s in p1["segments"]] == [
                        s["sha256"] for s in p2["segments"]
                    ]
        assert native.virtual_z_frames(after.index)["q"] == pytest.approx(math.pi / 2)


@pytest.mark.parametrize("nesting", ["parallel", "serial"])
def test_opaque_calls_are_not_hidden_inside_static_groups(package, nesting):
    with pytest.raises(CompileError, match="direct realtime children"):
        LabConfig.load(LAB).compile(_experiment(package, nesting=nesting))


def test_native_source_pins_are_rechecked_and_no_import_occurs(package):
    exp = _experiment(package)
    path = Path(package.to_dict()["sources"][0]["path"])
    path.write_text(
        SOURCE + "\nraise RuntimeError('must never import')\n", encoding="utf-8"
    )
    with pytest.raises(CompileError, match="hash|SHA|changed|pin"):
        LabConfig.load(LAB).compile(exp)
    assert "native_fixture" not in sys.modules


def test_native_calls_do_not_extend_pure_artiq_backend(package):
    exp = Experiment("old_backend", [])
    with exp.average(1):
        with exp.realtime():
            package.kernel("prepare_MOT", timing="prefix")(20 * us)
            play("out", Square(10 * us))
    with pytest.raises(CompileError, match="operation native_call"):
        compile_experiment(
            exp, DeviceSetup({"out": Device("out", "ttl")}), SignalMap({"out": "out"})
        )


def test_scope_order_duration_and_argument_contracts(package):
    with pytest.raises(ValueError, match="exact duration"):
        package.kernel("execute_readout", timing="suffix")
    exp = Experiment("invalid", [Qubit("q")])
    with exp.average(1):
        with exp.realtime():
            with pytest.raises(TypeError, match="finite literals"):
                package.kernel("prepare_MOT", timing="prefix")(float("nan"))
            with exp.triggered_sequence(group="qubit_awg"):
                with pytest.raises(ValueError, match="triggered_sequence"):
                    package.kernel("prepare_MOT", timing="prefix")(20 * us)
                trigplay("q.rf", Square(200 * us, amplitude=0.2, frequency=30000))
            package.kernel("prepare_MOT", timing="prefix")(20 * us)
    with pytest.raises(CompileError, match="prefix must precede"):
        LabConfig.load(LAB).compile(exp)


def test_native_trigger_resource_alias_is_rejected(package):
    lab = LabConfig.load(LAB)
    group = lab.setup.spectrum.groups["qubit_awg"]
    package._descriptor["resources"].append(group.trigger_device_names[0])
    with pytest.raises(CompileError, match="resources must be disjoint"):
        lab.compile(_experiment(package))


def _timed_experiment(package, gap_us=(10, 11), *, phase_reference="sequence"):
    q = Qubit("q")
    exp = Experiment("anchored_preparation", [q])
    gap = SweepParameter("gap", gap_us, "us")
    prepare = package.kernel("prepare_MOT", timing="prefix")
    finish = package.kernel(
        "finish_preparation",
        timing="timed",
        duration=1000 * us,
        end_boundary="native preparation output switched off",
    )
    with exp.average(2):
        with exp.sweep(gap):
            with exp.realtime():
                prepare(20 * us)
                finish(1000 * us)
                delay(gap)
                with exp.triggered_sequence(
                    group="qubit_awg", phase_reference=phase_reference
                ):
                    trigplay(q.rf, Square(200 * us, amplitude=0.2, frequency=30000))
                package.kernel("execute_readout", timing="suffix", duration=8 * us)()
    return exp


@pytest.mark.parametrize(
    ("reference", "phases"), [("sequence", (0, 0)), ("shot", (108, 118.8))]
)
def test_timed_preparation_gap_and_phase_reference(
    package, tmp_path, reference, phases
):
    compiled = LabConfig.load(LAB).compile(
        _timed_experiment(package, phase_reference=reference)
    )
    for point, gap_us, phase in zip(compiled.points, (10, 11), phases, strict=True):
        calls = compiled.metadata["native_artiq"]["points"][point.index]
        assert len(calls["prefix"]) == len(calls["timed"]) == 1
        timed = calls["timed"][0]
        assert timed["start_mu_from_T0"] == 0
        assert timed["duration_mu"] == 1000000
        assert timed["end_boundary"] == "native preparation output switched off"
        event = next(e for e in point.logical_events if e.kind == "native_timed")
        assert event.start_mu == 0
        assert event.duration_mu * compiled.setup.tick_seconds == pytest.approx(1e-3)
        pulse = next(e for e in point.logical_events if e.kind == "play")
        assert (
            pulse.start_mu - event.end_mu
        ) * compiled.setup.tick_seconds == pytest.approx(gap_us * 1e-6)
        program = next(e for e in point.physical_events if e.kind == "spectrum_program")
        first = program.params["cards"]["A"]["program"]["pulses"][0]
        assert math.degrees(first["phase_rad"]) % 360 == pytest.approx(phase)
        assert program.params["trigger_relative_mu"] == 1000000 + gap_us * 1000 - 512
    result = compiled.export(tmp_path)
    assert result["verification"]["offline"] == "passed"


@pytest.mark.parametrize("gap_us", [0, 0.496])
def test_short_gap_is_rejected_without_moving_preparation_or_gate(package, gap_us):
    with pytest.raises(
        CompileError, match="overlaps the strict native preparation tail"
    ):
        LabConfig.load(LAB).compile(_timed_experiment(package, (gap_us,)))


def test_exact_trigger_lead_gap_is_accepted(package):
    compiled = LabConfig.load(LAB).compile(_timed_experiment(package, (0.512,)))
    program = next(
        e for e in compiled.points[0].physical_events if e.kind == "spectrum_program"
    )
    assert program.params["trigger_relative_mu"] == 1000000
