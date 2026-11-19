"""Offline continuous-NCO acceptance cases; never execute instrument kernels."""

import json
import math
from pathlib import Path

from taiyidsl import (
    CompileError,
    Device,
    DeviceSetup,
    Experiment,
    FlatTopGaussian,
    LabConfig,
    Qubit,
    SignalMap,
    Square,
    delay,
    ns,
    play,
    rz,
)
from taiyidsl.backends.artiq.codegen_ram import ram_bank_summary
from taiyidsl.backends.artiq.verification import verify_equivalence


def laboratory(channels=1, *, equal_latency=False, shared_bus=False):
    """Explicit synthetic timing; equal latencies are a counterfactual test."""
    qubits = [Qubit(f"q{i}") for i in range(channels)]
    devices = {
        f"dds{i}": Device(
            f"dds{i}",
            "dds",
            bus=f"spi{0 if shared_bus else i}",
            update_group=f"board{i}",
            nco_mode="continuous",
            nco_epoch_latency_mu=80,
            square_latency_mu=80,
            ram_latency_mu=112,
            ram_pow_latency_mu=112 if equal_latency else 80,
            ram_rearm_duration_mu=4000,
            ram_phase_update_duration_mu=480,
        )
        for i in range(channels)
    }
    return qubits, LabConfig(
        DeviceSetup(devices, name="continuous-nco-offline-assumptions"),
        SignalMap({q.raman: f"dds{i}" for i, q in enumerate(qubits)}),
    )


def pulse(shape, duration_ns):
    common = dict(frequency=83.123456e6, amplitude=0.5)
    if shape == "square":
        return Square(duration_ns * ns, **common)
    return FlatTopGaussian(
        0 * ns,
        sigma=duration_ns / 10 * ns,
        cutoff_sigma=5,
        sample_interval=4 * ns,
        **common,
    )


def build_sequence(
    shape="ram",
    *,
    duration_ns=800,
    count=3,
    channels=1,
    xy=False,
    terminal_rz=False,
    equal_latency=False,
    shared_bus=False,
):
    qs, lab = laboratory(channels, equal_latency=equal_latency, shared_bus=shared_bus)
    exp = Experiment(
        "continuous_sequence",
        qs,
        metadata={
            "hardware_verified": False,
            "inserted_gap_mu": 0,
            "counterfactual_equal_latency": equal_latency,
        },
    )
    gate = pulse(shape, duration_ns)
    with exp.average(1), exp.realtime(), exp.parallel():
        for q in qs:
            with exp.serial():
                for index in range(count):
                    play(q.raman, gate, phase=math.pi / 2 if xy and index % 2 else 0)
                if terminal_rz:
                    rz(q, math.pi / 2)
    return lab.compile(exp)


def build_standalone_rz():
    (q,), lab = laboratory()
    exp = Experiment("standalone_hardware_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
    return lab.compile(exp)


def build_mixed_short():
    """200 ns Square edges inside another bus's real terminal-Rz preparation."""
    (q0, q1), lab = laboratory(2)
    exp = Experiment("mixed_200ns_square_800ns_ram", [q0, q1])
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial():
            delay(q0.raman, 400 * ns)
            play(q0.raman, pulse("square", 200))
        with exp.serial():
            play(q1.raman, pulse("ram", 800))
            rz(q1, math.pi / 2)
    return lab.compile(exp)


def run_suite(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    cases = {
        "square_800_repeat": lambda: build_sequence("square"),
        "square_200_single": lambda: build_sequence("square", duration_ns=200, count=1),
        "ram_800_long": lambda: build_sequence(count=1024),
        "ram_200_single": lambda: build_sequence(duration_ns=200, count=1),
        "ram_800_two_boards": lambda: build_sequence(channels=2),
        "ram_800_terminal_rz": lambda: build_sequence(terminal_rz=True),
        "standalone_rz": build_standalone_rz,
        "mixed_200_800": build_mixed_short,
        "ram_800_xy_two_boards_SYNTHETIC_EQUAL_LATENCY": lambda: build_sequence(
            channels=2,
            xy=True,
            equal_latency=True,
        ),
    }
    accepted = []
    for name, build in cases.items():
        compiled = build()
        artifact = compiled.export_artiq(output / name)
        accepted.append(
            {
                "name": name,
                "duration_mu": compiled.points[0].duration_mu,
                "source": str(artifact["source"]),
                "manifest": str(artifact["manifest"]),
                "verification": artifact["verification"],
                "equivalence": verify_equivalence(compiled)["status"],
                "banks": ram_bank_summary(compiled),
                "counterfactual_equal_latency": "SYNTHETIC" in name,
                "hardware_verified": False,
            }
        )
    rejected = []
    for name, kwargs in (
        ("square_800_xy_bandwidth", dict(shape="square", xy=True)),
        ("ram_800_xy_strict_latency", dict(xy=True)),
        ("ram_200_xy_bandwidth", dict(xy=True, duration_ns=200, equal_latency=True)),
        (
            "ram_800_xy_shared_spi",
            dict(xy=True, channels=2, equal_latency=True, shared_bus=True),
        ),
    ):
        try:
            build_sequence(**kwargs)
        except CompileError as error:
            rejected.append({"name": name, "reason": str(error)})
        else:
            raise AssertionError(f"Expected rejection: {name}")
    report = {
        "backend_version": "9",
        "accepted": accepted,
        "rejected": rejected,
        "inserted_gap_mu": 0,
        "hardware_verified": False,
        "real_artiq_compile": "run_separately",
        "timing_contract": (
            "Explicit unmeasured assumptions; synthetic equality is not calibration"
        ),
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "output/continuous_nco"
    report = run_suite(destination)
    print(
        f"{len(report['accepted'])} accepted; "
        f"{len(report['rejected'])} rejected; {destination}"
    )
