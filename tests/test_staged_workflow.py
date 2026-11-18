"""Named DSL stages preserve timing; they are not native kernel callbacks."""

import copy
import math
import runpy
from functools import partial
from pathlib import Path

import pytest

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    compile_experiment,
    delay,
    play,
    us,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/artiq_spectrum/staged_workflow.py"


@pytest.fixture(scope="module")
def demo():
    return runpy.run_path(str(EXAMPLE))


def _ttl_program(named):
    q = Qubit("q")
    exp = Experiment("stages", [q])
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                scope = (
                    exp.stage("first branch", exact_duration=30 * us)
                    if named
                    else exp.serial()
                )
                with scope:
                    play("a", Square(10 * us))
                    delay("a", 20 * us)
                play("b", Square(20 * us))
    setup = DeviceSetup({name: Device(name, "ttl") for name in ("a", "b")})
    return compile_experiment(exp, setup, SignalMap({"a": "a", "b": "b"}))


def test_stage_is_one_parallel_branch_without_timing_or_hardware_changes():
    inline, named = _ttl_program(False), _ttl_program(True)
    for attr in ("logical_events", "physical_events"):

        def boundaries(compiled):
            return [
                (
                    event.kind,
                    event.signal,
                    event.start_mu,
                    event.duration_mu,
                    {
                        key: value
                        for key, value in event.params.items()
                        if key != "operation_path"
                    },
                )
                for event in getattr(compiled.points[0], attr)
            ]

        assert boundaries(inline) == boundaries(named)
    assert inline.points[0].duration_mu == named.points[0].duration_mu == 30000
    assert "first branch" in str(named.ir)
    assert named.points[0].logical_events[0].params["operation_path"] == [
        "first branch"
    ]


def test_stage_scope_validation_and_duration_assertion():
    exp = Experiment("invalid stage", [])
    with pytest.raises(ValueError, match="inside realtime"):
        with exp.stage("outside"):
            pass
    with exp.average(1):
        with exp.realtime():
            with pytest.raises(TypeError, match="portable metadata"):
                with exp.stage("invalid metadata", parameters={"live": object()}):
                    pass
            with pytest.raises(ValueError, match="nonempty"):
                with exp.stage(""):
                    pass
            with exp.stage("wrong duration", exact_duration=20 * us):
                play("a", Square(10 * us))
    setup = DeviceSetup({"a": Device("a", "ttl")})
    with pytest.raises(CompileError, match="exact_duration"):
        compile_experiment(exp, setup, SignalMap({"a": "a"}))


def test_real_functions_compile_full_preparation_gate_readout_order(demo, tmp_path):
    exp = demo["build_demo"]()
    lab = LabConfig.load(EXAMPLE.with_name("lab_simulated.json"))
    compiled = lab.compile(exp)
    assert compiled.metadata["stage_implementation"].endswith("not_physical_MOT")
    assert len(compiled.points) == 2
    assert list(compiled.execution_order()) == [(0, 0), (1, 0), (0, 1), (1, 1)]
    for point in compiled.points:
        region = next(e for e in point.logical_events if e.kind == "spectrum_region")
        assert region.start_mu * compiled.setup.tick_seconds == pytest.approx(1610e-6)
        program = next(e for e in point.physical_events if e.kind == "spectrum_program")
        first = program.params["cards"]["A"]["program"]["pulses"][0]
        assert first["phase_rad"] == 0
        triggers = set(lab.setup.spectrum.groups["qubit_awg"].trigger_device_names)
        edges = [e for e in point.physical_events if e.signal in triggers]
        for kind in ("ttl_on", "ttl_off"):
            selected = [e for e in edges if e.kind == kind]
            assert {e.signal for e in selected} == triggers
            assert len(selected) == len(triggers)
            assert len({e.start_mu for e in selected}) == 1
        acquisition = compiled.acquisition_plan(point.index)
        assert acquisition[0]["handle"] == "post"
        assert acquisition[0]["start_s"] == pytest.approx(
            (region.start_mu + region.duration_mu) * compiled.setup.tick_seconds
        )
        assert compiled.virtual_z_frames(point.index)["q"] == pytest.approx(math.pi / 2)
    exported = compiled.export(tmp_path)
    assert exported["verification"]["offline"] == "passed"


def test_builders_run_at_construction_only(demo):
    calls = []

    def fixture(exp, q, p):
        calls.append(p["value"])
        with exp.stage("fixture", parameters=p):
            delay("cooling", 10 * us)

    params = {"value": 7}
    original = copy.deepcopy(params)
    exp = demo["build_experiment"](
        prepare_MOT=fixture,
        cooling=fixture,
        execute_readout=fixture,
        parameters=params,
        shots=3,
    )
    assert calls == [7, 7, 7]
    assert params == original
    params["value"] = 99
    LabConfig.load(EXAMPLE.with_name("lab_simulated.json")).compile(exp)
    assert calls == [7, 7, 7]
    assert exp.parameters["value"] == 7


def test_native_and_asynchronous_stage_callbacks_rejected_before_invocation(demo):
    calls = []

    def native(*args):
        calls.append("must not execute")

    native.artiq_embedded = object()

    async def asynchronous(*args):
        calls.append("must not execute")

    for function in (native, partial(native), asynchronous):
        with pytest.raises(TypeError, match="native ARTIQ|synchronously"):
            demo["build_experiment"](
                prepare_MOT=function,
                cooling=lambda *a: None,
                execute_readout=lambda *a: None,
                parameters={},
            )
    assert calls == []
