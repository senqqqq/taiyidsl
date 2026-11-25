import copy
import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from spectrum_sdk_double import FakeSdk

from taiyidsl.backends.artiq_spectrum.awg import build_card_program
from taiyidsl.runtime.spectrum.awg import (
    SegmentCache,
    SpcmAdapter,
    SpectrumRunner,
    SpectrumRuntimeError,
    calibration_fingerprint,
    hardware_fingerprint,
)


def contract(serial="101"):
    return {
        "model": "M4i.6631-x8",
        "serial_number": serial,
        "active_channels": [0, 1],
        "sample_rate_hz": 1_250_000_000,
        "reference_clock_hz": 125_000_000,
        "memory_bytes": 4 * 1024**3,
        "output_range_mv": 500,
        "firmware": None,
        "cable_id": "bench-A",
    }


def program(spec):
    return build_card_program(
        spec,
        [
            {
                "channel": 0,
                "start_sample": 0,
                "duration_samples": 500,
                "frequency_hz": 100e6,
                "amplitude": 0.5,
                "phase_rad": 0.0,
            }
        ],
        500,
    )


class FakeAdapter:
    is_hardware = False

    def __init__(self, name, spec, log):
        self.name, self.spec, self.log = name, spec, log
        self.failure = None
        self.becomes_ready = True

    def _call(self, operation):
        self.log.append((self.name, operation))
        if self.failure == operation:
            raise SpectrumRuntimeError(operation)

    def probe(self):
        self._call("probe")
        return {
            **self.spec,
            "firmware": "base1/module2",
            "sequence_available": True,
            "max_segments": 65536,
            "max_steps": 4096,
            "clock_mode": "external_reference",
        }

    def configure(self, spec):
        self._call("configure")

    def upload(self, data, loader):
        self._call("upload")
        for segment in data["segments"]:
            samples = loader(data, segment["id"])
            assert samples.shape == (segment["sample_count"], 2)

    def arm(self):
        self._call("arm")

    def ready(self):
        self._call("ready")
        return self.becomes_ready

    def wait_complete(self, timeout_s):
        self._call("complete")

    def stop(self):
        self._call("stop")


def fixture():
    log = []
    specs = {"a": contract("101"), "b": contract("102")}
    adapters = {name: FakeAdapter(name, spec, log) for name, spec in specs.items()}
    manifest = {
        "schema_version": 1,
        "points": [
            {
                "index": 0,
                "groups": [
                    {
                        "name": "qubit",
                        "cards": {
                            name: {"contract": spec, "program": program(spec)}
                            for name, spec in specs.items()
                        },
                        "calibration": {"status": "unmeasured"},
                    }
                ],
            }
        ],
    }
    return log, adapters, manifest


def measured_profile(adapters):
    return {
        "status": "measured",
        "revision": "measurement-1",
        "card_fingerprints": {
            name: hardware_fingerprint(adapter.spec, adapter.probe())
            for name, adapter in adapters.items()
        },
        "channels": {
            f"{name}:{channel}": {
                "phase_offset_rad": 0.01,
                "gain": 1.01,
                "delay_ns": 0.2,
                "phase_uncertainty_deg": 0.1,
                "timing_uncertainty_ns": 0.1,
                "sample_rate_hz": 1.25e9,
                "sample_shift_samples": 0,
                "phase_reference": "after_sample_shift",
                "carrier_frequency_hz": 100e6,
            }
            for name, adapter in adapters.items()
            for channel in adapter.spec["active_channels"]
        },
    }


def install_profile(manifest, profile):
    group = manifest["points"][0]["groups"][0]
    group["calibration"] = profile
    for entry in group["cards"].values():
        entry["program"]["calibration_sha256"] = calibration_fingerprint(profile)


