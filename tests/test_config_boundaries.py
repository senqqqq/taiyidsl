"""Configuration snapshots retain their wire format without DSL serialization."""

import json
from fractions import Fraction

from taiyidsl.config.hardware import (
    Device,
    DeviceCalibration,
    DeviceSetup,
    TriggerSequence,
    TriggerStep,
)
from taiyidsl.config.spectrum import (
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
)
from taiyidsl.units import us


def test_configuration_serialization_preserves_fraction_and_quantity_formats():
    """Keep the supported pre-migration model encoding, including omitted defaults."""
    spectrum = SpectrumConfig(
        {
            "awg": SpectrumGroup(
                trigger_device="trigger",
                cards={"card": SpectrumCard("SIM_CONFIG")},
                outputs={"rf": SpectrumOutput("card", 0)},
                calibration={
                    "gain": Fraction(2, 3),
                    "settling": 2 * us,
                    "samples": (Fraction(1, 4), Fraction(3, 4)),
                },
            )
        }
    )
    setup = DeviceSetup(
        devices={"trigger": Device("trigger", "ttl")},
        protocols={
            "readout": TriggerSequence(
                (TriggerStep("camera", 2 * us, Fraction(1, 1_000_000)),)
            )
        },
        calibration={
            "trigger": DeviceCalibration(
                phase_offset=Fraction(1, 4), amplitude_scale=Fraction(3, 2)
            )
        },
        spectrum=spectrum,
    )

    data = setup.to_dict()
    group = data["spectrum"]["groups"]["awg"]
    assert group["calibration"] == {
        "gain": 2 / 3,
        "settling": {"value": 2e-6, "dimension": "time"},
        "samples": [0.25, 0.75],
    }
    assert data["protocols"]["readout"]["steps"] == [
        {
            "signal": "camera",
            "duration": {"value": 2e-6, "dimension": "time"},
            "offset": 1e-6,
        }
    ]
    assert data["calibration"]["trigger"]["phase_offset"] == 0.25
    assert data["calibration"]["trigger"]["amplitude_scale"] == 1.5
    assert "ttl_idle_level" not in data["devices"]["trigger"]
    assert group["outputs"] == {"rf": {"card": "card", "channel": 0}}
    assert group["cards"]["card"]["active_channels"] == [0, 1]
    assert "mode" not in group["cards"]["card"]
    assert "trigger_devices" not in group and "trigger_signal" not in group
    assert json.loads(json.dumps(data)) == data
    assert SpectrumConfig.from_dict(data["spectrum"]).to_dict() == data["spectrum"]

    without_spectrum = DeviceSetup(devices={"trigger": Device("trigger", "ttl")})
    assert "spectrum" not in without_spectrum.to_dict()
