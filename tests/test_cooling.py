"""Observable cooling sequence semantics, not an atomic dynamics simulation."""

import json
import runpy
from pathlib import Path

import pytest

from taiyidsl import (
    CompileError,
    Experiment,
    MHz,
    Protocol,
    ProtocolStep,
    Quantity,
    Qubit,
    SweepParameter,
    us,
)
from taiyidsl.backends.artiq.verification import verify_equivalence
from taiyidsl.config.optics import OpticsConfig
from taiyidsl.experiments.cooling import (
    illuminated_readout,
    optical_pump,
    sideband_cool,
    trigger_readout,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = runpy.run_path(str(ROOT / "examples/raman_sideband_cooling.py"))


@pytest.fixture
def config():
    return OpticsConfig.load(ROOT / "examples/config/optics/rsc.json")


def compile_scan(config, rounds=(0, 1, 3)):
    q = Qubit("q0")
    return EXAMPLE["reference_rsc_lab"](q).compile(
        EXAMPLE["build_cooling_scan"](q, config, rounds=rounds, shots=2)
    )


def pulses(point, signal):
    return [e for e in point.logical_events if e.kind == "play" and e.signal == signal]


def test_json_roundtrip_and_deep_immutability(config):
    assert OpticsConfig.from_dict(json.loads(json.dumps(config.to_dict()))) == config
    data = config.to_dict()
    data["beams"]["common"]["amplitude"] = 0.9
    assert config["beams"]["common"]["amplitude"] == 0.15
    with pytest.raises(TypeError):
        config["beams"]["common"]["amplitude"] = 0.8


@pytest.mark.parametrize(
    "field,value",
    [
        ("amplitude", 1.1),
        ("amplitude", float("nan")),
        ("frequency_hz", -1),
        ("frequency_hz", True),
    ],
)
def test_reject_bad_beam_parameters(config, field, value):
    with pytest.raises((ValueError, TypeError)):
        data = config.to_dict()
        data["pump"][field] = value
        OpticsConfig.from_dict(data)


def test_schema_and_unknown_keys_are_not_silently_accepted(config):
    data = config.to_dict()
    data["schema_version"] = 999
    with pytest.raises(ValueError):
        OpticsConfig.from_dict(data)
    data = config.to_dict()
    data["raman"]["x"]["typo"] = 1
    with pytest.raises(ValueError, match="raman.x"):
        OpticsConfig.from_dict(data)


def test_three_axes_reset_and_synchronous_beams(config):
    compiled = compile_scan(config)
    for point in compiled.points:
        rounds = int(point.coordinates["rounds"])
        expected_pairs = rounds * 3 * 2
        a = pulses(point, "optics.raman_common")
        pump = pulses(point, "optics.pump_556")
        assert len(a) == expected_pairs
        assert len(pump) == expected_pairs + 1
        for axis in "xyz":
            b = pulses(point, f"optics.raman_{axis}")
            assert len(b) == rounds * 2
            for pulse in b:
                assert any(
                    (x.start_mu, x.end_mu) == (pulse.start_mu, pulse.end_mu) for x in a
                )
        # Initial spin pumping precedes all Raman pulses, followed by one reset
        # per pulse; none of the scattering pulses overlaps a Raman pair.
        for index, pulse in enumerate(a):
            assert pump[index].end_mu < pulse.start_mu
            assert pulse.end_mu < pump[index + 1].start_mu


def test_reference_and_pushout_have_matched_imaging_windows(config):
    compiled = compile_scan(config)
    for reference, selected in zip(compiled.points[::2], compiled.points[1::2]):
        assert not pulses(reference, "optics.pushout_556")
        assert len(pulses(selected, "optics.pushout_556")) == 1
        assert reference.duration_mu == selected.duration_mu
        for point in (reference, selected):
            images = pulses(point, "optics.imaging_399")
            cooling = pulses(point, "optics.imaging_cooling_556")
            trigger = next(e for e in point.logical_events if e.kind == "measure")
            assert len(images) == len(cooling) == 1
            assert images[0].start_mu == cooling[0].start_mu == trigger.start_mu
            assert images[0].end_mu == cooling[0].end_mu
        assert pulses(reference, "optics.imaging_399")[0].start_mu == (
            pulses(selected, "optics.imaging_399")[0].start_mu
        )


def test_experiment_freezes_parameters_and_keeps_operation_ir(config):
    q = Qubit("q0")
    data = config.to_dict()
    exp = EXAMPLE["build_cooling_scan"](q, data, rounds=(1,), shots=1)
    data["pump"]["amplitude"] = 0.9
    compiled = EXAMPLE["reference_rsc_lab"](q).compile(exp)
    assert compiled.parameters["pump"]["amplitude"] == config["pump"]["amplitude"]
    serialized = json.dumps(compiled.ir)
    assert '"sideband_cool"' in serialized
    assert '"repeat"' in serialized
    assert '"effective_parameters"' in serialized
    assert "recipe_snapshot" not in serialized


def test_spectroscopy_preserves_probe_signal_and_rf_sweep(config):
    q = Qubit("q0")
    compiled = EXAMPLE["reference_rsc_lab"](q).compile(
        EXAMPLE["build_thermometry_scan"](q, config, shots=2)
    )
    for point in compiled.points:
        probe = pulses(point, "optics.raman_x")[-1]
        assert probe.params["frequency"] == point.coordinates["probe_rf"]
        assert all(p.end_mu < probe.start_mu for p in pulses(point, "optics.pump_556"))
    assert verify_equivalence(compiled)["status"] == "passed"


def test_cooling_export_matches_unrolled_for_every_point(config):
    report = verify_equivalence(compile_scan(config))
    assert report["status"] == "passed"
    assert len(report["points"]) == 6
    assert report["host_order"]["shots_checked"] == 12


def test_long_camera_trigger_cannot_escape_exposure(config):
    q = Qubit("q0")
    lab = EXAMPLE["reference_rsc_lab"](q)
    lab.setup.protocols["image"] = Protocol((ProtocolStep(q.readout, 2000 * us),))
    with pytest.raises(CompileError):
        lab.compile(EXAMPLE["build_cooling_scan"](q, config, rounds=(0,)))


def test_cooling_parameter_changes_do_not_require_rf_off_gap(config):
    changed = config.to_dict()
    changed["pump"].update(
        signal=changed["beams"]["common"]["signal"], setup_wait_s=0, settle_s=0
    )
    changed["raman"]["x"]["setup_wait_s"] = 0
    q = Qubit("q0")
    exp = Experiment("no_window", [q])
    with exp.average(shots=1):
        with exp.realtime():
            sideband_cool(exp, changed, axes=("x",), cycles_per_axis=2, rounds=1)
    # Buffered preparation fits inside the existing pulses, with no added delay.
    compiled = EXAMPLE["reference_rsc_lab"](q).compile(exp)
    assert any(e.kind == "dds_prepare" for e in compiled.points[0].physical_events)
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize(
    "rounds,error",
    [
        ((True,), TypeError),
        ((-1,), ValueError),
        ((1, 1), ValueError),
        ((1.5,), ValueError),
    ],
)
def test_invalid_round_scan_is_rejected(config, rounds, error):
    # The shared numeric parser rejects bool as a type error before repeat binding.
    with pytest.raises(error):
        EXAMPLE["build_cooling_scan"](Qubit("q0"), config, rounds=rounds)


def test_one_call_overrides_do_not_modify_defaults_or_later_calls(config):
    q = Qubit("q0")
    before = config.to_dict()
    exp = Experiment("overrides", [q], parameters=config)
    with exp.average(shots=1):
        with exp.realtime():
            optical_pump(exp, exp.parameters["pump"], duration=50 * us, amplitude=0)
            optical_pump(exp, exp.parameters["pump"])
    compiled = EXAMPLE["reference_rsc_lab"](q).compile(exp)
    pump = pulses(compiled.points[0], "optics.pump_556")
    assert pump[0].duration_mu == 50_000
    assert pump[0].params["amplitude"] == 0
    assert pump[1].duration_mu == 20_000
    assert pump[1].params["amplitude"] == before["pump"]["amplitude"]
    assert config.to_dict() == before
    assert compiled.parameters == before
    ir = json.dumps(compiled.ir)
    assert '"effective_parameters"' in ir


def test_override_sweep_and_frequency_quantity_are_bound_per_point(config):
    q = Qubit("q0")
    exp = Experiment("override_sweep", [q], parameters=config)
    duration = SweepParameter("time", [0, 10, 50], unit="us")
    with exp.average(shots=1):
        with exp.sweep(duration):
            with exp.realtime():
                optical_pump(
                    exp, exp.parameters["pump"], duration=duration, frequency=81 * MHz
                )
    compiled = EXAMPLE["reference_rsc_lab"](q).compile(exp)
    for point in compiled.points:
        pump = pulses(point, "optics.pump_556")
        assert len(pump) == 1
        assert pump[0].duration_mu == int(point.coordinates["time"] * 1e9)
        assert pump[0].params["frequency"] == 81e6
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"amplitude": 1.1},
        {"amplitude": True},
        {"duration": -1},
        {"duration": 1 * MHz},
        {"frequency": 1 * us},
        {"phase": float("nan")},
        {"setup_wait": -1},
        {"settle": -1},
        {"duration": SweepParameter("bad", [-1, 1], unit="us")},
    ],
)
def test_invalid_explicit_override_fails_during_build(config, overrides):
    exp = Experiment("invalid", [Qubit("q0")])
    with exp.average(shots=1):
        with exp.realtime():
            with pytest.raises((TypeError, ValueError)):
                optical_pump(exp, config["pump"], **overrides)