def test_two_card_atomic_ready_barrier_and_single_artiq_call(tmp_path):
    log, adapters, manifest = fixture()
    runner = SpectrumRunner(adapters, allow_unverified=True, cache_dir=tmp_path)
    result = runner.run_point(manifest, 0, lambda point: log.append((point, "artiq")))
    assert log[:2] == [("a", "probe"), ("b", "probe")]
    launch = log.index((0, "artiq"))
    assert all(log.index((card, "ready")) < launch for card in adapters)
    assert all(
        log.index((card, "upload")) < log.index(("a", "arm")) for card in adapters
    )
    assert log.count((0, "artiq")) == 1
    assert result["status"] == "complete"
    assert result["physical_acceptance"] == "not_verified"
    assert list(tmp_path.glob("*.bin"))


@pytest.mark.parametrize("stage", ["configure", "upload", "arm", "ready", "complete"])
def test_partial_failure_stops_all_prepared_cards(stage):
    log, adapters, manifest = fixture()
    adapters["b"].failure = stage
    runner = SpectrumRunner(adapters, allow_unverified=True)
    with pytest.raises(SpectrumRuntimeError, match=stage):
        runner.run_point(manifest, 0, lambda point: log.append((point, "artiq")))
    assert ("a", "stop") in log
    assert ("b", "stop") in log
    if stage != "complete":
        assert (0, "artiq") not in log


def test_bad_second_card_preflight_makes_no_setup_mutations():
    log, adapters, manifest = fixture()
    adapters["b"].spec = {**adapters["b"].spec, "serial_number": "wrong"}
    with pytest.raises(SpectrumRuntimeError, match="serial_number"):
        SpectrumRunner(adapters, allow_unverified=True).run_point(
            manifest, 0, lambda _: None
        )
    assert all(operation == "probe" for _, operation in log)


def test_not_ready_never_launches_and_stops_both():
    log, adapters, manifest = fixture()
    adapters["b"].becomes_ready = False
    with pytest.raises(SpectrumRuntimeError, match="did not become ready"):
        SpectrumRunner(
            adapters, allow_unverified=True, ready_timeout_s=0.002
        ).run_point(manifest, 0, lambda point: log.append((point, "artiq")))
    assert (0, "artiq") not in log
    assert ("a", "stop") in log and ("b", "stop") in log


def test_measured_profile_runs_without_connection_token_and_binds_hardware():
    log, adapters, manifest = fixture()
    profile = measured_profile(adapters)
    install_profile(manifest, profile)
    runner = SpectrumRunner(adapters)
    assert runner.run_point(manifest, 0, lambda _: 42)["artiq_result"] == 42
    profile["card_fingerprints"]["b"] = "different-setup"
    install_profile(manifest, profile)
    log.clear()
    with pytest.raises(SpectrumRuntimeError, match="hardware fingerprint"):
        runner.run_point(manifest, 0, lambda _: None)
    assert all(operation == "probe" for _, operation in log)


def test_real_adapter_cannot_use_unverified_bypass():
    _, adapters, manifest = fixture()
    adapters["a"].is_hardware = True
    with pytest.raises(SpectrumRuntimeError, match="mock adapters"):
        SpectrumRunner(adapters, allow_unverified=True).run_point(
            manifest, 0, lambda _: None
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("sample_shift_samples", 1, "sample shift"),
        ("phase_reference", "before_sample_shift", "sample shift"),
        ("sample_rate_hz", 1e9, "sample rate"),
        ("carrier_frequency_hz", 200e6, "carrier frequency"),
    ],
)
def test_calibration_sample_shift_and_frequency_must_match(field, value, error):
    log, adapters, manifest = fixture()
    profile = measured_profile(adapters)
    profile["channels"]["a:0"][field] = value
    install_profile(manifest, profile)
    log.clear()
    with pytest.raises(SpectrumRuntimeError, match=error):
        SpectrumRunner(adapters).run_point(manifest, 0, lambda _: None)
    assert all(operation == "probe" for _, operation in log)


def test_changing_profile_without_rerendering_is_rejected():
    log, adapters, manifest = fixture()
    profile = measured_profile(adapters)
    install_profile(manifest, profile)
    profile["channels"]["a:0"]["phase_offset_rad"] += 0.1
    log.clear()
    with pytest.raises(SpectrumRuntimeError, match="rendered program calibration"):
        SpectrumRunner(adapters).run_point(manifest, 0, lambda _: None)
    assert all(operation == "probe" for _, operation in log)


