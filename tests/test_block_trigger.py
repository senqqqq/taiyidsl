"""A sequence selects one configured start path; users cannot retrigger it."""

import ast
import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from test_spectrum_backend import group_program, lab

from taiyidsl import (
    CompileError,
    ControlBinding,
    ControlRecipe,
    DeviceCalibration,
    Experiment,
    LabConfig,
    Qubit,
    Square,
    acquire,
    delay,
    ns,
    play,
    rz,
    trigplay,
)
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
from taiyidsl.backends.artiq_spectrum.export import (
    _epochs,
    add_trigger_resource_checks,
    align_source_epochs,
    artiq_subplan,
)
from taiyidsl.dsl import trig


def profile(*, default=None):
    result = lab()
    result.signal_map.update(
        {"qubit.start": "awg_trigger", "start_alias": "awg_trigger"}
    )
    if default is not None:
        config = result.setup.spectrum
        result.setup.spectrum = replace(
            config, groups={"g": replace(config.groups["g"], trigger_signal=default)}
        )
    return result


def sequence(*, trigger=None, manual=None, active=True, after=False):
    q = Qubit("q")
    exp = Experiment("explicit_start", [q])
    with exp.average(1), exp.realtime():
        if manual is not None and not after:
            manual()
        with exp.triggered_sequence(group="g", trigger=trigger):
            if active:
                trigplay(q.rf, Square(400 * ns, frequency=30_000))
                delay(100 * ns)
                rz(q, 0.5)
                trigplay(q.rf, Square(400 * ns, frequency=30_000))
            else:
                rz(q, 0.5)
        if manual is not None and after:
            manual()
    return exp


def test_explicit_default_and_legacy_start_have_identical_samples_and_edges():
    cases = [
        profile().compile(sequence()),
        profile().compile(sequence(trigger="qubit.start")),
        profile(default="qubit.start").compile(sequence()),
        profile(default="qubit.start").compile(sequence(trigger="start_alias")),
    ]
    reference = group_program(cases[0])
    edges = [
        (e.kind, e.signal, e.start_mu, e.duration_mu)
        for e in cases[0].points[0].physical_events
        if e.kind != "spectrum_program"
    ]
    assert [e[0] for e in edges] == ["ttl_on", "ttl_off"]
    for compiled in cases[1:]:
        group = group_program(compiled)
        assert group["trigger_device"] == reference["trigger_device"]
        assert group["trigger_relative_mu"] == reference["trigger_relative_mu"]
        assert group["trigger_signal"] in {"qubit.start", "start_alias"}
        assert [
            (e.kind, e.signal, e.start_mu, e.duration_mu)
            for e in compiled.points[0].physical_events
            if e.kind != "spectrum_program"
        ] == edges
        owned = [
            e for e in compiled.points[0].logical_events if e.params.get("region_id")
        ]
        assert all(e.params["trigger_signal"] == group["trigger_signal"] for e in owned)
        for name, card in group["cards"].items():
            program = card["program"]
            target = reference["cards"][name]["program"]
            assert program["steps"] == target["steps"]
            assert [s["sha256"] for s in program["segments"]] == [
                s["sha256"] for s in target["segments"]
            ]
            np.testing.assert_array_equal(
                reconstruct_window(program, 0, program["physical_duration_samples"]),
                reconstruct_window(target, 0, target["physical_duration_samples"]),
            )


def test_trigger_configuration_roundtrip_does_not_add_legacy_default_fields():
    legacy = profile().to_dict()
    assert "trigger_signal" not in legacy["setup"]["spectrum"]["groups"]["g"]
    config = profile(default="qubit.start")
    restored = LabConfig.from_dict(json.loads(json.dumps(config.to_dict())))
    assert restored.to_dict() == config.to_dict()
    assert restored.setup.spectrum.groups["g"].trigger_signal == "qubit.start"


@pytest.mark.parametrize("trigger", ["missing", "camera", "q.rf"])
def test_unknown_or_different_physical_trigger_is_rejected(trigger):
    with pytest.raises(CompileError, match="trigger|physical path|TTL"):
        profile().compile(sequence(trigger=trigger))


@pytest.mark.parametrize(
    "binding",
    [
        ControlRecipe((ControlBinding("awg_trigger"), ControlBinding("readout"))),
        ControlBinding("awg_trigger", amplitude_scale=0.5),
    ],
)
def test_trigger_does_not_accept_fanout_recipes_or_signal_transforms(binding):
    config = profile()
    config.signal_map["qubit.start"] = binding
    with pytest.raises(CompileError, match="exactly one|transforms"):
        config.compile(sequence(trigger="qubit.start"))


