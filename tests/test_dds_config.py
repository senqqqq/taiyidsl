"""Per-card native DDS configuration and spatial optical wiring, without IO."""

import builtins
import copy

import pytest
from test_device_descriptor import databases

from taiyidsl import DeviceSetup, SpectrumCard, SpectrumConfig, SpectrumGroup
from taiyidsl.spectrum_config import SpectrumOutput


def mixed_descriptor():
    descriptor, device_db, spec_db = databases()
    device_db["trig_c"] = {
        "type": "local",
        "module": "artiq.coredevice.ttl",
        "class": "TTLOut",
        "arguments": {"channel": 4},
    }
    spec_db["C"] = {
        "serial_number": "SIM_C",
        "mode": "dds",
        "dds_routing": "16/5",
        "dds_trigger_latency_ns": 560.0,
    }
    descriptor["instruments"]["C"] = {"kind": "spectrum", "device": "C"}
    descriptor["channels"].update(
        aod_x={"instrument": "C", "channel": 0},
        aod_y={"instrument": "C", "channel": 1},
    )
    descriptor["channels"]["clock"]["coverage"] = {"zones": ["iz0", "iz1"]}
    descriptor["signals"]["q1.clock"] = "clock"
    descriptor["sync_groups"]["qubit_awg"]["cards"].append("C")
    descriptor["sync_groups"]["qubit_awg"]["triggers"]["C"] = "trig_c"
    return descriptor, device_db, spec_db


def test_legacy_awg_serialization_and_native_dds_routes():
    card = SpectrumCard("SIM_A")
    legacy = card.to_dict()
    assert card.mode == "awg"
    assert not {"mode", "dds_routing", "dds_trigger_latency_ns"} & legacy.keys()
    assert SpectrumCard(**legacy) == card
    dds = SpectrumCard("SIM_C", mode="dds")
    assert dds.dds_trigger_latency_ns is None
    assert dds.dds_channel_cores == {
        0: (*range(8), *range(12, 20)),
        1: (8, 9, 10, 11, 20),
    }
    assert SpectrumCard("SIM_C", mode="dds", dds_routing="20/1").dds_channel_cores == {
        0: tuple(range(20)),
        1: (20,),
    }
    group = SpectrumGroup(
        trigger_device="trigger",
        cards={"A": card, "C": dds},
        outputs={"rf": SpectrumOutput("A", 0), "x": SpectrumOutput("C", 0)},
    )
    config = SpectrumConfig({"mixed": group})
    data = config.to_dict()
    assert data["groups"]["mixed"]["cards"]["A"] == {
        **legacy,
        "active_channels": [0, 1],
    }
    assert data["groups"]["mixed"]["cards"]["C"]["mode"] == "dds"
    assert SpectrumConfig.from_dict(data).to_dict() == data


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"mode": "DDS"}, "mode"),
        ({"mode": "dds", "dds_routing": "10/10"}, "routing"),
        ({"mode": "dds", "dds_trigger_latency_ns": float("nan")}, "latency"),
        ({"mode": "dds", "dds_trigger_latency_ns": True}, "latency"),
        ({"mode": "dds", "dds_amplitude_slope_step_per_s": 0}, "finite and positive"),
        ({"dds_trigger_latency_ns": 560.0}, "mode='dds'"),
    ],
)
def test_invalid_modes_routes_and_latency_are_explicit(settings, message):
    with pytest.raises(ValueError, match=message):
        SpectrumCard("SIM", **settings)


def test_mixed_descriptor_coverage_and_settings_are_detached_without_drivers(
    monkeypatch,
):
    original_import = builtins.__import__

    def offline_import(name, *args, **kwargs):
        if name.split(".")[0] in {"spcm", "artiq"}:
            pytest.fail(f"Configuration imported hardware driver {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", offline_import)
    descriptor, device_db, spec_db = mixed_descriptor()
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    assert setup.zone_coverage == {"clock": ("iz0", "iz1")}
    assert "clock" not in setup.shared_outputs
    assert setup.to_dict()["zone_coverage"] == {"clock": ["iz0", "iz1"]}
    cards = setup.spectrum.groups["qubit_awg"].cards
    assert [cards[name].mode for name in ("A", "B", "C")] == ["awg", "awg", "dds"]
    assert setup.describe()["instruments"]["C"]["settings"]["mode"] == "dds"
    snapshot = copy.deepcopy(setup.to_dict())
    setup.zone_coverage["clock"] = ("wrong",)
    descriptor["channels"]["clock"]["coverage"]["zones"].append("wrong")
    spec_db["C"]["mode"] = "awg"
    assert setup.to_dict() == snapshot


def test_coverage_rejects_invalid_zones_and_non_optical_outputs():
    descriptor, device_db, spec_db = mixed_descriptor()
    for zones in (["iz0", "iz0"], [], "iz0", [""]):
        changed = copy.deepcopy(descriptor)
        changed["channels"]["clock"]["coverage"] = {"zones": zones}
        with pytest.raises(ValueError, match="Coverage zones"):
            DeviceSetup.from_descriptor(changed, device_db=device_db, spec_db=spec_db)
    descriptor["channels"]["aod_x"]["coverage"] = {"zones": ["iz0"]}
    with pytest.raises(ValueError, match="AWG optical output"):
        DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)


def test_dds_does_not_accept_awg_carrier_state():
    with pytest.raises(ValueError, match="AWG carrier_mode"):
        SpectrumGroup(
            trigger_device="trigger",
            cards={"C": SpectrumCard("SIM_C", mode="dds")},
            outputs={"x": SpectrumOutput("C", 0, "continuous", 100e6)},
        )