def test_empty_point_runs_artiq_without_touching_spectrum():
    log, adapters, _ = fixture()
    result = SpectrumRunner(adapters).run_point(
        {"schema_version": 1, "points": [{"index": 0, "groups": []}]},
        0,
        lambda _: "artiq-only",
    )
    assert result["artiq_result"] == "artiq-only"
    assert log == []


def test_cache_checks_hash_and_budget(tmp_path):
    data = program(contract())
    first = data["segments"][0]
    cache = SegmentCache(tmp_path, max_bytes=1)
    cache.load(data, first["id"])
    assert not list(tmp_path.glob("*.bin"))
    cache = SegmentCache(tmp_path, max_bytes=1_000_000)
    cache.load(data, first["id"])
    target = tmp_path / f"{first['sha256']}.bin"
    target.write_bytes(b"broken")
    with pytest.raises(SpectrumRuntimeError, match="Corrupt"):
        cache.load(data, first["id"])


def test_runtime_rejects_modified_sequence_before_any_setup():
    log, adapters, manifest = fixture()
    manifest["points"][0]["groups"][0]["cards"]["a"]["program"]["steps"][0]["loops"] = 0
    with pytest.raises(ValueError):
        SpectrumRunner(adapters, allow_unverified=True).run_point(
            manifest, 0, lambda _: None
        )
    assert all(operation == "probe" for _, operation in log)


def test_real_sdk_sequence_upload_and_trigger_settings():
    sdk = FakeSdk()
    adapter = SpcmAdapter("101", sdk=sdk)
    spec = contract()
    adapter.configure(spec)
    assert not [item for item in sdk.log if item[0] == "clock_write"]
    assert ("trigger", "or_mask", sdk.SPC_TMASK_EXT0) in sdk.log
    assert ("trigger", "ext0_mode", sdk.SPC_TM_POS) in sdk.log
    assert ("trigger", "delay", 0) in sdk.log
    assert ("stop_level", 0, sdk.SPCM_STOPLVL_ZERO) in sdk.log
    assert ("offset", 0, 0) in sdk.log
    data = program(spec)
    adapter.upload(data, SegmentCache().load)
    uploads = [item for item in sdk.log if item[0] == "dma"]
    assert len(uploads) == len(data["segments"])
    assert uploads[0][2].shape[0] == 2
    adapter.arm()
    assert adapter.ready()
    sdk.card.state = sdk.M2STAT_CARD_TRIGGER
    with pytest.raises(SpectrumRuntimeError, match="Unexpected trigger"):
        adapter.ready()
    adapter.wait_complete(1)
    assert not adapter.ready()


def test_sdk_mode_refusal_is_rejected_before_upload():
    sdk = FakeSdk()
    sdk.card.card_mode = lambda value: -1
    with pytest.raises(SpectrumRuntimeError, match="sequence mode"):
        SpcmAdapter("101", sdk=sdk).configure(contract())
    assert not any(item[0] == "dma" for item in sdk.log)


@pytest.mark.parametrize(
    "setting",
    [
        "delay",
        "or_mask",
        "and_mask",
        "ext0_mode",
        "ext0_coupling",
        "termination",
        "ext0_level0",
    ],
)
def test_sdk_trigger_registers_must_read_back_exactly(setting):
    sdk = FakeSdk()
    sdk.bad_trigger_readback = setting
    with pytest.raises(SpectrumRuntimeError, match=f"Trigger {setting} readback"):
        SpcmAdapter("101", sdk=sdk).configure(contract())
    assert not any(item[0] == "dma" for item in sdk.log)


def test_clock_reconfiguration_requires_explicit_recalibration():
    sdk = FakeSdk()
    adapter = SpcmAdapter("101", sdk=sdk)
    sdk.clock["reference_clock"] = 10_000_000
    with pytest.raises(SpectrumRuntimeError, match="Clock setup changed"):
        adapter.configure(contract())
    assert not [item for item in sdk.log if item[0] == "clock_write"]
    adapter.configure_for_calibration(contract())
    assert sdk.clock["reference_clock"] == 125_000_000


