"""RAM latency must be an explicit, portable laboratory timing contract."""

from dataclasses import replace

import pytest

from taiyidsl import Device, LabConfig, Qubit, reference_lab


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "64"])
def test_invalid_ram_latency(value):
    with pytest.raises(ValueError, match="ram_latency_mu"):
        Device("dds0", "dds", ram_latency_mu=value)


@pytest.mark.parametrize("value", [None, 64])
def test_ram_latency_config_roundtrip(value):
    lab = reference_lab([Qubit("q0")])
    name = "urukul0_ch0"
    lab.setup.devices[name] = replace(lab.setup.devices[name], ram_latency_mu=value)
    loaded = LabConfig.from_dict(lab.to_dict())
    assert loaded.setup.devices[name].ram_latency_mu == value