def test_unknown_override_is_not_silently_ignored(config):
    exp = Experiment("invalid", [Qubit("q0")])
    with exp.average(shots=1):
        with exp.realtime():
            with pytest.raises(TypeError):
                optical_pump(exp, config["pump"], amplitdue=0.2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"phase": Quantity(float("nan"), "phase")},
        {"phase": Quantity(float("inf"), "phase")},
        {"frequency": Quantity(float("inf"), "frequency")},
        {"duration": Quantity(float("nan"), "time")},
        {"duration": Quantity([0.000005], "time")},
        {"amplitude": Quantity({"value": 0.1}, "dimensionless")},
        {
            "phase": SweepParameter(
                "bad_phase", [Quantity(float("nan"), "phase")], unit="rad"
            )
        },
    ],
)
def test_malformed_direct_quantity_overrides_fail_during_build(config, overrides):
    exp = Experiment("invalid_quantity", [Qubit("q0")])
    with exp.average(shots=1):
        with exp.realtime():
            with pytest.raises((TypeError, ValueError)):
                optical_pump(exp, config["pump"], **overrides)


@pytest.mark.parametrize(
    "options",
    [
        {"rounds": True},
        {"rounds": -1},
        {"cycles_per_axis": 0},
        {"cycles_per_axis": True},
        {"rounds": 0, "cycles_per_axis": -1},
        {"axes": ()},
        {"axes": ("missing",)},
    ],
)
def test_invalid_cooling_execution_options_fail(config, options):
    exp = Experiment("invalid", [Qubit("q0")])
    with exp.average(shots=1):
        with exp.realtime():
            with pytest.raises((TypeError, ValueError, KeyError)):
                sideband_cool(exp, config, **options)


