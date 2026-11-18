"""The exporter requests and initializes touched outputs, not the lab inventory."""

from dataclasses import replace

import pytest

from taiyidsl import Device, Experiment, Qubit, Square, delay, play, reference_lab, us
from taiyidsl.artiq import (
    ExportError,
    _DriverClock,
    _fake_artiq,
    _MockCore,
    _MockDDS,
    export_artiq,
    generate_source,
    verify_export,
)
from taiyidsl.execution import device_execution_plan


def plan(*, shared=False, empty=False):
    q = Qubit("q0")
    lab = reference_lab([q])
    # q0.rf is deliberately left unused, either on this board or a different one.
    if not shared:
        lab.setup.devices["urukul0_ch1"] = replace(
            lab.setup.devices["urukul0_ch1"],
            bus="other_spi",
            update_group="other_board",
        )
    lab.setup.devices["mot"] = Device("mot", "ttl")
    exp = Experiment("one_path", [q])
    with exp.average(shots=2):
        with exp.realtime():
            if empty:
                delay(q.raman, 10 * us)
            else:
                play(q.raman, Square(10 * us))
    return lab.compile(exp)


def test_only_used_channels_are_requested_and_initialized(tmp_path):
    compiled = plan()
    source = generate_source(compiled)
    assert "get_device('urukul0_ch0')" in source
    for unused in ("urukul0_ch1", "mot", "ttl_0_prepare", "ttl_0_readout"):
        assert f"get_device('{unused}')" not in source
    assert source.count(".init()") == 1
    report = verify_export(compiled)
    assert report["initialization"]["used_devices"] == ["urukul0_ch0"]
    assert report["initialization"]["status"] == "passed"
    artifact = export_artiq(compiled, tmp_path)
    assert artifact["verification"]["initialization"]["unused_devices"]


def test_partial_board_requires_external_preparation_without_dds_init():
    compiled = plan(shared=True)
    source = generate_source(compiled)
    assert ".init()" not in source
    contract = device_execution_plan(compiled)
    assert contract["prepared_shared_resources_required"] == [
        "bus:spi0",
        "update_group:urukul0",
    ]
    clock = _DriverClock()
    devices = {
        "core": _MockCore(clock),
        "urukul0_ch0": _MockDDS(
            clock, "urukul0_ch0", compiled.setup.devices["urukul0_ch0"]
        ),
    }
    with _fake_artiq(clock, devices):
        namespace = {}
        exec(source, namespace)
        exp = namespace["GeneratedExperiment"]()
        exp.build()
        with pytest.raises(RuntimeError, match="Shared DDS resource"):
            exp._validate_target()
        exp.prepared_shared_resources = tuple(
            contract["prepared_shared_resources_required"]
        )
        exp._validate_target()
    assert verify_export(compiled)["initialization"]["initialize_dds"] == []


def test_empty_output_shot_does_not_touch_inventory():
    compiled = plan(empty=True)
    source = generate_source(compiled)
    assert source.count("get_device(") == 1  # core only
    assert verify_export(compiled)["initialization"]["used_devices"] == []


def test_mock_detects_added_switch_on_in_initialization():
    compiled = plan()
    source = generate_source(compiled).replace(
        "self.device_0.sw.off()", "self.device_0.sw.on()", 1
    )
    with pytest.raises(ExportError, match="initialization"):
        verify_export(compiled, source=source)


def test_shared_dds_init_cannot_be_silently_reintroduced():
    compiled = plan(shared=True)
    source = generate_source(compiled).replace(
        "self.device_0.sw.off()",
        "self.device_0.init()\n        self.device_0.sw.off()",
        1,
    )
    with pytest.raises(ExportError, match="initialization"):
        verify_export(compiled, source=source)


def test_mock_rejects_register_writes_added_to_initialization():
    compiled = plan()
    source = generate_source(compiled).replace(
        "self.device_0.sw.off()",
        "self.device_0.set_mu(0)\n        self.device_0.sw.off()",
        1,
    )
    with pytest.raises(ExportError, match="register write during initialization"):
        verify_export(compiled, source=source)


def test_mock_rejects_initialization_added_inside_a_shot():
    compiled = plan()
    source = generate_source(compiled)
    assert "        at_mu(t0)" in source
    source = source.replace(
        "        at_mu(t0)",
        "        at_mu(t0)\n        self.device_0.init()",
        1,
    )
    with pytest.raises(ExportError, match="initialization is forbidden inside a shot"):
        verify_export(compiled, source=source)
