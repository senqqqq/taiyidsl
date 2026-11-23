"""Execute composed kernels against a clock, without running native hardware."""

import ast
import hashlib
import math
import runpy
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from test_spectrum_backend import group_program, lab

from taiyidsl import (
    Experiment,
    Qubit,
    Square,
    delay,
    ns,
    reset_frame,
    reset_phase,
    rz,
    shift_phase,
    trigplay,
)
from taiyidsl.backends.artiq._contracts import ExportError
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.mock_drivers import _fake_artiq
from taiyidsl.backends.artiq.verification import verify_export
from taiyidsl.backends.artiq_spectrum.export import (
    _epochs,
    align_source_epochs,
    artiq_subplan,
)
from taiyidsl.native.export import compose_native_source

PACKAGE = """from artiq.experiment import EnvExperiment, kernel, at_mu, now_mu

class NativeRoutines(EnvExperiment):
    def build(self):
        self.native_device = self.get_device("native_ttl")
        self.seen = []

    @kernel
    def initialize_native(self):
        self.core.reset()
        self.native_device.off()

    @kernel
    def prepare_MOT(self):
        self.native_device.on()
        at_mu(now_mu() + 16)
        self.core.break_realtime()

    @kernel
    def cooling(self):
        self.native_device.off()
        at_mu(now_mu() + 24)

    @kernel
    def execute_readout(self, duration):
        self.seen.append((now_mu(), self.native_control_t0_mu,
                          self.native_terminal_h_q, self.native_terminal_v_q))
        self.native_device.on()
        at_mu(now_mu() + duration)
        self.native_device.off()

    @kernel
    def cleanup(self):
        self.native_device.off()
        self.core.wait_until_mu(now_mu())
        at_mu(now_mu() + 200)
"""


def _fixture(
    tmp_path,
    *,
    suffix_duration=8,
    suffix_argument=8,
    finalize=True,
    package_source=PACKAGE,
    timed_gap=None,
    timed_duration=24,
    phase_reference="sequence",
    terminal_reset=None,
):
    source_path = tmp_path / "native_export_fixture.py"
    source_path.write_text(package_source, encoding="utf-8")
    q = Qubit("q")
    exp = Experiment("native_composition", [q])
    with exp.average(2), exp.realtime():
        if timed_gap is not None:
            # A static idle placeholder isolates exporter tests from the native
            # frontend. The explicit contract below replaces it with real calls.
            delay("camera", (timed_duration + timed_gap) * ns)
        with exp.triggered_sequence(group="g", phase_reference=phase_reference):
            trigplay(q.rf, Square(400 * ns, frequency=150e6, amplitude=0.2))
            delay(1000 * ns)
            shift_phase(q.uid, 0.2)
            rz(q, math.pi / 2)
            trigplay(q.rf, Square(400 * ns, frequency=150e6, amplitude=0.2))
            if terminal_reset is not None:
                terminal_reset(q.frame)
    compiled = lab().compile(exp)
    contract = {
        "schema_version": 1,
        "package": {
            "module": "native_export_fixture",
            "class_name": "NativeRoutines",
            "sources": [
                {
                    "module": "native_export_fixture",
                    "path": str(source_path),
                    "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                }
            ],
            "build_method": "build",
            "initialize_method": "initialize_native",
            "finalize_method": "cleanup" if finalize else None,
            "resources": ["native_ttl"],
            "phase_reference": "control_epoch",
        },
        "points": [
            {
                "index": 0,
                "prefix": [
                    {"method": "prepare_MOT", "args": [], "source": "test"},
                    {"method": "cooling", "args": [], "source": "test"},
                ],
                "suffix": [
                    {
                        "method": "execute_readout",
                        "args": [suffix_argument],
                        "source": "test",
                        "duration_mu": suffix_duration,
                        "phase_sensitive": True,
                    }
                ],
            }
        ],
    }
    if timed_gap is not None:
        declaration = contract["points"][0]
        declaration["prefix"] = declaration["prefix"][:1]
        declaration["timed"] = [
            {
                "method": "cooling",
                "args": [],
                "source": "test:strict_cooling",
                "start_mu_from_T0": 0,
                "duration_mu": timed_duration,
                "phase_sensitive": False,
                "end_boundary": "final preparation digital IO complete",
            }
        ]
    compiled = replace(
        compiled, metadata={**compiled.metadata, "native_artiq": contract}
    )
    native = artiq_subplan(compiled)
    epochs = _epochs(compiled, native)
    standalone = align_source_epochs(generate_source(native, mode="unrolled"), epochs)
    source, report = compose_native_source(compiled, native, standalone, epochs)
    return compiled, native, standalone, source, report, source_path


TIMED_PACKAGE = PACKAGE.replace(
    "    def cooling(self):\n"
    "        self.native_device.off()\n"
    "        at_mu(now_mu() + 24)",
    "    def cooling(self):\n"
    "        self.native_device.off()\n"
    "        at_mu(now_mu() + 24)\n"
    "        self.native_device.off()",
)


class Clock:
    def __init__(self, work_mu):
        self.cursor = 0
        self.counter = 1000
        self.work_mu = work_mu
        self.trace = []

    def at_mu(self, value):
        self.cursor = int(value)


class Core:
    def __init__(self, clock):
        self.clock = clock
        self.ref_period = 1e-9
        self.ref_multiplier = 8

    def reset(self):
        self.clock.trace.append(("reset", self.clock.cursor))
        self.clock.cursor = self.clock.counter + 125000

    def break_realtime(self):
        self.clock.cursor = max(self.clock.cursor, self.clock.counter + 125000)
        self.clock.trace.append(("break", self.clock.cursor))

    def wait_until_mu(self, value):
        self.clock.trace.append(("wait", int(value)))
        self.clock.counter = max(self.clock.counter, int(value))


class TTL:
    def __init__(self, clock, name):
        self.clock, self.name = clock, name

    def on(self):
        self.clock.trace.append((self.name + ".on", self.clock.cursor))
        if self.name == "native":
            self.clock.counter += self.clock.work_mu

    def off(self):
        self.clock.trace.append((self.name + ".off", self.clock.cursor))


def _run(source, tmp_path, monkeypatch, *, work_mu=0, cached=None, alias=False):
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "native_export_fixture", raising=False)
    if cached is not None:
        monkeypatch.setitem(sys.modules, "native_export_fixture", cached)
    clock = Clock(work_mu)
    devices = {
        "core": Core(clock),
        "awg_trigger": TTL(clock, "trigger"),
        "native_ttl": TTL(clock, "native"),
    }
    if alias:
        devices["native_ttl"] = devices["awg_trigger"]
    namespace = {"__name__": "composed_native_test"}
    with _fake_artiq(clock, devices):
        exec(compile(source, "<composed-native>", "exec"), namespace)
        experiment = namespace["GeneratedExperiment"]()
        experiment.build()
        experiment._validate_target()
        experiment.point_0()
    monkeypatch.delitem(sys.modules, "native_export_fixture", raising=False)
    return clock, experiment


