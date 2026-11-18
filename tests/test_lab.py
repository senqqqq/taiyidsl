"""Laboratory profiles preserve the reference contracts and isolate compilations."""

import pytest

from taiyidsl import DeviceCalibration, Qubit, rabi, reference_setup, us


def assert_reference_contract(setup, mapping):
    assert mapping == {
        "q0.raman": "urukul0_ch0",
        "q0.rf": "urukul0_ch1",
        "q0.prepare": "ttl_0_prepare",
        "q0.readout": "ttl_0_readout",
        "q-one.raman": "urukul1_ch0",
        "q-one.rf": "urukul1_ch1",
        "q-one.prepare": "ttl_1_prepare",
        "q-one.readout": "ttl_1_readout",
    }
    assert set(setup.devices) == set(mapping.values())
    assert setup.name == "reference-offline-only"
    assert setup.tick_seconds == 1e-9
    assert setup.timing_verified is False
    assert setup.calibration == {}
    for name, device in setup.devices.items():
        if name.startswith("urukul"):
            assert device.kind == "dds"
            assert device.grid_mu == 4
            assert device.config_grid_mu == 8
            assert device.config_duration_mu == 3000
            assert device.square_latency_mu == 80
            assert device.square_prepare_duration_mu == 1280
            assert device.sysclk_hz == 1e9
            assert device.bus == "spi" + name[6]
            assert device.update_group == name.split("_")[0]
        else:
            assert device.kind == "ttl"
            assert device.grid_mu == device.min_duration_mu == 1
    preparation = setup.protocols["standard"]
    assert preparation.steps[0].signal == "{qubit}.prepare"
    assert preparation.steps[0].duration == 20 * us
    assert preparation.steps[0].offset == 0
    readout = setup.protocols["state_readout"]
    assert readout.steps[0].signal == "{qubit}.readout"
    assert readout.steps[0].duration is None
    assert readout.steps[0].offset == 0


def test_legacy_reference_setup_contract():
    assert_reference_contract(*reference_setup([Qubit("q0"), Qubit("q-one")]))


def test_reference_lab_preserves_layout_for_qubit_mapping():
    from taiyidsl.lab import reference_lab

    lab = reference_lab({"q0": Qubit("q0"), "q-one": Qubit("q-one")})
    assert lab.backend == "artiq"
    assert_reference_contract(lab.setup, lab.signal_map)


def test_initialization_lab_uses_supplied_qubit_and_camera_trigger():
    from taiyidsl.lab import initialization_lab

    lab = initialization_lab(Qubit("atom-a"))
    assert lab.signal_map == {
        "optics.pump_556": "urukul0_ch0",
        "optics.pushout_556": "urukul0_ch1",
        "atom-a.readout": "ttl0",
    }
    assert lab.setup.name == "initialization-offline-only"
    assert lab.setup.timing_verified is False
    assert lab.setup.devices["urukul0_ch0"].update_group == "urukul0"
    assert lab.setup.devices["urukul0_ch1"].bus == "spi0"
    (step,) = lab.setup.protocols["image"].steps
    assert (step.signal, step.duration, step.offset) == ("{qubit}.readout", 10 * us, 0)


def test_lab_compile_snapshots_and_forwards_quantization():
    from taiyidsl.lab import reference_lab

    q = Qubit("q0")
    lab = reference_lab([q])
    compiled = lab.compile(rabi(q, [5.0013 * us]), allow_quantization=True)
    before = compiled.to_dict()
    lab.signal_map.clear()
    lab.setup.calibration["urukul0_ch0"] = DeviceCalibration(phase_offset=0.4)
    lab.setup.protocols.clear()
    assert compiled.to_dict() == before
    compiled.assert_integrity()
    assert any(d["kind"] == "time_quantization" for d in compiled.diagnostics)


def test_lab_profiles_are_fresh_and_own_the_backend_selection():
    from taiyidsl.lab import reference_lab

    q = Qubit("q0")
    first, second = reference_lab([q]), reference_lab([q])
    first.setup.protocols.clear()
    first.signal_map.clear()
    assert second.setup.protocols
    assert second.signal_map
    with pytest.raises(TypeError, match="backend"):
        second.compile(rabi(q, [4 * us]), backend="artiq")