@pytest.mark.parametrize("address", [None, "/dev/example-card"])
def test_failed_open_with_null_handle_never_stops_card_and_can_retry(address):
    sdk = FakeSdk()
    failed = MagicMock()
    failed.__bool__.return_value = False
    failed._closed = False  # spcm 1.13.4 state after open_handle returns NULL
    original = OSError("device not found")
    failed.open.side_effect = original
    sdk.Card = MagicMock(side_effect=[failed, sdk.card])
    adapter = SpcmAdapter("101", address=address, sdk=sdk)
    with pytest.raises(OSError) as caught:
        adapter.probe()
    assert caught.value is original
    assert "another process" in caught.value.__notes__[0]
    assert failed._closed is True  # Device.__del__ now skips STOP on NULL.
    adapter.close()
    failed.close.assert_not_called()
    failed.close_handle.assert_not_called()
    failed.sn.assert_not_called()
    assert adapter.probe()["serial_number"] == "101"
    adapter.close()
    assert sdk.Card.call_count == 2


@pytest.mark.parametrize("failure", ["metadata", "serial"])
def test_failed_open_with_live_handle_releases_it_without_stopping(failure):
    card = MagicMock()  # Has a handle but ownership is not verified yet.
    if failure == "metadata":
        card.open.side_effect = OSError("metadata read failed")
        expected, message = OSError, "metadata"
    else:
        card.sn.return_value = 202
        expected, message = SpectrumRuntimeError, "different card serial"
    adapter = SpcmAdapter("101", sdk=SimpleNamespace(Card=lambda **kwargs: card))
    with pytest.raises(expected, match=message):
        adapter.probe()
    adapter.close()
    card.close_handle.assert_called_once_with()
    card.close.assert_not_called()
    card.stop.assert_not_called()


def test_sdk_import_is_lazy(monkeypatch):
    calls = []
    original = importlib.import_module

    def spy(name, *args, **kwargs):
        if name == "spcm":
            calls.append(name)
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", spy)
    adapter = SpcmAdapter("101")
    assert calls == []
    with pytest.raises(SpectrumRuntimeError, match="optional Spectrum SDK"):
        adapter.probe()
    assert calls == ["spcm"]
    with pytest.raises(ValueError, match="physical card serial"):
        SpcmAdapter("SIM_A")


def test_fingerprint_changes_with_cable_range_and_clock():
    spec = contract()
    initial = hardware_fingerprint(spec)
    for key, value in (
        ("cable_id", "new"),
        ("output_range_mv", 600),
        ("sample_rate_hz", 625e6),
        ("trigger_latency_samples", 499),
        ("trigger_device", "other_ttl"),
        ("epoch_grid_mu", 32),
        ("epoch_offset_mu", 16),
        ("trigger_width_mu", 64),
        ("artiq_tick_seconds", 1e-9),
    ):
        changed = copy.deepcopy(spec)
        changed[key] = value
        assert hardware_fingerprint(changed) != initial


def test_hybrid_sequence_commit_staging_and_probe_only_close():
    sdk = FakeSdk()
    adapter = SpcmAdapter("101", sdk=sdk)
    adapter.probe()
    adapter.close()
    assert sdk.log == [("open",), ("close",)]
    adapter.configure(contract())
    assert ("write_setup",) not in sdk.log
    assert ("trigger", "or_mask", sdk.SPC_TMASK_EXT0) in sdk.log
    with pytest.raises(SpectrumRuntimeError, match="Upload a complete"):
        adapter.arm()
    adapter.upload(program(contract()), SegmentCache().load)
    assert sdk.log[-2:] == [("sequence", "start_step", (0,), {}), ("write_setup",)]
    assert not any(x[0] == "start" for x in sdk.log)
    adapter.close()
    assert sdk.card.mode == sdk.SPC_REP_STD_SINGLE
    assert sdk.log[-4:] == [
        ("output", 0, False),
        ("output", 1, False),
        ("write_setup",),
        ("close",),
    ]
    assert not any(x[0] == "clock_write" for x in sdk.log)


