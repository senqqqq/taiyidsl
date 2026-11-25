"""The same one-shot program drives both TTLs, only after both AWGs are ready."""

import json
import math
import os
from dataclasses import replace
from pathlib import Path

import pytest
from spectrum_sdk_double import FakeSdk
from test_nix_worker import build, isolated_launcher

from taiyidsl import DeviceSetup, LabConfig
from taiyidsl.config.spectrum import (
    SpectrumCard,
    SpectrumConfig,
    SpectrumGroup,
    SpectrumOutput,
)
from taiyidsl.runtime.spectrum.awg import (
    SpcmAdapter,
    SpectrumRunner,
    SpectrumRuntimeError,
)


@pytest.fixture(scope="module")
def tutorial():
    notebook = (
        Path(__file__).resolve().parents[1]
        / "docs/tutorials/artiq_spectrum_dual_ttl_clock_zh.ipynb"
    )
    ns = {}
    for cell in json.loads(notebook.read_text(encoding="utf-8"))["cells"]:
        if cell["id"] in {"inputs", "configuration", "one-shot"}:
            exec(compile("".join(cell["source"]), str(notebook), "exec"), ns)
        if cell["id"] == "inputs":
            ns.update(SERIAL_0="101", SERIAL_1="102")  # SDK doubles, still offline.
    compiled = ns["compiled"]
    assert ns["lab"].emulation and len(compiled.points) == compiled.shots == 1
    return ns


@pytest.fixture(scope="module")
def pair(tmp_path_factory, tutorial):
    export = tutorial["compiled"].export(tmp_path_factory.mktemp("dual-export"))
    return json.loads(export["manifest"].read_text(encoding="utf-8"))


def test_dual_tutorial_preserves_ports_timestamps_and_each_pulse(pair):
    assert pair["artiq_verification"]["offline_export_check"] == "passed"
    (group,) = pair["points"][0]["groups"]
    assert group["trigger_devices"] == {"spcm0": "ttl20", "spcm1": "ttl21"}
    assert [
        (r["device"], r["physical_mu"], r["width_mu"]) for r in group["trigger_routes"]
    ] == [("ttl20", 0, 64), ("ttl21", 0, 64)]
    for name, freq in [("spcm0", 200_000), ("spcm1", 400_000)]:
        card = group["cards"][name]
        assert card["contract"]["output_basis"] == "setpoints_only"
        assert card["contract"]["active_channels"] == [0]
        a, b = card["program"]["pulses"]
        assert a["frequency_hz"] == b["frequency_hz"] == freq
        assert a["duration_samples"] == b["duration_samples"] == 62_500
        assert b["start_sample"] - a["start_sample"] - a["duration_samples"] == 31_250
        assert b["phase_rad"] - a["phase_rad"] == pytest.approx(math.pi / 2)


@pytest.mark.parametrize("failure", [None, "second_dma", "second_not_ready"])
def test_pair_waits_for_both_never_retries(pair, failure):
    adapters, sdks = {}, {}
    for name, serial in [("spcm0", 101), ("spcm1", 102)]:
        sdk = FakeSdk(serial=serial)
        sdk.clock["mode"] = sdk.SPC_CM_INTPLL
        sdks[name] = sdk
        adapters[name] = SpcmAdapter(str(serial), sdk=sdk)
    second = sdks["spcm1"]
    if failure == "second_dma":

        def transfer_fails(card):
            raise OSError("second DMA failed")

        second.DataTransfer = transfer_fails
    if failure == "second_not_ready":
        second.card.status = lambda: second.M2STAT_CARD_READY
    runner = SpectrumRunner(adapters, ready_timeout_s=0.01)
    shots = []

    def trigger(point):
        assert all(card.ready() for card in adapters.values())
        shots.append(point)

    if failure:
        with pytest.raises(
            (OSError, SpectrumRuntimeError), match="second DMA|did not become ready"
        ):
            runner.run_point(pair, 0, trigger)
        assert not shots
        assert all(any(c[0] == "stop" for c in sdk.log) for sdk in sdks.values())
        if failure == "second_dma":
            assert not any(c[0] == "start" for sdk in sdks.values() for c in sdk.log)
    else:
        result = runner.run_point(pair, 0, trigger)
        assert shots == [0] and result["status"] == "complete"
        assert set(result["cards"]) == set(adapters)
        for card in result["cards"].values():
            assert (
                card["output_basis"] == "setpoints_only"
                and card["shared_reference"] is False
            )
            assert card["analog_delay_gain_absolute_phase"] == "not_verified"
    assert not any(c[0] == "clock_write" for sdk in sdks.values() for c in sdk.log)