def test_top_level_kernel_preserves_functions_epoch_frames_and_physical_tail(
    tmp_path, monkeypatch
):
    compiled, native, standalone, source, report, _ = _fixture(tmp_path)
    assert verify_export(native, source=standalone)["offline_export_check"] == "passed"
    clock, experiment = _run(source, tmp_path, monkeypatch, work_mu=300000)
    point = report["points"][0]
    base = experiment.native_physical_base_mu
    origin = experiment.native_control_t0_mu
    assert base % point["epoch"]["grid_mu"] == point["epoch"]["offset_mu"]
    assert origin == base + point["logical_origin_mu_from_B"]
    assert len(experiment.seen) == 1
    assert experiment.seen[0][:2] == (origin + 1800, origin)
    assert experiment.seen[0][2:] == pytest.approx((0.2, math.pi / 2))
    assert experiment.native_terminal_f_q == pytest.approx(0.2 - math.pi / 2)
    assert point["terminal_frames"]["q"]["effective_phase"] == pytest.approx(
        experiment.native_terminal_f_q
    )
    assert point["physical_end_mu_from_B"] > point["logical_end_mu_from_B"]
    assert point["suffix"][0]["start_mu_from_B"] == point["logical_end_mu_from_B"]
    assert sum(kind == "reset" for kind, _ in clock.trace) == 1
    trigger = [(kind, when) for kind, when in clock.trace if kind.startswith("trigger")]
    group = group_program(compiled)
    assert trigger == [
        ("trigger.on", base + group["trigger_physical_mu"]),
        ("trigger.off", base + group["trigger_physical_mu"] + 64),
    ]
    trigger_index = next(
        i for i, entry in enumerate(clock.trace) if entry[0] == "trigger.on"
    )
    assert all(
        kind not in {"reset", "break", "wait"}
        for kind, _ in clock.trace[trigger_index:-4]
    )
    waits = [value for kind, value in clock.trace if kind == "wait"]
    assert waits[0] == base + point["minimum_drain_end_mu_from_B"]
    assert waits[-1] == waits[0] + 200
    # Native work changes the wall-clock epoch, not precomputed RF or frame.
    _, other = _run(source, tmp_path, monkeypatch, work_mu=700000)
    assert other.native_control_t0_mu > origin
    assert other.seen[0][0] - other.native_control_t0_mu == 1800
    assert other.seen[0][2:] == experiment.seen[0][2:]