@pytest.mark.parametrize("operation", ["replace_bank", "select_entry"])
def test_failed_upload_or_entry_commit_cannot_arm_stale_bank(monkeypatch, operation):
    sdk = FakeSdk()
    adapter = SpcmAdapter("101", sdk=sdk)
    adapter.configure(contract())
    data = program(contract())
    adapter.upload(data, SegmentCache().load)

    def fail(*args):
        raise OSError("injected bank failure")

    with monkeypatch.context() as m, pytest.raises(IOError, match="bank failure"):
        if operation == "select_entry":
            m.setattr(sdk.card, "write_setup", fail)
            adapter.select_entry(0)
        else:
            adapter.upload(data, fail)
    with pytest.raises(SpectrumRuntimeError, match="Upload a complete"):
        adapter.arm()
    with pytest.raises(SpectrumRuntimeError, match="resident entry"):
        adapter.select_entry(0)
    adapter.close()
    assert sdk.card.mode == sdk.SPC_REP_STD_SINGLE
    assert sdk.log[-1] == ("close",)


def test_failed_clock_commit_keeps_card_disarmed(monkeypatch):
    sdk = FakeSdk()
    adapter = SpcmAdapter("101", sdk=sdk)

    def reject_commit():
        assert sdk.card.mode == sdk.SPC_REP_STD_SINGLE
        assert sdk.log[-2:] == [("output", 0, False), ("output", 1, False)]
        raise OSError("clock commit failed")

    monkeypatch.setattr(sdk.card, "write_setup", reject_commit)
    with pytest.raises(IOError, match="clock commit failed"):
        adapter.configure_for_calibration(contract())
    with pytest.raises(SpectrumRuntimeError, match="Upload a complete"):
        adapter.arm()
    assert not any(x[0] == "start" for x in sdk.log)


def test_hybrid_cleanup_error_keeps_upload_failure_and_stops_other_card(monkeypatch):
    log, adapters, manifest = fixture()
    adapters["b"].failure = "upload"

    def bad_stop():
        raise OSError("cannot confirm mute")

    monkeypatch.setattr(adapters["b"], "stop", bad_stop)
    runner = SpectrumRunner(adapters, allow_unverified=True)
    with pytest.raises(SpectrumRuntimeError, match="upload") as error:
        runner.run_point(manifest, 0, lambda _: pytest.fail("unexpected ARTIQ launch"))
    assert any("cannot confirm mute" in note for note in error.value.__notes__)
    assert ("a", "stop") in log


@pytest.mark.parametrize("schema", [1, 2])
def test_real_adapter_partial_second_upload_never_launches_first(monkeypatch, schema):
    from test_spectrum_banks import resident_fixture

    _, _, manifest = fixture() if schema == 1 else resident_fixture()
    sdks = [FakeSdk(), FakeSdk()]
    adapters = {}
    for name, serial, sdk in zip(("a", "b"), (101, 102), sdks, strict=True):
        monkeypatch.setattr(sdk, "Card", lambda *, serial_number, card=sdk.card: card)
        monkeypatch.setattr(sdk.card, "sn", lambda serial=serial: serial)
        adapter = SpcmAdapter(str(serial), sdk=sdk)
        monkeypatch.setattr(adapter, "is_hardware", False)  # Injected SDK only.
        adapters[name] = adapter

    def fail(*args):
        raise OSError("second card DMA failed")

    monkeypatch.setattr(sdks[1], "DataTransfer", fail)
    runner = SpectrumRunner(adapters, allow_unverified=True)
    with pytest.raises(IOError, match="second card DMA failed") as caught:
        runner.run_point(manifest, 0, lambda _: pytest.fail("unexpected ARTIQ launch"))
    assert not getattr(caught.value, "__notes__", [])
    for sdk in sdks:
        assert sdk.card.mode == sdk.SPC_REP_STD_SINGLE
        assert ("output", 0, False) in sdk.log and ("output", 1, False) in sdk.log
        assert not any(x[0] == "start" for x in sdk.log)
