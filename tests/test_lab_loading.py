"""Versioned lab loading preserves wiring and rejects ambiguous profiles."""

import json
from pathlib import Path

import pytest

from taiyidsl import ControlBinding, ControlRecipe, Qubit
from taiyidsl.hardware import Protocol, ProtocolStep, TriggerSequence, TriggerStep
from taiyidsl.lab import LabConfig, initialization_lab, reference_lab

PROFILES = Path(__file__).resolve().parents[1] / "examples/config/labs"


def profile(name="initialization"):
    return json.loads((PROFILES / f"{name}.json").read_text(encoding="utf-8"))


def test_initialization_profile_matches_legacy_topology_and_trigger():
    loaded = LabConfig.load(PROFILES / "initialization.json", qubit_id="q-test")
    legacy = initialization_lab(Qubit("q-test"))
    assert loaded.to_dict() == legacy.to_dict()
    assert loaded.setup.protocols["image"].steps[0].signal == "{qubit}.readout"
    assert loaded.setup.protocols["image"].steps[0].duration == 10e-6


def test_rsc_profile_keeps_existing_distinct_wiring():
    loaded = LabConfig.load(PROFILES / "rsc.json", qubit_id="q0")
    signals = (
        "optics.pump_556",
        "optics.raman_common",
        "optics.raman_x",
        "optics.raman_y",
        "optics.raman_z",
        "optics.pushout_556",
        "optics.imaging_399",
        "optics.imaging_cooling_556",
    )
    for index, signal in enumerate(signals):
        device = f"urukul{index // 4}_ch{index % 4}"
        assert loaded.signal_map.bindings(signal) == (ControlBinding(device),)
        assert loaded.setup.devices[device].bus == f"spi{index // 4}"
        assert loaded.setup.devices[device].update_group == f"urukul{index // 4}"
    assert loaded.signal_map.bindings("q0.readout") == (ControlBinding("ttl0"),)
    assert loaded.setup.name == "rsc-offline-only"
    assert len(loaded.setup.devices) == 9
    assert loaded.setup.protocols["image"].steps[0].duration == 10e-6


def test_reference_lab_roundtrip_includes_optional_duration_and_multiple_outputs():
    lab = reference_lab([Qubit("q0"), Qubit("q1")])
    lab.signal_map["joint"] = ControlRecipe(
        (
            ControlBinding("urukul0_ch0", frequency_scale=2, phase_offset=0.5),
            ControlBinding("urukul1_ch0", amplitude_scale=0.8),
        )
    )
    copied = LabConfig.from_dict(json.loads(json.dumps(lab.to_dict())))
    assert copied.to_dict() == lab.to_dict()
    assert copied.setup.protocols["state_readout"].steps[0].duration is None
    assert copied.signal_map.bindings("joint") == lab.signal_map.bindings("joint")


def test_input_is_not_retained_as_mutable_configuration():
    data = profile()
    loaded = LabConfig.from_dict(data, qubit_id="q0")
    data["signals"]["optics.pump_556"][0]["frequency_scale"] = 10
    data["setup"]["devices"]["urukul0_ch0"]["grid_mu"] = 999
    assert loaded.signal_map.bindings("optics.pump_556")[0].frequency_scale == 1
    assert loaded.setup.devices["urukul0_ch0"].grid_mu == 4


def test_explicit_qubit_binding_and_legacy_aliases():
    with pytest.raises(ValueError, match="qubit_id"):
        LabConfig.from_dict(profile())
    data = profile()
    data["signals"]["q0.readout"] = [{"device": "ttl0"}]
    with pytest.raises(ValueError, match="Duplicate resolved signal"):
        LabConfig.from_dict(data, qubit_id="q0")
    assert Protocol is TriggerSequence
    assert ProtocolStep is TriggerStep


@pytest.mark.parametrize(
    "location,key",
    [
        ((), "typo"),
        (("setup",), "unknown"),
        (("setup", "devices", "urukul0_ch0"), "frequency_typo"),
        (("setup", "trigger_sequences", "image"), "callback"),
    ],
)
def test_unknown_fields_fail(location, key):
    data = profile()
    target = data
    for part in location:
        target = target[part]
    target[key] = "invalid"
    with pytest.raises(ValueError, match="unknown"):
        LabConfig.from_dict(data, qubit_id="q0")


@pytest.mark.parametrize("key", ["schema_version", "setup", "backend", "signals"])
def test_missing_top_level_fields_fail(key):
    data = profile()
    del data[key]
    with pytest.raises(ValueError, match="missing"):
        LabConfig.from_dict(data, qubit_id="q0")


def test_unknown_devices_and_unmapped_triggers_fail():
    data = profile()
    data["signals"]["optics.pump_556"][0]["device"] = "not_present"
    with pytest.raises(ValueError, match="Unknown mapped device"):
        LabConfig.from_dict(data, qubit_id="q0")
    data = profile()
    data["setup"]["trigger_sequences"]["image"]["steps"][0]["signal"] = "no_camera"
    with pytest.raises(ValueError, match="Unmapped trigger signal"):
        LabConfig.from_dict(data, qubit_id="q0")


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), "10 us"])
def test_invalid_trigger_time_fails(value):
    data = profile()
    data["setup"]["trigger_sequences"]["image"]["steps"][0]["duration_s"] = value
    with pytest.raises((ValueError, TypeError)):
        LabConfig.from_dict(data, qubit_id="q0")


def test_unknown_schema_and_backend_fail():
    data = profile()
    data["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        LabConfig.from_dict(data, qubit_id="q0")
    data = profile()
    data["backend"] = "not_a_registered_backend"
    with pytest.raises(NotImplementedError, match="Unknown backend"):
        LabConfig.from_dict(data, qubit_id="q0")