def test_body_has_no_reset_break_wait_and_legacy_source_is_unchanged(tmp_path):
    _, _, standalone, source, _, _ = _fixture(tmp_path)
    tree = ast.parse(source)
    generated = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    body = next(node for node in generated.body if node.name == "_taiyi_body_0")
    assert all(
        not isinstance(node, ast.Attribute)
        or node.attr not in {"reset", "break_realtime", "wait_until_mu"}
        for node in ast.walk(body)
    )
    assert "self.core.reset()" in standalone
    assert "self.core.wait_until_mu(now_mu())" in standalone


def test_suffix_must_match_declared_duration_without_padding(tmp_path, monkeypatch):
    *_, source, report, _ = _fixture(tmp_path, suffix_argument=7, finalize=False)
    with pytest.raises(ValueError, match="duration differs"):
        _run(source, tmp_path, monkeypatch)
    assert report["points"][0]["suffix"][0]["duration_mu"] == 8


def test_changed_source_is_rejected_before_import_or_execution(tmp_path, monkeypatch):
    compiled, native, standalone, source, _, path = _fixture(tmp_path)
    path.write_text("raise AssertionError('must not import')\n", encoding="utf-8")
    with pytest.raises(ExportError, match="hash changed"):
        compose_native_source(compiled, native, standalone, _epochs(compiled, native))
    with pytest.raises(RuntimeError, match="source changed"):
        _run(source, tmp_path, monkeypatch)


def test_preload_timing_boundary_is_rejected(tmp_path):
    compiled, native, standalone, _, _, _ = _fixture(tmp_path)
    changed = standalone.replace(
        "    def point_0(self):\n        self.core.reset()",
        "    def point_0(self):\n        self.core.reset()\n"
        "        self.core.break_realtime()",
    )
    with pytest.raises(ExportError, match="preload"):
        compose_native_source(compiled, native, changed, _epochs(compiled, native))


@pytest.mark.parametrize("wrong_origin", [False, True])
def test_unverified_or_wrong_origin_cached_module_is_rejected(
    tmp_path, monkeypatch, wrong_origin
):
    *_, source, _, path = _fixture(tmp_path)
    cached = ModuleType("native_export_fixture")
    cached.__file__ = str(tmp_path / "different.py" if wrong_origin else path)
    if wrong_origin:
        cached.__taiyidsl_source_sha256__ = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    with pytest.raises(RuntimeError, match="stale native module cache"):
        _run(source, tmp_path, monkeypatch, cached=cached)


def test_native_compile_harness_rejects_connected_devices_and_core_overrides():
    helper = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/compile_native_artiq.py")
    )["merge_compile_devices"]
    base = {"core": {"arguments": {"host": None}}}
    local = {
        "type": "local",
        "module": "artiq.coredevice.ttl",
        "class": "TTLOut",
        "arguments": {"channel": 512},
    }
    assert helper(base, {"ttl": local, "alias": "ttl"})["alias"] == "ttl"
    for extra in (
        {"core": local},
        {"camera": {"type": "controller", "host": "127.0.0.1"}},
        {"alias": "missing"},
        {"a": "b", "b": "a"},
    ):
        with pytest.raises(ValueError):
            helper(base, extra)


def test_runtime_device_alias_to_trigger_is_rejected_before_kernel(
    tmp_path, monkeypatch
):
    *_, source, _, _ = _fixture(tmp_path)
    with pytest.raises(RuntimeError, match="hardware resource alias"):
        _run(source, tmp_path, monkeypatch, alias=True)


@pytest.mark.parametrize(
    "attribute", ["device_0", "native_control_t0_mu", "native_terminal_f_q", "point_0"]
)
def test_native_build_cannot_overwrite_generated_attributes(
    tmp_path, monkeypatch, attribute
):
    package = PACKAGE.replace(
        "self.seen = []", f"self.seen = []\n        self.{attribute} = 0"
    )
    *_, source, _, _ = _fixture(tmp_path, package_source=package)
    with pytest.raises(RuntimeError, match="build attribute conflicts"):
        _run(source, tmp_path, monkeypatch)


@pytest.mark.parametrize("reset", [reset_frame, reset_phase])
def test_native_receives_unified_zero_after_either_reset(tmp_path, monkeypatch, reset):
    *_, source, report, _ = _fixture(tmp_path, terminal_reset=reset)
    _, experiment = _run(source, tmp_path, monkeypatch)
    assert experiment.native_terminal_f_q == 0
    assert experiment.seen[0][2:] == (0, 0)
    assert report["points"][0]["terminal_frames"]["q"] == {
        "frame_phase": 0.0,
        "effective_phase": 0.0,
        "virtual_z": 0.0,
        "hardware_phase": 0.0,
    }
    assert report["control_attributes"]["terminal_frames"]["q"]["F"] == (
        "native_terminal_f_q"
    )