def test_axis_order_and_cycle_counts_are_execution_arguments(config):
    q = Qubit("q0")
    exp = Experiment("reordered", [q], parameters=config)
    with exp.average(shots=1):
        with exp.realtime():
            sideband_cool(
                exp, exp.parameters, axes=("z", "x"), cycles_per_axis=1, rounds=1
            )
    point = EXAMPLE["reference_rsc_lab"](q).compile(exp).points[0]
    z = pulses(point, "optics.raman_z")
    x = pulses(point, "optics.raman_x")
    assert len(z) == len(x) == 1
    assert z[0].end_mu < x[0].start_mu
    assert not pulses(point, "optics.raman_y")
    assert len(pulses(point, "optics.pump_556")) == 3


def test_explicit_readout_functions_preserve_distinct_zero_delay_behavior(config):
    data = config.to_dict()
    data["pushout"].update(setup_wait_s=0, settle_s=0)
    q = Qubit("q0")
    for readout, count in ((trigger_readout, 0), (illuminated_readout, 2)):
        exp = Experiment("readout", [q])
        with exp.average(shots=1):
            with exp.realtime():
                readout(exp, q, data)

        def nodes(node):
            yield node
            for child in node.children:
                yield from nodes(child)

        assert (
            sum(
                node.kind == "delay" and node.params["duration"] == 0
                for node in nodes(exp.root)
            )
            == count
        )
