"""Independent checks at the hybrid compiler/export/runtime boundaries."""

import ast
import json
from dataclasses import replace

import numpy as np
import pytest
from test_spectrum_backend import group_program, lab
from test_spectrum_runtime import fixture, install_profile, measured_profile

from taiyidsl import (
    CompileError,
    ControlBinding,
    DeviceCalibration,
    Experiment,
    HalfSinePhase,
    Qubit,
    SpectrumConfig,
    Square,
    SweepParameter,
    delay,
    ns,
    play,
    reset_frame,
    rz,
    shift_phase,
    trigplay,
    us,
)
from taiyidsl.artiq import _DriverClock, _fake_artiq, verify_export
from taiyidsl.spectrum_export import artiq_subplan, spectrum_manifest
from taiyidsl.spectrum_runtime import SpectrumRunner, SpectrumRuntimeError
from taiyidsl.spectrum_waveforms import reconstruct_window


def _set_group(profile, **changes):
    config = profile.setup.spectrum
    profile.setup.spectrum = replace(
        config, groups={"g": replace(config.groups["g"], **changes)}
    )


def _one_gate(*, duration=400 * ns, preparation=0):
    q = Qubit("q")
    exp = Experiment("contract_gate", [q])
    with exp.average(1), exp.realtime():
        if preparation:
            play("camera", Square(preparation))
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(duration, frequency=30_000, amplitude=0.4))
            rz(q, 0.25)
    return exp


def test_waveforms_and_trigger_edges_point_to_original_logical_ids():
    compiled = lab().compile(_one_gate(preparation=8 * ns))
    point = compiled.points[0]
    region = next(e for e in point.logical_events if e.kind == "spectrum_region")
    gate = next(
        e
        for e in point.logical_events
        if e.params.get("route") == "spectrum" and e.kind == "play"
    )
    assert gate.logical_ids and region.logical_ids
    stored = group_program(compiled)["cards"]["A"]["program"]["pulses"][0]
    assert tuple(stored["logical_id"]) == gate.logical_ids
    trigger_edges = [e for e in point.physical_events if e.signal == "awg_trigger"]
    assert len(trigger_edges) == 2
    assert all(e.logical_ids == region.logical_ids for e in trigger_edges)
    logical_ids = {
        identifier for event in point.logical_events for identifier in event.logical_ids
    }
    assert all(set(e.logical_ids) <= logical_ids for e in point.physical_events)


def test_native_subplan_trigger_keeps_the_region_source_id():
    compiled = lab().compile(_one_gate())
    region = next(
        e for e in compiled.points[0].logical_events if e.kind == "spectrum_region"
    )
    native = artiq_subplan(compiled)
    trigger = next(e for e in native.points[0].logical_events if e.kind == "trigger")
    assert trigger.logical_ids == region.logical_ids
    assert trigger.source == region.source


def test_generic_artiq_calibration_cannot_be_silently_applied_to_awg():
    profile = lab()
    profile.setup.calibration["rf"] = DeviceCalibration(
        latency_mu=8, phase_offset=0.2, amplitude_scale=0.5
    )
    with pytest.raises(CompileError, match="Spectrum calibration|DeviceCalibration"):
        profile.compile(_one_gate())


def test_generic_device_latency_cannot_be_silently_ignored_on_awg():
    profile = lab()
    profile.setup.devices["rf"] = replace(profile.setup.devices["rf"], latency_mu=8)
    with pytest.raises(CompileError, match="latency|calibration"):
        profile.compile(_one_gate())


@pytest.mark.parametrize(
    "channel_calibration",
    [
        {"gain": 0},
        {"gain": -1},
        {"gain": float("nan")},
        {"phase_offset_rad": float("inf")},
        {"delay_ns": float("nan")},
    ],
)
def test_invalid_channel_calibration_is_rejected(channel_calibration):
    profile = lab()
    _set_group(profile, calibration={"channels": {"A:0": channel_calibration}})
    with pytest.raises(CompileError, match="calibration"):
        profile.compile(_one_gate())