def test_invalid_default_is_rejected_even_when_block_selects_another_alias():
    with pytest.raises(CompileError, match="physical path"):
        profile(default="camera").compile(sequence(trigger="qubit.start"))


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("operation", ["play", "trig", "acquire"])
def test_active_group_owns_its_trigger_for_entire_shot(operation, after):
    def manual():
        if operation == "play":
            play("start_alias", Square(64 * ns))
        elif operation == "trig":
            trig("start_alias", width=64 * ns)
        else:
            acquire("start_alias", handle="camera", duration=64 * ns, qubits=["q"])

    with pytest.raises(CompileError, match="owns trigger device"):
        profile().compile(sequence(manual=manual, after=after))


def test_independent_manual_trigger_and_inactive_frame_only_group_are_supported():
    compiled = profile().compile(sequence(manual=lambda: trig("camera", width=64 * ns)))
    assert sum(e.kind == "ttl_on" for e in compiled.points[0].physical_events) == 2
    frame_only = profile().compile(
        sequence(manual=lambda: trig("start_alias", width=64 * ns), active=False)
    )
    assert not any(
        e.kind == "spectrum_program" for e in frame_only.points[0].physical_events
    )
    assert frame_only.virtual_z_frames()["q"] == 0.5
    assert sum(e.kind == "ttl_on" for e in frame_only.points[0].physical_events) == 1


def test_spectrum_start_requires_idle_low_and_its_own_latency_contract():
    config = profile()
    config.setup.devices["awg_trigger"] = replace(
        config.setup.devices["awg_trigger"], ttl_idle_level=1
    )
    with pytest.raises(CompileError, match="idle-low"):
        config.compile(sequence())
    config = profile()
    config.setup.calibration["awg_trigger"] = DeviceCalibration(latency_mu=8)
    with pytest.raises(CompileError, match="trigger path calibration"):
        config.compile(sequence())


def _guard_namespace(compiled):
    native = artiq_subplan(compiled)
    original = align_source_epochs(
        generate_source(native, mode="unrolled"), _epochs(compiled, native)
    )
    guarded = add_trigger_resource_checks(original, compiled)
    before, after = ast.parse(original), ast.parse(guarded)
    original_class = next(n for n in before.body if isinstance(n, ast.ClassDef))
    guarded_class = next(n for n in after.body if isinstance(n, ast.ClassDef))
    for method in original_class.body:
        if isinstance(method, ast.FunctionDef) and method.name.startswith("point_"):
            other = next(
                n for n in guarded_class.body if getattr(n, "name", None) == method.name
            )
            assert ast.dump(method, include_attributes=False) == ast.dump(
                other, include_attributes=False
            )
    validator = next(
        n for n in guarded_class.body if getattr(n, "name", None) == "_validate_target"
    )
    assert ast.unparse(validator.body[-1]).startswith(
        "_taiyi_validate_trigger_resources(self,"
    )
    helpers = [
        n
        for n in after.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("_taiyi_")
    ]
    namespace = {}
    exec(
        compile(ast.Module(body=helpers, type_ignores=[]), "trigger_guard", "exec"),
        namespace,
    )
    return namespace, validator.body[-1]


@pytest.mark.parametrize("alias", ["same_object", "same_channel", "sw", "io_update"])
def test_exported_host_target_rejects_real_device_aliases(alias):
    compiled = profile().compile(
        sequence(manual=lambda: play("camera", Square(64 * ns)))
    )
    namespace, invocation = _guard_namespace(compiled)
    trigger = SimpleNamespace(channel=42)
    if alias == "same_object":
        other = trigger
    elif alias == "same_channel":
        other = SimpleNamespace(channel=42)
    else:
        other = SimpleNamespace(**{alias: SimpleNamespace(channel=42)})
    devices = {"awg_trigger": trigger, "readout": other}
    namespace["self"] = SimpleNamespace(get_device=devices.__getitem__)
    with pytest.raises(RuntimeError, match="trigger hardware resource alias"):
        exec(
            compile(ast.Module(body=[invocation], type_ignores=[]), "target", "exec"),
            namespace,
        )
    devices["readout"] = SimpleNamespace(channel=43)
    exec(
        compile(ast.Module(body=[invocation], type_ignores=[]), "target", "exec"),
        namespace,
    )
