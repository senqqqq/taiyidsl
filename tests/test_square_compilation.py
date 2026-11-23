import pytest

from examples.pulse_pair import pulse_pair
from examples.schedule_pulse_pair import example_wiring
from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    SignalMap,
    Square,
    compile_experiment,
    delay,
    ns,
    play,
    us,
)
from taiyidsl.backends.artiq._contracts import ExportError
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_export


def test_legacy_tracking_kernel_matches_physical_edges_and_registers():
    compiled = compile_experiment(pulse_pair(), *example_wiring())
    source = generate_source(compiled, mode="unrolled")
    report = verify_export(compiled, source=source)
    assert report["points"][0]["events"] == 6
    observed = compiled.points[0].physical_events
    (point,) = compiled.points
    assert point.origin_mu == 3000
    assert [(e.kind, e.start_mu) for e in observed] == [
        ("dds_config", 0),
        ("dds_on", 3000),
        ("dds_off", 7000),
        ("dds_config", 16000),
        ("dds_on", 19000),
        ("dds_off", 23000),
    ]
    configs = [e for e in observed if e.kind == "dds_config"]
    assert [
        (e.params["ftw"], e.params["pow"], e.params["asf"], e.params["ref_time_mu"])
        for e in configs
    ] == [
        (42949673, 0, 4096, 3000),
        (42949673, 5215, 4096, 3000),
    ]
    with pytest.raises(ExportError):
        verify_export(compiled, source=source.replace("pow_=5215", "pow_=5214"))


def test_short_gap_cannot_accommodate_dds_reconfiguration():
    experiment = Experiment("short", [])
    with experiment.average(1), experiment.realtime():
        play("q0.rf", Square(4 * us, amplitude=0.5))
        delay("q0.rf", 8 * ns)
        play("q0.rf", Square(4 * us, amplitude=0.25))
    with pytest.raises(CompileError, match="preparation window"):
        compile_experiment(experiment, *example_wiring())


def test_ttl_program_executes_both_edges_and_trailing_wait():
    experiment = Experiment("ttl", [])
    with experiment.average(1), experiment.realtime():
        play("trigger", Square(4 * us))
        delay("trigger", 1 * us)
    compiled = compile_experiment(
        experiment,
        DeviceSetup({"ttl0": Device("ttl0", "ttl")}),
        SignalMap({"trigger": "ttl0"}),
    )
    report = verify_export(compiled)
    assert report["points"][0]["events"] == 2
    observed = compiled.points[0].physical_events
    assert [(e.kind, e.start_mu) for e in observed] == [
        ("ttl_on", 0),
        ("ttl_off", 4000),
    ]
    assert compiled.points[0].duration_mu == 5000