def test_binding_base_frames_modulation_and_active_updates_are_applied_once():
    profile = lab()
    profile.signal_map["q.rf"] = ControlBinding(
        "rf",
        frequency_scale=1.5,
        frequency_offset=10_000,
        phase_scale=-2,
        phase_offset=0.23,
        amplitude_scale=0.5,
    )
    _set_group(
        profile,
        calibration={
            "channels": {
                "A:0": {
                    "gain": 2.0,
                    "phase_offset_rad": 0.11,
                    "delay_ns": 0.0,
                }
            }
        },
    )
    q = Qubit("q")
    exp = Experiment("phase_composition", [q])
    with exp.average(1), exp.realtime():
        play("camera", Square(8 * ns))
        with exp.triggered_sequence(group="g"):
            shift_phase(q.frame, 0.3)
            rz(q, 0.1)
            with exp.parallel():
                trigplay(
                    q.rf,
                    Square(
                        400 * ns,
                        frequency=20_000,
                        amplitude=0.8,
                        phase=0.4,
                        phase_modulation=HalfSinePhase(0.6),
                    ),
                )
                with exp.serial():
                    delay(120 * ns)
                    shift_phase(q.frame, 0.2)
                    delay(120 * ns)
                    reset_frame(q.frame)
    program = group_program(profile.compile(exp))["cards"]["A"]["program"]
    pulse = program["pulses"][0]
    assert pulse["start_sample"] == 140
    n = np.arange(500, dtype=float)
    initial_phase = -2 * (0.4 + 0.3 - 0.1) + 0.23 + 0.11
    phase = (
        initial_phase
        + 2 * np.pi * 40_000 * n / 1.25e9
        - 2 * 0.6 * np.sin(np.pi * n / 500)
        - 0.4 * (n >= 150)
        + 0.8 * (n >= 300)
    )
    expected = np.rint(32767 * 0.2 * np.cos(phase)).astype(np.int16)
    actual = reconstruct_window(program, 140, 640)[:, 0]
    np.testing.assert_array_equal(actual, expected)


def test_unassociated_alias_is_not_modified_by_another_signal_frame():
    profile = lab()
    profile.signal_map["alias"] = "rf"
    profile.setup.spectrum = replace(
        profile.setup.spectrum,
        signal_frames={"clock": None, "rydberg": None, "alias": None},
    )
    q = Qubit("q")
    exp = Experiment("alias_frame", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        shift_phase(q.frame, 0.4)
        rz(q, 0.2)
        with exp.parallel():
            trigplay(
                "alias", Square(400 * ns, frequency=30_000, amplitude=0.4, phase=0.3)
            )
            with exp.serial():
                delay(200 * ns)
                shift_phase(q.frame, 0.3)
        trigplay(q.rf, Square(400 * ns, frequency=30_000, amplitude=0.4, phase=0.3))
    program = group_program(profile.compile(exp))["cards"]["A"]["program"]
    first, second = program["pulses"]
    assert first["phase_steps"] == []
    n = np.arange(500, dtype=float)
    expected = np.rint(
        32767 * 0.4 * np.cos(0.3 + 2 * np.pi * 30_000 * n / 1.25e9)
    ).astype(np.int16)
    np.testing.assert_array_equal(
        reconstruct_window(program, first["start_sample"], first["start_sample"] + 500)[
            :, 0
        ],
        expected,
    )
    assert second["phase_rad"] == pytest.approx(
        0.3 + 0.7 - 0.2 + 2 * np.pi * 30_000 * 400e-9
    )


def test_explicit_external_artiq_parallel_branch_preserves_native_timing(tmp_path):
    profile = lab(cooling=True)
    profile.setup.calibration["cooling"] = DeviceCalibration(
        latency_mu=4, frequency_offset=1e6, amplitude_scale=0.5
    )
    q = Qubit("q")
    exp = Experiment("parallel_artiq", [q])
    with exp.average(1), exp.realtime(), exp.parallel():
        play("cooling", Square(20 * us, frequency=80e6, amplitude=0.4))
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(400 * ns, frequency=30_000, amplitude=0.4))
    compiled = profile.compile(exp)
    native = artiq_subplan(compiled)
    native_point = native.points[0]
    output = next(e for e in native_point.physical_events if e.kind == "dds_on")
    config = next(e for e in native_point.physical_events if e.kind == "dds_config")
    assert output.start_mu - native_point.origin_mu == -4
    assert config.params["frequency"] == 81e6
    assert config.params["amplitude"] == 0.2
    assert native.setup.devices["cooling"].config_duration_mu == 3000
    result = compiled.export(tmp_path)
    assert result["verification"]["offline"] == "passed"
    assert verify_export(native)["offline_export_check"] == "passed"


