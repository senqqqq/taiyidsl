import json
from dataclasses import dataclass, field, replace

import pytest

from taiyidsl import artiq as artiq_backend
from taiyidsl.artiq import ExportError, export_artiq, generate_source, verify_export
from taiyidsl.compiler import compile_experiment
from taiyidsl.dsl import Experiment, Square, delay, play, shift_phase
from taiyidsl.hardware import reference_setup
from taiyidsl.models import Qubit
from taiyidsl.templates import rabi, ramsey
from taiyidsl.units import us


@dataclass
class DeviceFixture:
    name: str
    kind: str
    config_duration_mu: int = 3000
    sysclk_hz: float = 1e9
    spi_ref_period_mu: int = 8
    update_group: str | None = None
    bus: str | None = None
    nco_mode: str = "legacy_tracking"


@dataclass
class SetupFixture:
    devices: dict
    tick_seconds: float = 1e-9
    rtio_coarse_mu: int = 8
    rtio_lane_count: int = 8


@dataclass
class EventFixture:
    kind: str
    signal: str
    start_mu: int
    duration_mu: int = 0
    params: dict = field(default_factory=dict)
    source: str = "test"
    resources: tuple = ()
    logical_ids: tuple = ()

    @property
    def end_mu(self):
        return self.start_mu + self.duration_mu


@dataclass
class PointFixture:
    index: int
    physical_events: list
    logical_events: list = field(default_factory=list)
    coordinates: dict = field(default_factory=lambda: {"duration": 1e-5})
    axis_indices: tuple = (0,)
    duration_mu: int = 11000
    origin_mu: int = 3000


@dataclass
class CompiledFixture:
    name: str
    points: list
    setup: SetupFixture
    shots: int = 2
    averaging_mode: str = "cyclic"
    axes: list = field(
        default_factory=lambda: [{"name": "duration", "values": [1e-5], "unit": "s"}]
    )
    provenance: dict = field(default_factory=lambda: {"reference_only": True})
    diagnostics: list = field(default_factory=list)


def make_compiled():
    events = [
        EventFixture(
            "dds_config",
            "urukul0_ch0",
            0,
            3000,
            {"frequency": 80e6, "amplitude": 0.5, "phase": 0, "ref_time_mu": 3000},
        ),
        EventFixture("dds_on", "urukul0_ch0", 3000),
        EventFixture("dds_off", "urukul0_ch0", 13000),
        EventFixture("ttl_on", "ttl0", 13000),
        EventFixture("ttl_off", "ttl0", 14000),
    ]
    point = PointFixture(
        0,
        events,
        [
            EventFixture(
                "play",
                "q0.raman",
                0,
                10000,
                {"frequency": 80e6, "amplitude": 0.5, "phase": 0},
            )
        ],
    )
    return CompiledFixture(
        "rabi",
        [point],
        SetupFixture(
            {
                "urukul0_ch0": DeviceFixture("urukul0_ch0", "dds"),
                "ttl0": DeviceFixture("ttl0", "ttl"),
            }
        ),
    )


@pytest.fixture
def compiled_fixture():
    return make_compiled()


def test_export_and_independent_mock_reconstruction(compiled_fixture, tmp_path):
    artifact = export_artiq(compiled_fixture, tmp_path)
    assert artifact["verification"]["offline_export_check"] == "passed"
    manifest = json.loads(artifact["manifest"].read_text(encoding="utf-8"))
    assert manifest["verification"]["artiq_compile"] == "not_verified"
    assert manifest["verification"]["hardware"] == "not_verified"
    assert manifest["dds_register_quantization"][0]["words"]["asf"] == 8192
    assert "self.core.wait_until_mu(now_mu())" in artifact["source"].read_text()
    assert (
        verify_export(compiled_fixture, artifact["source"])["python_syntax"] == "passed"
    )


def test_mock_detects_wrong_edge_in_generated_code(compiled_fixture):
    source = generate_source(compiled_fixture).replace(".sw.on()", ".sw.off()")
    with pytest.raises(ExportError, match="differ"):
        verify_export(compiled_fixture, source=source)


def test_mock_detects_wrong_reference_phase_mode(compiled_fixture):
    source = generate_source(compiled_fixture).replace(
        "phase_mode=PHASE_MODE_TRACKING", "phase_mode=-1"
    )
    with pytest.raises(ExportError, match="tracking"):
        verify_export(compiled_fixture, source=source)


def test_overlapping_blocking_driver_call_is_rejected(compiled_fixture):
    compiled_fixture.points[0].physical_events.insert(
        1, EventFixture("ttl_on", "ttl0", 500)
    )
    with pytest.raises(ExportError, match="opaque driver submission"):
        generate_source(compiled_fixture)


def test_device_contract_duration_is_not_taken_from_event(compiled_fixture):
    compiled_fixture.points[0].physical_events[0].duration_mu = 500
    with pytest.raises(ExportError, match="driver contract"):
        generate_source(compiled_fixture)


def test_mock_detects_wrong_tuning_word(compiled_fixture):
    source = generate_source(compiled_fixture).replace("asf=8192", "asf=8191")
    with pytest.raises(ExportError, match="differ"):
        verify_export(compiled_fixture, source=source)


def test_code_has_compact_host_average_loops(compiled_fixture):
    source = generate_source(compiled_fixture)
    assert "for shot in range(2):\n            for point in range(1):" in source
    compiled_fixture.averaging_mode = "sequential"
    source = generate_source(compiled_fixture)
    assert "for point in range(1):\n            for shot in range(2):" in source
    assert "if self.result_adapter is None" in source


def test_invalid_dds_parameters_fail(compiled_fixture):
    compiled_fixture.points[0].physical_events[0].params["amplitude"] = 2
    with pytest.raises(ExportError, match="range"):
        generate_source(compiled_fixture)