def test_native_default_sequence_reference_excludes_preparation_time(tmp_path):
    compiled, _, _, _, _, _ = _fixture(
        tmp_path, timed_gap=1000, package_source=TIMED_PACKAGE
    )
    program = group_program(compiled)
    assert program["phase_reference"]["mode"] == "sequence"
    assert program["phase_reference"]["reference_start_ticks"] == 5120
    pulse = program["cards"]["A"]["program"]["pulses"][0]
    assert pulse["phase_rad"] % (2 * math.pi) == pytest.approx(0, abs=1e-12)


def test_native_report_separates_timeline_and_carrier_reference(tmp_path):
    *_, report, _ = _fixture(tmp_path, timed_gap=1000, package_source=TIMED_PACKAGE)
    assert report["time_reference"] == "control_epoch_after_native_prefix"
    assert report["phase_reference"] == "per_triggered_sequence"
    assert report["control_attributes"]["phase_origin_is_timeline_only"] is True
    reference = report["points"][0]["carrier_references"]["g"]
    assert reference["mode"] == "sequence"
    assert reference["reference_start_ticks"] == 5120


@pytest.mark.parametrize("gap", [512, 1000])
def test_strict_native_preparation_and_gate_gap_share_control_epoch(
    tmp_path, monkeypatch, gap
):
    compiled, native, standalone, source, report, _ = _fixture(
        tmp_path,
        timed_gap=gap,
        package_source=TIMED_PACKAGE,
        finalize=False,
        phase_reference="shot",
    )
    assert verify_export(native, source=standalone)["offline_export_check"] == "passed"
    point = report["points"][0]
    timed = point["timed"][0]
    assert timed["start_mu_from_B"] == point["logical_origin_mu_from_B"]
    assert timed["end_mu_from_B"] - timed["start_mu_from_B"] == 24
    assert timed["end_boundary"] == "final preparation digital IO complete"
    for work_mu in (300000, 700000):
        clock, experiment = _run(source, tmp_path, monkeypatch, work_mu=work_mu)
        origin = experiment.native_control_t0_mu
        cooling_edges = [
            (kind, when)
            for kind, when in clock.trace
            if kind == "native.off" and origin <= when <= origin + 24
        ]
        assert cooling_edges == [("native.off", origin), ("native.off", origin + 24)]
        trigger = next(when for kind, when in clock.trace if kind == "trigger.on")
        assert trigger + 512 - (origin + 24) == gap
        assert len(experiment.seen) == 1
        assert experiment.seen[0][:2] == (origin + 24 + gap + 1800, origin)
        assert experiment.seen[0][2:] == pytest.approx((0.2, math.pi / 2))
        assert experiment.native_terminal_f_q == pytest.approx(0.2 - math.pi / 2)
        start = next(
            i
            for i, (kind, when) in enumerate(clock.trace)
            if kind == "native.off" and when == origin
        )
        assert all(
            kind not in {"reset", "break", "wait"} for kind, _ in clock.trace[start:-1]
        )
        assert clock.trace[-1][0] == "wait"
    tree = ast.parse(source)
    generated = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    body = next(node for node in generated.body if node.name == "_taiyi_body_0")
    assert ast.unparse(body.body[0]) != "at_mu(t0)"
    # Carrier phase includes the strict preparation and requested gap from T0.
    pulse = group_program(compiled)["cards"]["A"]["program"]["pulses"][0]
    expected = 2 * math.pi * ((150e6 * (24 + gap) * 1e-9) % 1)
    assert pulse["phase_rad"] % (2 * math.pi) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("gap", [0, 496])
def test_gap_cannot_hide_trigger_advance_inside_opaque_native_call(tmp_path, gap):
    with pytest.raises(ExportError, match="trigger precedes.*strict native return"):
        _fixture(tmp_path, timed_gap=gap)


def test_strict_native_duration_mismatch_stops_before_trigger(tmp_path, monkeypatch):
    *_, source, _, _ = _fixture(tmp_path, timed_gap=1000, timed_duration=32)
    emitted = []
    original_on = TTL.on

    def record_on(self):
        emitted.append(self.name)
        original_on(self)

    monkeypatch.setattr(TTL, "on", record_on)
    with pytest.raises(ValueError, match="Strict native duration differs"):
        _run(source, tmp_path, monkeypatch)
    assert "trigger" not in emitted