def test_setpoint_scope_and_conflicts():
    group = SpectrumGroup(
        "ttl20",
        {"A": SpectrumCard("101")},
        {"out": SpectrumOutput("A", 0)},
        output_basis="setpoints_only",
    )
    cfg = SpectrumConfig({"g": group})
    assert SpectrumConfig.from_dict(cfg.to_dict()) == cfg
    with pytest.raises(ValueError, match="Unknown Spectrum output_basis"):
        replace(group, output_basis="maybe_measured")
    with pytest.raises(ValueError, match="uncorrected setpoints"):
        replace(group, calibration={"status": "measured"})
    with pytest.raises(ValueError, match="not DDS transport"):
        replace(group, cards={"A": SpectrumCard("101", mode="dds")})
    internal = replace(
        group.cards["A"], reference_clock_hz=None, clock_policy="internal_pll_bench"
    )
    with pytest.raises(ValueError, match="all internal PLLs"):
        replace(group, cards={"A": internal, "B": SpectrumCard("102")})
    with pytest.raises(ValueError, match="uncorrected setpoints"):
        replace(group, cards={"A": internal}, output_basis="measured")


def test_external_setpoints_cannot_replace_a_strict_compiled_contract():
    _, compiled = build("unused", False)  # existing default: strict external
    group = next(
        e.params
        for e in compiled.points[0].physical_events
        if e.kind == "spectrum_program"
    )
    manifest = {"schema_version": 1, "points": [{"index": 0, "groups": [group]}]}
    group["cards"]["A"]["contract"]["output_basis"] = "setpoints_only"
    sdk = FakeSdk()
    runner = SpectrumRunner({"A": SpcmAdapter("101", sdk=sdk)})
    with pytest.raises(SpectrumRuntimeError, match="output basis mismatch"):
        runner.run_point(manifest, 0, lambda _: pytest.fail("unexpected shot"))
    assert sdk.log == [("open",)]


@pytest.mark.skipif(
    not os.getenv("TAIYIDSL_TEST_NIX_PYTHON"), reason="requires the built Nix worker"
)
def test_nix_dual_tutorial_session(tmp_path, monkeypatch, tutorial):
    launcher, events = isolated_launcher(tmp_path, monkeypatch, "dual_tutorial")
    db = {
        **tutorial["devices"],
        "core": {
            **tutorial["devices"]["core"],
            "arguments": {"ref_period": 1e-9, "host": "unit-test.invalid"},
        },
    }
    setup = DeviceSetup.from_descriptor(
        tutorial["descriptor"], device_db=db, spec_db=tutorial["spec_db"]
    )
    lab = LabConfig(
        setup,
        qubits=[tutorial["q0"], tutorial["q1"]],
        artiq_python=str(launcher),
        emulation=False,
    )
    compiled = lab.compile(tutorial["exp"])
    with lab.connect() as session:
        receipt = session.run(compiled)
    assert receipt["completed_shots"] == [{"point": 0, "shot": 0}]
    assert receipt["physical_acceptance"] == "not_verified"
    cards = receipt["spectrum_receipts"][0]["receipt"]["cards"]
    assert {k: c["serial_number"] for k, c in cards.items()} == {
        "spcm0": "101",
        "spcm1": "102",
    }
    log = events.read_text().splitlines()
    assert log.count("point_0") == 1 and log[: log.index("point_0")].count("start") == 2
    assert log.count("close") == 2 and log[-1] == "closed core"