@pytest.mark.parametrize("zero_position", ["first", "last"])
def test_zero_duration_gate_is_logical_only_with_other_nonzero_gates(zero_position):
    q = Qubit("q")
    exp = Experiment("zero_gate", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        if zero_position == "first":
            trigplay(q.rf, Square(0 * ns, frequency=30_000))
        trigplay(q.rf, Square(400 * ns, frequency=30_000))
        if zero_position == "last":
            trigplay(q.rf, Square(0 * ns, frequency=30_000))
    compiled = lab().compile(exp)
    logical = [e for e in compiled.points[0].logical_events if e.kind == "play"]
    assert len(logical) == 2
    program = group_program(compiled)["cards"]["A"]["program"]
    assert len(program["pulses"]) == 1
    assert program["pulses"][0]["duration_samples"] == 500


def test_zero_tail_does_not_move_logical_end_or_artiq_readout():
    q = Qubit("q")
    exp = Experiment("tail_and_readout", [q])
    with exp.average(1), exp.realtime():
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(400 * ns, frequency=30_000))
        play("camera", Square(8 * ns))
    compiled = lab().compile(exp)
    point = compiled.points[0]
    region = next(e for e in point.logical_events if e.kind == "spectrum_region")
    readout = next(e for e in point.logical_events if e.signal == "camera")
    assert region.end_mu == readout.start_mu == 2000
    program = group_program(compiled)["cards"]["A"]["program"]
    end = program["pulses"][0]["start_sample"] + 500
    assert program["physical_duration_samples"] > end
    assert not np.any(
        reconstruct_window(program, end, program["physical_duration_samples"])
    )
    native_point = artiq_subplan(compiled).points[0]
    readout_on = next(
        e
        for e in native_point.physical_events
        if e.kind == "ttl_on" and e.signal == "readout"
    )
    assert readout_on.start_mu - native_point.origin_mu == 400


@pytest.mark.parametrize("native_tick", [1e-9, 2e-9])
def test_exported_epoch_stays_on_native_grid_and_trigger_phase_class(
    tmp_path, native_tick
):
    profile = lab()
    config = profile.setup.spectrum
    profile.setup.tick_seconds = native_tick
    profile.setup.spectrum = SpectrumConfig(
        {"g": replace(config.groups["g"], epoch_offset_mu=8)},
        config.signal_frames,
        artiq_tick_seconds=native_tick,
    )
    compiled = profile.compile(_one_gate())
    result = compiled.export(tmp_path)
    manifest = json.loads(result["manifest"].read_text())
    group = manifest["points"][0]["groups"][0]
    source = (tmp_path / "artiq" / "experiment.py").read_text()
    tree = ast.parse(source)
    point = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "point_0"
    )
    origin = next(
        n
        for n in point.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "t0" for t in n.targets)
    )
    expression = compile(ast.Expression(origin.value), "<generated-epoch>", "eval")
    for now in (0, 1, 7, 8, 31, 32, 1001):
        start = eval(expression, {"__builtins__": {}, "now_mu": lambda: now})
        assert start >= now
        assert start % 8 == 0
        assert (start + group["trigger_physical_mu"]) % 32 == 8
    assert group["trigger_time_unit_s"] == native_tick


def test_frame_only_scope_archives_terminal_frame_without_touching_cards():
    q = Qubit("q")
    exp = Experiment("empty_hardware", [q])
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="g"):
        rz(q, 0.7)
    compiled = lab().compile(exp)
    assert compiled.virtual_z_frames() == {q.uid: 0.7}
    region = next(
        e for e in compiled.points[0].logical_events if e.kind == "spectrum_region"
    )
    assert region.params["terminal_frames"][q.uid]["virtual_z"] == 0.7
    manifest = spectrum_manifest(compiled)
    assert manifest["points"][0]["groups"] == []

    class UntouchableAdapter:
        def __getattr__(self, name):
            raise AssertionError(f"Unused card was touched: {name}")

    calls = []
    runner = SpectrumRunner({"A": UntouchableAdapter(), "B": UntouchableAdapter()})
    receipt = runner.run_point(manifest, 0, lambda point: calls.append(point))
    assert calls == [0]
    assert receipt["cards"] == {}


