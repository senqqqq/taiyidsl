"""A synchronous group may use one shared trigger or one TTL per card."""

import ast
import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from test_block_trigger import _guard_namespace
from test_spectrum_backend import group_program, lab, make_ramsey

from taiyidsl import (
    CompileError,
    ControlBinding,
    ControlRecipe,
    Device,
    Experiment,
    LabConfig,
    Qubit,
    Square,
    ns,
    play,
    trig,
)
from taiyidsl.spectrum_export import artiq_subplan
from taiyidsl.spectrum_runtime import hardware_fingerprint
from taiyidsl.spectrum_waveforms import reconstruct_window


def dual_lab(*, shared=False):
    profile = lab()
    for name in ("start_a", "start_b"):
        profile.setup.devices[name] = Device(name, "ttl", grid_mu=1)
    group = profile.setup.spectrum.groups["g"]
    routes = {"A": "start_a", "B": "start_a" if shared else "start_b"}
    profile.setup.spectrum = replace(
        profile.setup.spectrum,
        groups={
            "g": replace(
                group,
                trigger_device=None,
                trigger_devices=routes,
                trigger_signal="awg.start",
            )
        },
    )
    profile.signal_map["awg.start"] = ControlRecipe(
        tuple(ControlBinding(name) for name in sorted(set(routes.values())))
    )
    return profile


def experiment(*, trigger=None, manual=None):
    q = Qubit("q")
    exp = Experiment("two_start_lines", [q])
    with exp.average(1), exp.realtime():
        if manual is not None:
            trig(manual, width=64 * ns)
        with exp.triggered_sequence(group="g", trigger=trigger):
            play(q.rf, Square(400 * ns, frequency=30_000))
    return exp


def test_dual_ttls_have_identical_edges_and_per_card_contracts():
    compiled = dual_lab().compile(experiment(trigger="awg.start"))
    group = group_program(compiled)
    assert group["trigger_devices"] == {"A": "start_a", "B": "start_b"}
    assert group["trigger_device"] is None
    assert len(group["trigger_routes"]) == 2
    assert group["cards"]["A"]["contract"]["trigger_device"] == "start_a"
    assert group["cards"]["B"]["contract"]["trigger_device"] == "start_b"
    edges = {
        name: [
            (e.kind, e.start_mu)
            for e in compiled.points[0].physical_events
            if e.signal == name
        ]
        for name in ("start_a", "start_b")
    }
    assert edges["start_a"] == edges["start_b"]
    assert [kind for kind, _ in edges["start_a"]] == ["ttl_on", "ttl_off"]
    native = artiq_subplan(compiled)
    assert len([e for e in native.points[0].logical_events if e.kind == "trigger"]) == 2
    assert {
        native.signal_map[e.signal]
        for e in native.points[0].logical_events
        if e.kind == "trigger"
    } == {"start_a", "start_b"}


def test_explicit_shared_routes_are_deduplicated_without_changing_samples():
    legacy = lab().compile(make_ramsey())
    shared = dual_lab(shared=True).compile(make_ramsey())
    group = group_program(shared)
    assert len(group["trigger_routes"]) == 1
    assert group["trigger_routes"][0]["signal"] == "__spectrum_trigger_g"
    assert (
        len(
            [
                e
                for e in shared.points[0].physical_events
                if e.kind == "ttl_on" and e.signal == "start_a"
            ]
        )
        == 1
    )
    for name, card in group["cards"].items():
        expected = group_program(legacy)["cards"][name]["program"]
        program = card["program"]
        assert program["steps"] == expected["steps"]
        assert [s["sha256"] for s in program["segments"]] == [
            s["sha256"] for s in expected["segments"]
        ]
        start = program["pulses"][0]["start_sample"] if program["pulses"] else 0
        np.testing.assert_array_equal(
            reconstruct_window(program, start, start + 64),
            reconstruct_window(expected, start, start + 64),
        )


def test_trigger_route_changes_are_bound_into_card_calibration_fingerprint():
    old = group_program(lab().compile(experiment()))["cards"]["A"]["contract"]
    new = group_program(dual_lab().compile(experiment()))["cards"]["A"]["contract"]
    assert hardware_fingerprint(old) != hardware_fingerprint(new)


def test_dual_configuration_roundtrip_and_card_route_coverage():
    profile = dual_lab()
    encoded = json.loads(json.dumps(profile.to_dict()))
    restored = LabConfig.from_dict(encoded)
    assert restored.to_dict() == encoded
    group = restored.setup.spectrum.groups["g"]
    assert group.trigger_device_names == ("start_a", "start_b")
    with pytest.raises(ValueError, match="exactly one"):
        replace(group, trigger_device="awg_trigger")
    with pytest.raises(ValueError, match="exactly.*cards"):
        replace(group, trigger_devices={"A": "start_a"})


@pytest.mark.parametrize(
    "names", [("start_a",), ("start_a", "readout"), ("start_a", "start_b", "readout")]
)
def test_explicit_trigger_must_match_complete_configured_set(names):
    profile = dual_lab()
    profile.signal_map["other.start"] = ControlRecipe(
        tuple(ControlBinding(n) for n in names)
    )
    with pytest.raises(CompileError, match="configured.*physical path"):
        profile.compile(experiment(trigger="other.start"))


def test_dual_trigger_aliases_and_transforms_cannot_bypass_ownership():
    profile = dual_lab()
    profile.signal_map["manual"] = "start_b"
    with pytest.raises(CompileError, match="owns trigger device"):
        profile.compile(experiment(manual="manual"))
    profile.signal_map["awg.start"] = ControlRecipe(
        (ControlBinding("start_a"), ControlBinding("start_b", phase_scale=-1))
    )
    with pytest.raises(CompileError, match="transforms"):
        profile.compile(experiment())


def test_every_trigger_line_has_to_satisfy_polarity_and_time_grid():
    profile = dual_lab()
    profile.setup.devices["start_b"] = replace(
        profile.setup.devices["start_b"], ttl_idle_level=1
    )
    with pytest.raises(CompileError, match="idle-low"):
        profile.compile(experiment())
    profile.setup.devices["start_b"] = Device("start_b", "ttl", grid_mu=3)
    with pytest.raises(CompileError, match="trigger width.*grid"):
        profile.compile(experiment())


@pytest.mark.parametrize("conflict", ["start_a", "readout"])
def test_second_trigger_cannot_alias_first_trigger_or_another_physical_output(conflict):
    compiled = dual_lab().compile(experiment(manual="camera"))
    namespace, invocation = _guard_namespace(compiled)
    devices = {
        "start_a": SimpleNamespace(channel=20),
        "start_b": SimpleNamespace(channel=21),
        "readout": SimpleNamespace(channel=22),
    }
    namespace["self"] = SimpleNamespace(get_device=devices.__getitem__)
    code = compile(ast.Module(body=[invocation], type_ignores=[]), "dual_guard", "exec")
    exec(code, namespace)
    devices["start_b"] = SimpleNamespace(channel=devices[conflict].channel)
    with pytest.raises(RuntimeError, match="trigger hardware resource alias"):
        exec(code, namespace)
