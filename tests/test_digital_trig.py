"""Digital triggers preserve polarity, exact timing and independent edges."""

import json
from dataclasses import replace

import pytest

from taiyidsl import (
    CompileError,
    ControlBinding,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    SweepParameter,
    delay,
    ns,
    play,
    trig,
)
from taiyidsl.backends.artiq.verification import verify_export
from taiyidsl.reporting.visualization import _physical_traces


def lab(idle=0):
    return LabConfig(
        DeviceSetup({"ttl0": Device("ttl0", "ttl", grid_mu=1, ttl_idle_level=idle)}),
        SignalMap({"scope.trigger": "ttl0", "alias": "ttl0"}),
    )


def experiment_with(action):
    exp = Experiment("digital_trigger", [Qubit("q")])
    with exp.average(2), exp.realtime():
        action(exp)
    return exp


@pytest.mark.parametrize("idle", [0, 1])
def test_polarity_sweep_initialization_and_export(idle, tmp_path):
    profile = lab(idle)
    width = SweepParameter("width", [8, 16], "ns")
    exp = Experiment("trig_scan", [Qubit("q")])
    with exp.average(2), exp.sweep(width), exp.realtime():
        trig("scope.trigger", width=width)
        delay("scope.trigger", 16 * ns)
        trig("scope.trigger", width=8 * ns)
    compiled = profile.compile(exp)
    first_edge, last_edge = ("ttl_off", "ttl_on") if idle else ("ttl_on", "ttl_off")
    for point, length in zip(compiled.points, (8, 16), strict=True):
        assert [(e.kind, e.start_mu) for e in point.physical_events] == [
            (first_edge, 0),
            (last_edge, length),
            (first_edge, length + 16),
            (last_edge, length + 24),
        ]
        assert point.duration_mu == length + 24
        assert [e.kind for e in point.logical_events] == ["trig", "delay", "trig"]
        traces = _physical_traces(point.physical_events, 1e-9)
        spans = [t for t in traces if t["name"] == "ttl_output"]
        assert len(spans) == 2
        assert spans[0]["meta"]["details"]["duration_mu"] == length
        assert (
            spans[0]["meta"]["details"]["parameters"]["digital_active_level"]
            == 1 - idle
        )
    for mode in ("unrolled", "auto"):
        exported = compiled.export(tmp_path / mode, mode=mode)
        assert (
            verify_export(compiled, exported["source"])["offline_export_check"]
            == "passed"
        )
        assert exported["verification"]["initialization"]["status"] == "passed"
    encoded = profile.to_dict()
    assert LabConfig.from_dict(json.loads(json.dumps(encoded))).to_dict() == encoded
    assert ("ttl_idle_level" in encoded["setup"]["devices"]["ttl0"]) == bool(idle)


def test_trig_and_legacy_square_have_same_edges():
    profile = lab()
    direct = profile.compile(
        experiment_with(lambda _: trig("scope.trigger", width=16 * ns))
    )
    old = profile.compile(
        experiment_with(lambda _: play("scope.trigger", Square(16 * ns)))
    )
    assert [(e.kind, e.start_mu) for e in direct.points[0].physical_events] == [
        (e.kind, e.start_mu) for e in old.points[0].physical_events
    ]


def test_idle_high_trigger_with_polar_dds_export(tmp_path):
    from test_mixed_polar_backend import envelope, mixed_lab

    qubits, profile = mixed_lab(asf=False)
    profile.setup.devices["scope"] = Device("scope", "ttl", ttl_idle_level=1)
    profile.signal_map["scope.trigger"] = "scope"
    exp = Experiment("polar_and_trig", qubits)
    with exp.average(1), exp.realtime(), exp.parallel():
        play(qubits[0].raman, envelope(800))
        trig("scope.trigger", width=800 * ns)
    compiled = profile.compile(exp)
    edges = [e for e in compiled.points[0].physical_events if e.signal == "scope"]
    assert [(e.kind, e.start_mu - compiled.points[0].origin_mu) for e in edges] == [
        ("ttl_off", 0),
        ("ttl_on", 800),
    ]
    exported = compiled.export(tmp_path)
    assert (
        verify_export(compiled, exported["source"])["offline_export_check"] == "passed"
    )


def test_trig_requires_ttl_identity_binding():
    exp = experiment_with(lambda _: trig("scope.trigger", width=16 * ns))
    profile = lab()
    profile.setup.devices["ttl0"] = Device("ttl0", "dds")
    with pytest.raises(CompileError, match="TTL"):
        profile.compile(exp)
    profile = lab()
    profile.signal_map["scope.trigger"] = ControlBinding("ttl0", amplitude_scale=0.5)
    with pytest.raises(CompileError, match="identity"):
        profile.compile(exp)


def test_invalid_width_and_physical_conflicts_are_rejected():
    for width in (0 * ns, 2 * ns, 8.5 * ns):
        with pytest.raises(CompileError):
            lab().compile(experiment_with(lambda _: trig("scope.trigger", width=width)))

    def overlap(exp):
        with exp.parallel():
            trig("scope.trigger", width=16 * ns)
            trig("alias", width=8 * ns)

    with pytest.raises(CompileError, match="conflict"):
        lab().compile(experiment_with(overlap))

    def adjacent(_):
        trig("scope.trigger", width=8 * ns)
        trig("scope.trigger", width=8 * ns)

    with pytest.raises(CompileError, match="gap"):
        lab(1).compile(experiment_with(adjacent))


def test_scope_and_idle_state_are_explicit():
    exp = Experiment("scope", [Qubit("q")])
    with exp.average(1):
        with pytest.raises(ValueError, match="realtime"):
            trig("scope.trigger", width=8 * ns)
        with exp.realtime(), exp.triggered_sequence(group="g"):
            with pytest.raises(ValueError, match="triggered_sequence"):
                trig("scope.trigger", width=8 * ns)
    with pytest.raises(CompileError, match="idle-high"):
        lab(1).compile(experiment_with(lambda _: play("scope.trigger", Square(8 * ns))))
    for level in (True, 0.0, -1, 2):
        with pytest.raises(ValueError, match="ttl_idle_level"):
            replace(lab().setup.devices["ttl0"], ttl_idle_level=level)
    with pytest.raises(ValueError, match="TTL"):
        Device("dds", "dds", ttl_idle_level=1)