def test_changed_trigger_latency_invalidates_measured_calibration_before_setup():
    log, adapters, manifest = fixture()
    install_profile(manifest, measured_profile(adapters))
    manifest["points"][0]["groups"][0]["cards"]["a"]["contract"][
        "trigger_latency_samples"
    ] = 1
    log.clear()
    with pytest.raises(SpectrumRuntimeError, match="hardware fingerprint"):
        SpectrumRunner(adapters).run_point(manifest, 0, lambda _: None)
    assert all(operation == "probe" for _, operation in log)


def _exported_bridge(compiled, directory):
    """Load the real emitted host bridge with no ARTIQ SDK or driver imports."""
    result = compiled.export(directory)
    source = result["source"].read_text(encoding="utf-8")
    devices = {name: object() for name in artiq_subplan(compiled).setup.devices}
    devices["core"] = object()
    namespace = {"__file__": str(result["source"]), "__name__": "bridge_contract"}
    with _fake_artiq(_DriverClock(), devices):
        exec(compile(source, str(result["source"]), "exec"), namespace)
        instance = namespace["GeneratedExperiment"]()
        instance.build()
    return instance


class _LifecycleCard:
    is_hardware = False

    def __init__(self, name, contract, log):
        self.name, self.contract, self.log = name, contract, log
        self.armed = False
        self.fail_arm = False
        self.fail_complete = False
        self.clock_session_id = f"test-session-{name}"

    def _record(self, operation):
        self.log.append((self.name, operation))

    def probe(self):
        self._record("probe")
        return {
            **self.contract,
            "firmware": "test-firmware",
            "sequence_available": True,
            "max_segments": 65536,
            "max_steps": 4096,
            "clock_mode": "external_reference",
        }

    def configure(self, contract):
        assert not self.armed
        assert contract == self.contract
        self._record("configure")

    def upload(self, program, loader):
        self._record("upload")
        for segment in program["segments"]:
            assert loader(program, segment["id"]).shape == (
                segment["sample_count"],
                len(self.contract["active_channels"]),
            )

    def arm(self):
        self._record("arm")
        if self.fail_arm:
            raise SpectrumRuntimeError("injected arm failure")
        self.armed = True

    def select_entry(self, step):
        assert not self.armed and step >= 0
        self._record("select")

    def ready(self):
        self._record("ready")
        return self.armed

    def wait_complete(self, timeout_s):
        assert self.armed and timeout_s > 0
        self._record("complete")
        if self.fail_complete:
            raise SpectrumRuntimeError("injected completion failure")
        self.armed = False

    def stop(self):
        self._record("stop")
        self.armed = False


def _bridge_cards(instance, log):
    group = next(
        point["groups"][0]
        for point in instance.spectrum_manifest["points"]
        if point["groups"]
    )
    return {
        name: _LifecycleCard(name, card["contract"], log)
        for name, card in group["cards"].items()
    }


def _bridge_scan(mode):
    q = Qubit("q")
    exp = Experiment("bridge_scan", [q])
    duration = SweepParameter("duration", (0, 400), "ns")
    phase = SweepParameter("phase", (0, 0.5), "rad")
    with (
        exp.average(2, mode=mode),
        exp.sweep(duration),
        exp.sweep(phase),
        exp.realtime(),
    ):
        with exp.triggered_sequence(group="g"):
            trigplay(q.rf, Square(duration, frequency=30_000, phase=phase))
        play("camera", Square(8 * ns))
    return lab().compile(exp)


