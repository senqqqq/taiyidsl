"""Explicit polar timing contracts survive configuration and submission checks."""

import json
from dataclasses import replace

import pytest

from taiyidsl import CompileError, Device, DeviceSetup, LabConfig, SignalMap
from taiyidsl.backends.artiq.submission import validate_submission_plan
from taiyidsl.ir import ScheduledEvent


def polar_device(name="a", bus="spi0", **options):
    return Device(
        name,
        "dds",
        bus=bus,
        ram_destination="powasf",
        ram_profile_control="urukul_rev8",
        ram_latency_mu=112,
        ram_polar_phase_latency_mu=100,
        ram_profile_latency_mu=112,
        ram_profile_phase_latency_mu=100,
        **options,
    )


def test_polar_configuration_roundtrip_preserves_separate_pipeline_contracts():
    lab = LabConfig(DeviceSetup({"a": polar_device()}), SignalMap({"q.drive": "a"}))
    loaded = LabConfig.from_dict(json.loads(json.dumps(lab.to_dict())))
    assert loaded.to_dict() == lab.to_dict()
    assert loaded.setup.devices["a"].ram_pow_latency_mu is None
    assert loaded.setup.devices["a"].ram_polar_phase_latency_mu == 100


@pytest.mark.parametrize(
    "field,value",
    [
        ("ram_destination", "polar"),
        ("ram_profile_control", "fast"),
        ("ram_polar_phase_latency_mu", True),
        ("ram_profile_latency_mu", 0),
        ("ram_profile_phase_latency_mu", 2.5),
    ],
)
def test_invalid_polar_configuration_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        replace(polar_device(), **{field: value})


def test_polar_destination_requires_dds():
    with pytest.raises(ValueError, match="DDS"):
        Device("ttl", "ttl", ram_destination="powasf")


def selector(name, start=0, duration=416):
    return ScheduledEvent(
        "dds_polar_select", name, start, duration, {"effect_mu": start + duration + 112}
    )


def test_independent_selectors_can_overlap_without_buffered_commit():
    setup = DeviceSetup(
        {
            "a": polar_device(),
            "b": polar_device("b", "spi1"),
            "ttl": Device("ttl", "ttl"),
        }
    )
    validate_submission_plan(
        [
            selector("a"),
            selector("b"),
            ScheduledEvent("ttl_on", "ttl", 200, 0),
            ScheduledEvent("ttl_off", "ttl", 400, 0),
        ],
        setup,
    )


@pytest.mark.parametrize("bus", ["spi0", None])
def test_selectors_cannot_hide_shared_or_unknown_bus_occupancy(bus):
    setup = DeviceSetup({"a": polar_device(), "b": polar_device("b", bus)})
    with pytest.raises(CompileError, match="SPI bus"):
        validate_submission_plan([selector("a"), selector("b")], setup)


def test_selector_duration_cannot_fake_a_faster_interface():
    setup = DeviceSetup(
        {"a": replace(polar_device(), ram_profile_control="urukul_rev9")}
    )
    with pytest.raises(CompileError, match="duration differs"):
        validate_submission_plan([selector("a")], setup)


def test_retrigger_cannot_interrupt_selection():
    setup = DeviceSetup({"a": polar_device()})
    with pytest.raises(CompileError, match="IO_UPDATE overlaps"):
        validate_submission_plan(
            [selector("a"), ScheduledEvent("dds_polar_retrigger", "a", 200, 0)], setup
        )
