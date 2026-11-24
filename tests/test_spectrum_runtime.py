import copy
import importlib
from types import SimpleNamespace

import numpy as np
import pytest

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
        self.clock_session_id = f"session-{name}"
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
        "clock_session_ids": {
            name: adapter.clock_session_id for name, adapter in adapters.items()
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


def test_measured_profile_binds_clock_session_and_firmware():
    log, adapters, manifest = fixture()
    profile = measured_profile(adapters)
    install_profile(manifest, profile)
    runner = SpectrumRunner(adapters)
    assert runner.run_point(manifest, 0, lambda _: 42)["artiq_result"] == 42
    adapters["b"].clock_session_id = "after-relock"
    log.clear()
    with pytest.raises(SpectrumRuntimeError, match="clock session"):
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


class FakeSdk:
    def __init__(self):
        self.log = []
        names = (
            "SPC_PCIMEMSIZE SPC_PCIVERSION SPC_PCIMODULEVERSION "
            "SPC_SEQMODE_AVAILMAXSEGMENT SPC_SEQMODE_AVAILMAXSTEPS "
            "SPC_MIINST_ISDEMOCARD SPC_CM_EXTREFCLOCK SPC_REP_STD_SEQUENCE "
            "SPC_TMASK_EXT0 SPC_TM_POS COUPLING_DC M2CMD_DATA_STARTDMA "
            "M2CMD_DATA_WAITDMA SPCSEQ_END SPCSEQ_ENDLOOPALWAYS "
            "M2CMD_CARD_ENABLETRIGGER M2CMD_CARD_WAITREADY M2CMD_DATA_STOPDMA "
            "SPCM_STOPLVL_ZERO SPC_REP_STD_SINGLE SPC_MEMSIZE"
        ).split()
        for index, name in enumerate(names, 10):
            setattr(self, name, index)
        self.SPCM_FEAT_SEQUENCE = 0x1000
        self.M2STAT_CARD_READY, self.M2STAT_CARD_TRIGGER = 4, 2
        self.clock = {
            "mode": self.SPC_CM_EXTREFCLOCK,
            "sample_rate": 1_250_000_000,
            "reference_clock": 125_000_000,
        }
        self.card = FakeCard(self)

    def Card(self, **kwargs):
        assert kwargs == {"serial_number": 101}
        return self.card

    def Clock(self, card):
        def method(name):
            def access(value=None):
                if value is not None:
                    self.log.append(("clock_write", name, value))
                    self.clock[name] = value
                return self.clock[name]

            return access

        return SimpleNamespace(**{name: method(name) for name in self.clock})

    def Channels(self, card, card_enable):
        card.channel_mask = card_enable
        return [FakeChannel(self, i) for i in range(2) if card_enable & (1 << i)]

    def Trigger(self, card):
        def record(name):
            def access(value):
                self.log.append(("trigger", name, value))
                return value + (getattr(self, "bad_trigger_readback", None) == name)

            return access

        return SimpleNamespace(
            **{
                name: record(name)
                for name in (
                    "or_mask",
                    "and_mask",
                    "ext0_mode",
                    "ext0_coupling",
                    "termination",
                    "ext0_level0",
                    "delay",
                )
            }
        )

    def Sequence(self, card):
        def record(name):
            def call(*args, **kwargs):
                self.log.append(("sequence", name, args, kwargs))
                if name == "max_segments":
                    card.steps.clear()
                    card.loaded.clear()
                elif name == "write_segment":
                    card.segment = args[0]
                elif name == "step_memory":
                    card.steps[args[0]] = kwargs["segment_index"]
                elif name == "start_step":
                    card.entry = args[0]

            return call

        return SimpleNamespace(
            **{
                name: record(name)
                for name in (
                    "max_segments",
                    "write_segment",
                    "segment_size",
                    "step_memory",
                    "start_step",
                )
            }
        )

    def DataTransfer(self, card):
        sdk = self

        class Transfer:
            def allocate_buffer(self, count):
                self.buffer = np.zeros(
                    (card.channel_mask.bit_count(), count), dtype=np.int16
                )

            def start_buffer_transfer(self, *flags):
                sdk.log.append(("dma", flags, self.buffer.copy()))
                card.loaded.add(card.segment)

        return Transfer()


class FakeCard:
    def __init__(self, sdk):
        self.sdk = sdk
        self.state = 4
        self.channel_mask = 0
        self.mode = sdk.SPC_REP_STD_SEQUENCE  # Unknown prior session; no valid table.
        self.entry = 0
        self.steps = {}
        self.loaded = set()

    def open(self):
        self.sdk.log.append(("open",))

    def sn(self):
        return 101

    def product_name(self):
        return "M4i.6631-x8"

    def get_i(self, key):
        sdk = self.sdk
        return {
            sdk.SPC_PCIMEMSIZE: 4 * 1024**3,
            sdk.SPC_PCIVERSION: 1,
            sdk.SPC_PCIMODULEVERSION: 2,
            sdk.SPC_SEQMODE_AVAILMAXSEGMENT: 65536,
            sdk.SPC_SEQMODE_AVAILMAXSTEPS: 4096,
            sdk.SPC_MIINST_ISDEMOCARD: 0,
        }[key]

    def features(self):
        return self.sdk.SPCM_FEAT_SEQUENCE

    def stop(self, *flags):
        self.state = 4
        self.sdk.log.append(("stop", flags))

    def card_mode(self, value):
        self.mode = value
        self.sdk.log.append(("card_mode", value))
        return value

    def set_i(self, reg, value):
        self.sdk.log.append(("set_i", reg, value))

    def write_setup(self):
        if self.mode == self.sdk.SPC_REP_STD_SEQUENCE:
            if (
                self.entry not in self.steps
                or self.steps[self.entry] not in self.loaded
            ):
                raise ValueError(f"Sequence check: start step ({self.entry}) not valid")
        self.sdk.log.append(("write_setup",))

    def timeout(self, value):
        self.sdk.log.append(("timeout", value))

    def start(self, *flags):
        self.state = 0
        self.sdk.log.append(("start", flags))

    def status(self):
        return self.state

    def cmd(self, flag):
        assert flag == self.sdk.M2CMD_CARD_WAITREADY
        self.state = 6

    def close(self):
        self.sdk.log.append(("close",))


class FakeChannel:
    def __init__(self, sdk, index):
        self.sdk, self.index = sdk, index

    def enable(self, value):
        self.sdk.log.append(("output", self.index, value))

    def amp(self, value):
        self.sdk.log.append(("range", self.index, value))
        return value

    def offset(self, value):
        self.sdk.log.append(("offset", self.index, value))
        return value

    def stop_level(self, value):
        self.sdk.log.append(("stop_level", self.index, value))
        return value


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
    original = adapter.clock_session_id
    sdk.clock["reference_clock"] = 10_000_000
    with pytest.raises(SpectrumRuntimeError, match="Clock setup changed"):
        adapter.configure(contract())
    assert not [item for item in sdk.log if item[0] == "clock_write"]
    adapter.configure_for_calibration(contract())
    assert adapter.clock_session_id != original
    assert sdk.clock["reference_clock"] == 125_000_000


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


def test_calibration_rejects_stale_identity_after_failed_clock_commit(monkeypatch):
    sdk = FakeSdk()
    adapter = SpcmAdapter("101", sdk=sdk, clock_session_id="old")

    def reject_commit():
        assert sdk.card.mode == sdk.SPC_REP_STD_SINGLE
        assert sdk.log[-2:] == [("output", 0, False), ("output", 1, False)]
        raise OSError("clock commit failed")

    monkeypatch.setattr(sdk.card, "write_setup", reject_commit)
    with pytest.raises(IOError, match="clock commit failed"):
        adapter.configure_for_calibration(contract())
    assert adapter.clock_session_id != "old"
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