@pytest.mark.parametrize(
    "template,values",
    [(rabi, [0 * us, 4 * us, 8 * us]), (ramsey, [0 * us, 2 * us, 8 * us])],
)
def test_real_template_compiler_export_roundtrip(template, values, tmp_path):
    qubit = Qubit("q0")
    setup, mapping = reference_setup([qubit])
    compiled = compile_experiment(template(qubit, values, shots=3), setup, mapping)
    artifact = export_artiq(compiled, tmp_path)
    saved = json.loads(artifact["compiled"].read_text(encoding="utf-8"))
    assert saved["provenance"]["content_hash"] == compiled.provenance["content_hash"]
    assert len(saved["points"]) == 3
    assert all(p["status"] == "passed" for p in artifact["verification"]["points"])


def test_real_phase_update_in_declared_gap_exports_correct_words(tmp_path):
    import math

    qubit = Qubit("q0")
    setup, mapping = reference_setup([qubit])
    exp = Experiment("phase", [qubit])
    with exp.average(shots=1), exp.realtime():
        play(qubit.raman, Square(8 * us))
        delay(qubit.raman, 2 * us)
        shift_phase(qubit.frame, math.pi / 2)
        play(qubit.raman, Square(8 * us))
    compiled = compile_experiment(exp, setup, mapping)
    artifact = export_artiq(compiled, tmp_path)
    manifest = json.loads(artifact["manifest"].read_text())
    quantization = manifest["dds_register_quantization"]
    assert [
        item["words"]["pow"]
        for item in quantization
        if item["device"] == mapping[qubit.raman]
    ] == [0, 16384]
    # A frame command also updates the mapped, RF-off drive that has no play.
    assert [
        item["words"]["pow"]
        for item in quantization
        if item["device"] == mapping[qubit.rf]
    ] == [16384]
    assert (
        verify_export(compiled, artifact["source"])["offline_export_check"] == "passed"
    )


def test_phase_change_without_gap_keeps_continuous_gate():
    qubit = Qubit("q0")
    setup, mapping = reference_setup([qubit])
    exp = Experiment("continuous", [qubit])
    with exp.average(shots=1), exp.realtime():
        play(qubit.raman, Square(8 * us, phase=0))
        play(qubit.raman, Square(8 * us, phase=1))
    compiled = compile_experiment(exp, setup, mapping)
    point = compiled.points[0]
    assert [
        (e.kind, e.start_mu - point.origin_mu)
        for e in point.physical_events
        if e.kind in {"dds_on", "dds_off"}
    ] == [("dds_on", 0), ("dds_off", 16000)]
    assert [
        e.start_mu - point.origin_mu
        for e in point.physical_events
        if e.kind == "dds_effect"
    ] == [0, 8000]
    assert verify_export(compiled)["offline_export_check"] == "passed"


def test_shared_board_initial_configuration_and_explicit_origin_alignment():
    q0, q1 = Qubit("q0"), Qubit("q1")
    setup, mapping = reference_setup([q0, q1])
    for q in (q0, q1):
        name = mapping[q.raman]
        setup.devices[name] = replace(
            setup.devices[name],
            bus="shared_spi",
            update_group="shared_board",
            grid_mu=8,
        )
    exp = Experiment("parallel", [q0, q1])
    with exp.average(shots=1), exp.realtime(), exp.parallel():
        play(q0.raman, Square(8 * us))
        play(q1.raman, Square(8 * us))
    compiled = compile_experiment(exp, setup, mapping)
    events = compiled.points[0].physical_events
    configs = [e for e in events if e.kind == "dds_prepare"]
    assert configs[0].start_mu + configs[0].duration_mu <= configs[1].start_mu
    assert len({e.start_mu for e in events if e.kind == "dds_on"}) == 1
    source = generate_source(compiled)
    assert "t0 = ((now_mu() + 7) // 8) * 8" in source
    assert verify_export(compiled, source=source)["offline_export_check"] == "passed"
    unaligned = source.replace("t0 = ((now_mu() + 7) // 8) * 8", "t0 = now_mu()")
    with pytest.raises(ExportError, match="grid"):
        verify_export(compiled, source=unaligned)


def test_emitted_budget_guard_detects_driver_that_exceeds_contract(
    compiled_fixture, monkeypatch
):
    source = generate_source(compiled_fixture)
    original = artiq_backend._MockDDS.set_mu

    def slower_driver(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.clock.cursor += self.device.config_duration_mu - (332 * 8 + 8) + 8

    monkeypatch.setattr(artiq_backend._MockDDS, "set_mu", slower_driver)
    with pytest.raises(ValueError, match="exceeded declared configuration budget"):
        verify_export(compiled_fixture, source=source)


def test_emitted_target_check_rejects_wrong_core_clock(compiled_fixture, monkeypatch):
    source = generate_source(compiled_fixture)
    original = artiq_backend._MockCore.__init__

    def wrong_clock(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.ref_period *= 2

    monkeypatch.setattr(artiq_backend._MockCore, "__init__", wrong_clock)
    with pytest.raises(ValueError, match="Core reference period differs"):
        verify_export(compiled_fixture, source=source)


def test_emitted_target_check_rejects_wrong_dds_clock(compiled_fixture, monkeypatch):
    source = generate_source(compiled_fixture)
    original = artiq_backend._MockDDS.__init__

    def wrong_clock(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.sysclk *= 0.8

    monkeypatch.setattr(artiq_backend._MockDDS, "__init__", wrong_clock)
    with pytest.raises(ValueError, match="DDS clock differs"):
        verify_export(compiled_fixture, source=source)
