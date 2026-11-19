"""POWASF generated programs exercise uploaded words and actual selector costs."""

import json
import math

import pytest

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    ns,
    play,
    rz,
)
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_equivalence, verify_export


def build_polar(
    *,
    count=3,
    duration=800,
    channels=1,
    revision=8,
    terminal=False,
    standalone=False,
    same_axis=False,
    ttl=False,
):
    qubits = [Qubit(f"q{i}") for i in range(channels)]
    devices, signals = {}, {}
    for i, q in enumerate(qubits):
        name = f"dds{i}"
        devices[name] = Device(
            name,
            "dds",
            bus=f"spi{i}",
            update_group=f"board{i}",
            nco_mode="continuous",
            nco_epoch_latency_mu=80,
            ram_destination="powasf",
            ram_latency_mu=112,
            ram_polar_phase_latency_mu=100,
            ram_profile_control=f"urukul_rev{revision}",
            ram_profile_latency_mu=112,
            ram_profile_phase_latency_mu=100,
        )
        signals[q.raman] = name
    if ttl:
        devices["ttl"] = Device("ttl", "ttl")
        signals[qubits[0].readout] = "ttl"
    pulse = FlatTopGaussian(
        0 * ns,
        sigma=(duration / 8) * ns,
        cutoff_sigma=4,
        sample_interval=4 * ns,
        amplitude=0.5,
        frequency=83.123456e6,
    )
    experiment = Experiment("polar_export", qubits)
    with experiment.average(2):
        with experiment.realtime():
            with experiment.parallel():
                for q in qubits:
                    with experiment.serial():
                        if not standalone:
                            for i in range(count):
                                play(
                                    q.raman,
                                    pulse,
                                    phase=0 if same_axis else (i % 2) * math.pi / 2,
                                )
                        if terminal or standalone:
                            rz(q, math.pi / 3)
                if ttl:
                    play(qubits[0].readout, Square(200 * ns))
    return LabConfig(DeviceSetup(devices), SignalMap(signals)).compile(experiment)


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("same_axis", [False, True])
def test_polar_table_and_unrolled_have_equal_executed_effects(channels, same_axis):
    compiled = build_polar(channels=channels, same_axis=same_axis, ttl=True)
    result = verify_equivalence(compiled)
    assert result["status"] == "passed"
    assert result["auto"]["ram_execution"] == "polar_event_table"
    assert result["hardware"] == "not_verified"
    source = generate_source(compiled)
    assert "RAM_DEST_POWASF" in source
    assert ".set_pow(" not in source
    assert "phase_autoclear=1" not in source


@pytest.mark.parametrize("revision", [8, 9])
def test_200ns_single_gate_and_parallel_ttl(revision):
    compiled = build_polar(count=1, duration=200, revision=revision, ttl=True)
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize("standalone", [False, True])
def test_independent_and_terminal_rz_have_real_hardware_execution(standalone):
    compiled = build_polar(standalone=standalone, terminal=True)
    frames = [
        e
        for e in compiled.points[0].physical_events
        if e.kind == "dds_polar_effect" and e.params["polar_frame_only"]
    ]
    assert len(frames) == 1
    assert verify_equivalence(compiled)["status"] == "passed"


@pytest.mark.parametrize(
    "mutation",
    [
        "autoclear",
        "held_clear",
        "mode",
        "bank",
        "missing_upload",
        "timestamp",
        "profile",
        "revision",
        "bus_alias",
    ],
)
def test_polar_mock_rejects_corrupted_export(mutation):
    compiled = build_polar(channels=2)
    source = generate_source(compiled, mode="unrolled")
    if mutation == "autoclear":
        source = source.replace("phase_autoclear=0", "phase_autoclear=1")
    elif mutation == "held_clear":
        source = source.replace(
            "phase_clear=0, phase_autoclear=0", "phase_clear=1, phase_autoclear=0"
        )
    elif mutation == "mode":
        source = source.replace("ram_destination=RAM_DEST_POWASF", "ram_destination=2")
    elif mutation in {"bank", "missing_upload"}:
        effect = next(
            e
            for e in compiled.points[0].physical_events
            if e.kind == "dds_polar_effect"
        )
        bank = effect.params["polar_bank"][1]
        name = f"bank_0_device_0_{bank['profile']}"
        if mutation == "missing_upload":
            source = source.replace(
                f"self.device_0.write_ram([int32(x) for x in self.{name}])", "pass"
            )
        else:
            # Corrupt a single packed phase word, retaining every amplitude bit.
            old = [w if w < 2**31 else w - 2**32 for w in bank["words"]]
            new = list(old)
            new[-1] ^= 1 << 16
            source = source.replace(
                f"self.{name} = array({old!r}", f"self.{name} = array({new!r}"
            )
    elif mutation == "timestamp":
        select = next(
            e
            for e in compiled.points[0].physical_events
            if e.kind == "dds_polar_select"
        )
        source = source.replace(
            f"at_mu(t0 + {select.start_mu})", f"at_mu(t0 + {select.start_mu + 8})", 1
        )
    elif mutation == "profile":
        # Last match is a timed selector, after the same profile's preload.
        head, tail = source.rsplit(
            "self.device_0.cpld.set_profile(self.device_0.chip_select - 4, 1)", 1
        )
        source = (
            head
            + "self.device_0.cpld.set_profile(self.device_0.chip_select - 4, 0)"
            + tail
        )
    elif mutation == "revision":
        source = source.replace(
            "self.result_adapter = None",
            "self.result_adapter = None\n        self.device_0.cpld.proto_rev = 9",
            1,
        )
    else:
        source = source.replace(
            "self.result_adapter = None",
            "self.result_adapter = None\n"
            "        self.device_1.bus.channel = self.device_0.bus.channel",
            1,
        )
    with pytest.raises(ValueError):
        verify_export(compiled, source=source)


def test_artifact_manifest_and_long_reuse(tmp_path):
    short = build_polar(count=4)
    long = build_polar(count=128)
    paths = long.export_artiq(tmp_path)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["ram_banks"][0]["plays"] == 128
    assert manifest["ram_banks"][0]["destination"] == "powasf"
    assert manifest["verification"]["hardware"] == "not_verified"
    assert len(generate_source(short).splitlines()) == len(
        generate_source(long).splitlines()
    )
    assert verify_equivalence(long)["status"] == "passed"
