"""Lightweight optical tables and hardware-independent parameter snapshots."""

import json
from fractions import Fraction

import pytest

from taiyidsl import Experiment, OpticsConfig, Quantity, Qubit, delay, reference_lab, us


def data():
    return {
        "schema_version": 2,
        "revision": "v1",
        "pump": {
            "signal": "optics.pump_556",
            "frequency_hz": 80e6,
            "amplitude": 0.1,
            "duration_s": 20e-6,
            "setup_wait_s": 20e-6,
            "settle_s": 10e-6,
        },
    }


def test_standalone_optical_table_roundtrips_without_cooling_or_readout(tmp_path):
    config = OpticsConfig.from_dict(data())
    path = tmp_path / "optics.json"
    path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    assert OpticsConfig.load(path) == config
    assert set(config) == {"schema_version", "revision", "pump"}
    assert config["pump"]["phase_rad"] == 0


def test_input_and_serialized_copies_cannot_change_snapshot():
    raw = data()
    optics = OpticsConfig.from_dict(raw)
    exp = Experiment("snapshot", [Qubit("q0")], parameters=optics)
    raw["pump"]["amplitude"] = 0.7
    exported = optics.to_dict()
    exported["pump"]["amplitude"] = 0.8
    assert exp.parameters["pump"]["amplitude"] == 0.1
    assert exp.parameters is not optics
    assert isinstance(exp.parameters, OpticsConfig)
    assert exp.parameters["pump"] is not optics["pump"]
    with pytest.raises(TypeError):
        exp.parameters["pump"]["amplitude"] = 0.9
    with pytest.raises(AttributeError):
        exp.parameters = optics


def test_new_experiment_uses_new_parameters_old_experiment_is_unchanged():
    raw = data()
    before = Experiment("before", [], parameters=raw)
    raw["pump"]["amplitude"] = 0.2
    after = Experiment("after", [], parameters=raw)
    assert before.parameters["pump"]["amplitude"] == 0.1
    assert after.parameters["pump"]["amplitude"] == 0.2


def test_generic_mapping_has_no_optical_schema_dependency():
    raw = {"custom": {"duration": 10 * us, "labels": ["one", "two"]}}
    q = Qubit("q0")
    exp = Experiment("generic", [q], parameters=raw)
    raw["custom"]["labels"].append("three")
    with exp.average(shots=1):
        with exp.realtime():
            delay(q.raman, exp.parameters["custom"]["duration"])
    compiled = reference_lab([q]).compile(exp)
    assert exp.parameters["custom"]["labels"] == ("one", "two")
    assert compiled.parameters["custom"]["duration"]["dimension"] == "time"
    json.dumps(compiled.to_dict(), allow_nan=False)
    compiled.parameters["custom"]["labels"].append("tampered")
    with pytest.raises(ValueError, match="modified"):
        compiled.assert_integrity()


@pytest.mark.parametrize("version", [True, 0, 1, 3, "2", None])
def test_optics_rejects_old_or_invalid_schema(version):
    raw = data()
    raw["schema_version"] = version
    with pytest.raises(ValueError):
        OpticsConfig.from_dict(raw)


@pytest.mark.parametrize("field", ["rounds", "stages", "readout_mode", "recipes"])
def test_execution_policy_does_not_belong_in_optics_table(field):
    raw = data()
    raw[field] = 2
    with pytest.raises(ValueError, match=field):
        OpticsConfig.from_dict(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("frequency_hz", -1),
        ("amplitude", 1.1),
        ("amplitude", True),
        ("duration_s", -1),
        ("setup_wait_s", float("nan")),
        ("phase_rad", float("inf")),
    ],
)
def test_optical_values_are_checked_with_parameter_paths(field, value):
    raw = data()
    raw["pump"][field] = value
    with pytest.raises((ValueError, TypeError), match=f"pump.{field}"):
        OpticsConfig.from_dict(raw)


def test_unknown_or_missing_pulse_fields_are_rejected():
    raw = data()
    raw["pump"]["ampitude"] = 0.1
    with pytest.raises(ValueError, match="ampitude"):
        OpticsConfig.from_dict(raw)
    del raw["pump"]["ampitude"]
    del raw["pump"]["signal"]
    with pytest.raises(ValueError, match="signal"):
        OpticsConfig.from_dict(raw)


def test_raman_references_validate_without_copying_common_beam():
    beam = {
        k: v
        for k, v in data()["pump"].items()
        if k in {"signal", "frequency_hz", "amplitude"}
    }
    raw = {
        "schema_version": 2,
        "beams": {"common": beam},
        "raman": {
            "x": {
                "a": "common",
                "b": "missing",
                "duration_s": 20e-6,
                "setup_wait_s": 10e-6,
            }
        },
    }
    with pytest.raises(ValueError, match="missing"):
        OpticsConfig.from_dict(raw)
    raw["beams"]["missing"] = {**beam, "signal": "optics.raman_x"}
    config = OpticsConfig.from_dict(raw)
    assert config["raman"]["x"]["a"] == "common"
    assert isinstance(config.to_dict()["raman"]["x"]["a"], str)


@pytest.mark.parametrize("value", [float("nan"), object(), {1: "bad key"}])
def test_generic_parameters_reject_unstable_or_unserializable_values(value):
    with pytest.raises((ValueError, TypeError)):
        Experiment("bad", [], parameters={"custom": value})


def test_same_signal_can_have_separate_working_points():
    raw = data()
    beam = {k: raw["pump"][k] for k in ("signal", "frequency_hz", "amplitude")}
    raw["beams"] = {"spin_pump": beam, "cooling_reset": {**beam, "amplitude": 0.2}}
    config = OpticsConfig.from_dict(raw)
    assert (
        config["beams"]["spin_pump"]["signal"]
        == config["beams"]["cooling_reset"]["signal"]
    )
    assert config["beams"]["spin_pump"]["amplitude"] == 0.1
    assert config["beams"]["cooling_reset"]["amplitude"] == 0.2


@pytest.mark.parametrize("value", [5 * us, Fraction(1, 5)])
def test_optics_extensions_must_roundtrip_as_json(value):
    with pytest.raises(TypeError, match="optics.extensions.custom"):
        OpticsConfig.from_dict({"schema_version": 2, "extensions": {"custom": value}})


@pytest.mark.parametrize("value", [[1], float("nan"), float("inf")])
def test_generic_snapshot_rejects_malformed_quantity(value):
    with pytest.raises((TypeError, ValueError)):
        Experiment("bad", [], parameters={"duration": Quantity(value, "time")})


def test_generic_snapshot_copies_and_normalizes_quantity():
    raw = Quantity(0.1, "time")
    exp = Experiment("quantity", [], parameters={"duration": raw})
    assert exp.parameters["duration"] is not raw
    assert exp.parameters["duration"].value == Fraction(1, 10)