@pytest.mark.parametrize("mode", ["cyclic", "sequential"])
def test_exported_bridge_preserves_scan_average_order_and_each_ready_barrier(
    tmp_path, mode
):
    compiled = _bridge_scan(mode)
    instance = _exported_bridge(compiled, tmp_path)
    assert instance.spectrum_runner is None
    log = []
    cards = _bridge_cards(instance, log)
    instance.spectrum_runner = SpectrumRunner(cards, allow_unverified=True)
    instance._validate_target = lambda: None
    instance.initialize_devices = lambda: log.append(("native", "initialize"))
    pending = []

    class Results:
        def before_shot(self, point, shot):
            assert not pending
            assert not any(card.armed for card in cards.values())
            pending.append((point, shot))
            log.append(("before", point, shot))

        def after_shot(self, point, shot):
            assert pending.pop() == (point, shot)
            assert not any(card.armed for card in cards.values())
            log.append(("after", point, shot))

        def finish(self):
            assert not pending
            assert not any(card.armed for card in cards.values())
            log.append(("finish",))

    def kernel(point):
        assert pending and pending[0][0] == point
        active = bool(instance.spectrum_manifest["points"][point]["groups"])
        assert all(card.armed == active for card in cards.values())
        log.append(("kernel", point))

    instance.result_adapter = Results()
    instance._execute_point = kernel
    instance.run()
    shots = (
        [(point, shot) for shot in range(2) for point in range(4)]
        if mode == "cyclic"
        else [(point, shot) for point in range(4) for shot in range(2)]
    )
    expected = [("native", "initialize")]
    resident = None
    uploads = 0
    for point, shot in shots:
        active = point >= 2
        expected.append(("before", point, shot))
        if active:
            if resident != point:
                uploads += 1
                expected.extend((name, "probe") for name in cards)
                if resident is not None:
                    expected.extend((name, "stop") for name in reversed(cards))
                for name in cards:
                    expected.extend([(name, "configure"), (name, "upload")])
                resident = point
            expected.extend((name, "probe") for name in cards)
            expected.extend((name, "select") for name in cards)
            expected.extend((name, "arm") for name in cards)
            expected.extend((name, "ready") for name in cards)
        expected.append(("kernel", point))
        if active:
            expected.extend((name, "complete") for name in cards)
        expected.append(("after", point, shot))
    expected.append(("finish",))
    assert log == expected
    assert [
        (item["point"], item["shot"]) for item in instance.spectrum_receipts
    ] == shots
    assert (
        sum(item["receipt"]["uploaded"] for item in instance.spectrum_receipts)
        == uploads
    )
    assert uploads == (4 if mode == "cyclic" else 2)
    assert [item["execution_index"] for item in instance.spectrum_receipts] == list(
        range(len(shots))
    )
    assert all(
        item["receipt"]["status"] == "complete" for item in instance.spectrum_receipts
    )
    assert [point.coordinates for point in compiled.points] == [
        {"duration": duration, "phase": phase}
        for duration in (0, 4e-7)
        for phase in (0, 0.5)
    ]


def test_exported_bridge_stops_before_kernel_and_results_when_card_arm_fails(tmp_path):
    instance = _exported_bridge(lab().compile(_one_gate()), tmp_path)
    log = []
    cards = _bridge_cards(instance, log)
    cards["B"].fail_arm = True
    instance.spectrum_runner = SpectrumRunner(cards, allow_unverified=True)
    instance._validate_target = lambda: None
    instance.initialize_devices = lambda: None

    class Results:
        def before_shot(self, point, shot):
            assert not log
            log.append(("before", point, shot))

        def __getattr__(self, name):
            raise AssertionError(f"Results callback ran after card arm failure: {name}")

    instance.result_adapter = Results()
    instance._execute_point = lambda _: pytest.fail("Kernel ran before all cards ready")
    with pytest.raises(SpectrumRuntimeError, match="injected arm failure"):
        instance.run()
    assert log[-2:] == [("B", "stop"), ("A", "stop")]
    assert not instance.spectrum_receipts
    assert not any(card.armed for card in cards.values())


@pytest.mark.parametrize("failure", ["before", "kernel", "complete", "after"])
def test_exported_bridge_failed_shot_has_no_completed_receipt(tmp_path, failure):
    instance = _exported_bridge(lab().compile(_one_gate()), tmp_path)
    log = []
    cards = _bridge_cards(instance, log)
    cards["B"].fail_complete = failure == "complete"
    instance.spectrum_runner = SpectrumRunner(cards, allow_unverified=True)

    def record(stage, *indices):
        log.append((stage, *indices))
        if stage == failure:
            raise RuntimeError(f"injected {stage} failure")

    class Results:
        def before_shot(self, point, shot):
            record("before", point, shot)

        def after_shot(self, point, shot):
            assert not any(card.armed for card in cards.values())
            record("after", point, shot)

    instance.result_adapter = Results()
    instance._execute_point = lambda point: record("kernel", point)
    with pytest.raises(RuntimeError, match="injected .* failure"):
        instance._run_shot(0, 0)

    assert not instance.spectrum_receipts
    assert not any(card.armed for card in cards.values())
    if failure == "before":
        assert log == [("before", 0, 0)]
    else:
        assert log.count(("kernel", 0)) == 1
        if failure in {"kernel", "complete"}:
            assert ("after", 0, 0) not in log
            assert log[-2:] == [("B", "stop"), ("A", "stop")]
        else:
            assert log[-3:] == [("A", "complete"), ("B", "complete"), ("after", 0, 0)]
