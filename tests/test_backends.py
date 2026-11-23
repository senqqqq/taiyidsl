"""A second, test-only target exercises the public backend seam end to end."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from taiyidsl import (
    Backend,
    BackendCapabilities,
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    PhysicalPlan,
    Qubit,
    SignalMap,
    Square,
    compile_experiment,
    get_backend,
    ns,
    play,
    reference_setup,
    register_backend,
)
from taiyidsl.backends.artiq._contracts import ExportError
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.export import export_artiq


class PulseBackend(Backend):
    """Test double: directly emits timed pulses, with no DDS configuration gap."""

    name = "test_pulses"
    version = "1"
    target = "offline pulse test target"
    phase_policy = "pulse_local"
    capabilities = BackendCapabilities(
        device_kinds={"pulse", "dds", "ttl"}, operations={"play", "delay"}
    )

    def lower(self, logical_events, setup, mapping, diagnostics):
        pulses = []
        for event in logical_events:
            if event.kind == "play" and event.duration_mu:
                for binding in mapping.bindings(event.signal):
                    pulses.append(replace(event, kind="pulse", signal=binding.device))
        return PhysicalPlan(pulses, 0)

    def export(self, compiled, output_dir, **options):
        path = Path(output_dir) / "pulses.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(compiled.to_dict()), encoding="utf-8")
        return path


def changing_phase_experiment():
    q = Qubit("q0")
    exp = Experiment("adjacent", [q])
    with exp.average(2):
        with exp.realtime():
            play(q.raman, Square(12 * ns))
            play(q.raman, Square(12 * ns, phase=0.5))
    return q, exp


def pulse_lab(backend=None):
    return LabConfig(
        DeviceSetup({"channel": Device("channel", "pulse", grid_mu=2)}),
        SignalMap({"q0.raman": "channel"}),
        backend=backend or PulseBackend(),
    )


def test_second_target_uses_existing_dsl_without_artiq_constraints(tmp_path):
    q, exp = changing_phase_experiment()
    with pytest.raises(CompileError, match="preparation window|configuration grid"):
        compile_experiment(exp, *reference_setup([q]))
    compiled = pulse_lab().compile(exp)
    assert [
        (e.kind, e.start_mu, e.duration_mu) for e in compiled.points[0].physical_events
    ] == [
        ("pulse", 0, 12),
        ("pulse", 12, 12),
    ]
    assert compiled.points[0].origin_mu == 0
    assert list(compiled.execution_order()) == [(0, 0), (0, 1)]
    saved = json.loads(compiled.export(tmp_path).read_text())
    assert saved["provenance"]["backend"]["name"] == "test_pulses"
    assert saved["provenance"]["phase_policy"] == "pulse_local"
    assert "artiq_compile" not in saved["provenance"]
    assert compiled.ir["kind"] == "experiment"


def test_wrong_exporter_rejected_before_writing(tmp_path):
    _, exp = changing_phase_experiment()
    compiled = pulse_lab().compile(exp)
    for export in (compiled.export_artiq, lambda path: export_artiq(compiled, path)):
        with pytest.raises(ExportError, match="ARTIQ-compiled"):
            export(tmp_path / "wrong")
    with pytest.raises(ExportError, match="ARTIQ-compiled"):
        generate_source(compiled)
    assert not (tmp_path / "wrong").exists()


def test_capabilities_reject_unsupported_device_and_operation():
    _, exp = changing_phase_experiment()
    lab = pulse_lab("artiq")
    with pytest.raises(CompileError, match="device kind pulse"):
        lab.compile(exp)
    restricted = PulseBackend()
    restricted.capabilities = replace(restricted.capabilities, operations={"delay"})
    with pytest.raises(CompileError, match="operation play"):
        pulse_lab(restricted).compile(exp)
    restricted.capabilities = replace(
        PulseBackend.capabilities, pulse_shapes=frozenset()
    )
    with pytest.raises(CompileError, match="pulse shape square"):
        pulse_lab(restricted).compile(exp)


def test_backend_can_supply_target_grid_and_clock():
    class CoarseBackend(PulseBackend):
        def signal_grid(self, signal, setup, mapping):
            return 5

    _, exp = changing_phase_experiment()
    lab = pulse_lab(CoarseBackend())
    lab.setup.tick_seconds = 2e-9
    with pytest.raises(CompileError, match="timing grid"):
        lab.compile(exp)
    compiled = lab.compile(exp, allow_quantization=True)
    assert compiled.points[0].duration_mu == 10  # Two 10 ns pulses, 2 ns ticks.
    assert compiled.diagnostics[0]["actual_s"] == 1e-8


def test_backend_registration_and_compilation_snapshot():
    from taiyidsl.backends import _BACKENDS

    target = PulseBackend()
    name = "registered_test_pulses"
    target.name = name
    try:
        register_backend(target)
        _, exp = changing_phase_experiment()
        compiled = pulse_lab(name).compile(exp)
        assert compiled.provenance["backend"]["name"] == name
        with pytest.raises(ValueError, match="already registered"):
            register_backend(target)
        target.version = "2"
        compiled.assert_integrity()
        assert compiled.provenance["backend"]["version"] == "1"
        compiled._backend.version = "3"
        with pytest.raises(CompileError, match="Backend changed"):
            compiled.assert_integrity()
    finally:
        _BACKENDS.pop(name, None)
    with pytest.raises(NotImplementedError, match="Unknown backend"):
        get_backend("missing_test_target")


def test_default_and_explicit_artiq_export_match(tmp_path):
    q = Qubit("q0")
    exp = Experiment("one", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, Square(12 * ns))
    compiled = compile_experiment(exp, *reference_setup([q]))
    generic = compiled.export(tmp_path / "generic")
    legacy = compiled.export_artiq(tmp_path / "legacy")
    assert generic["source"].read_bytes() == legacy["source"].read_bytes()
    assert generic["verification"] == legacy["verification"]


def test_importing_frontend_and_visualization_does_not_load_artiq():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import taiyidsl; "
            "import taiyidsl.reporting.visualization; "
            "assert 'taiyidsl.backends.artiq' not in sys.modules",
        ],
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_square_subclass_keeps_existing_pulse_semantics():
    class NamedSquare(Square):
        pass

    q = Qubit("q0")
    exp = Experiment("named_square", [q])
    with exp.average(1):
        with exp.realtime():
            play(q.raman, NamedSquare(12 * ns))
    compiled = compile_experiment(exp, *reference_setup([q]))
    assert [e.kind for e in compiled.points[0].physical_events] == [
        "dds_nco_epoch",
        "dds_nco_epoch_off",
        "dds_prepare",
        "dds_commit",
        "dds_commit_off",
        "dds_effect",
        "dds_on",
        "dds_off",
    ]


def test_artiq_device_contracts_are_checked_by_target():
    q, exp = changing_phase_experiment()
    setup, mapping = reference_setup([q])
    setup.devices["urukul0_ch0"] = replace(
        setup.devices["urukul0_ch0"], config_duration_mu=1004
    )
    with pytest.raises(CompileError, match="configuration grid"):
        compile_experiment(exp, setup, mapping)
    lab = pulse_lab()
    lab.setup.devices["channel"] = replace(lab.setup.devices["channel"], sysclk_hz=0)
    assert lab.compile(exp).points[0].duration_mu == 24
