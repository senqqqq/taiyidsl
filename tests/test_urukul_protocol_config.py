"""Declared board protocol is a hardware contract, not a pulse optimization."""

import json

import pytest

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    ns,
    play,
)
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_export


@pytest.mark.parametrize("revision", [None, 8, 9])
def test_protocol_roundtrip_for_native_square_or_asf(revision):
    lab = LabConfig(
        DeviceSetup({"dds": Device("dds", "dds", urukul_proto_rev=revision)}),
        SignalMap({"q.raman": "dds"}),
    )
    copied = LabConfig.from_dict(json.loads(json.dumps(lab.to_dict())))
    assert copied.to_dict() == lab.to_dict()


@pytest.mark.parametrize("revision", [True, 8.0, "8", 7, 10])
def test_protocol_requires_exact_supported_integer(revision):
    with pytest.raises(ValueError, match="urukul_proto_rev"):
        Device("dds", "dds", urukul_proto_rev=revision)


def test_polar_selector_cannot_claim_a_different_installed_protocol():
    with pytest.raises(ValueError, match="conflicts"):
        Device(
            "dds",
            "dds",
            ram_destination="powasf",
            ram_profile_control="urukul_rev8",
            urukul_proto_rev=9,
        )


@pytest.mark.parametrize("revision", [8, 9])
def test_declared_protocol_is_checked_for_native_square_without_polar(revision):
    q = Qubit("q")
    device = Device(
        "dds",
        "dds",
        nco_mode="continuous",
        nco_epoch_latency_mu=80,
        square_latency_mu=80,
        urukul_proto_rev=revision,
    )
    exp = Experiment("native_protocol", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, Square(200 * ns))
    compiled = LabConfig(
        DeviceSetup({"dds": device}), SignalMap({q.raman: "dds"})
    ).compile(exp)
    source = generate_source(compiled)
    assert verify_export(compiled, source=source)["offline_export_check"] == "passed"
    corrupted = source.replace(
        "self.result_adapter = None",
        "self.result_adapter = None\n"
        f"        self.device_0.cpld.proto_rev = {17 - revision}",
    )
    with pytest.raises(ValueError, match="urukul_proto_rev"):
        verify_export(compiled, source=corrupted)
