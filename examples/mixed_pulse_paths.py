"""Cross-channel Square, ASF RAM and POWASF examples; no hardware is operated.

The timing declarations are unmeasured inputs. A 200 ns single gate is distinct
from a sequence that requests a different phase/profile every 200 ns.
"""

import json
import math
from dataclasses import replace
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
from taiyidsl.backends.artiq.verification import verify_equivalence


def laboratory(*, include_asf=False, selector="urukul_rev8", joint_frame=False):
    """Independent SPI/update groups on one core, using synthetic contracts."""
    q_polar, q_square, q_asf = (Qubit(name) for name in ("polar", "square", "asf"))
    shared = dict(
        nco_mode="continuous",
        nco_epoch_latency_mu=80,
        urukul_proto_rev=int(selector[-1]),
    )
    devices = {
        "polar_dds": Device(
            "polar_dds",
            "dds",
            bus="spi0",
            update_group="board0",
            ram_destination="powasf",
            ram_latency_mu=112,
            ram_polar_phase_latency_mu=100,
            ram_profile_control=selector,
            ram_profile_latency_mu=112,
            ram_profile_phase_latency_mu=100,
            **shared,
        ),
        "square_dds": Device(
            "square_dds",
            "dds",
            bus="spi1",
            update_group="board1",
            square_latency_mu=80,
            **shared,
        ),
    }
    mapping = {q_polar.raman: "polar_dds"}
    mapping[q_polar.rf if joint_frame else q_square.raman] = "square_dds"
    qubits = [q_polar] if joint_frame else [q_polar, q_square]
    if include_asf:
        devices["asf_dds"] = Device(
            "asf_dds",
            "dds",
            bus="spi2",
            update_group="board2",
            ram_latency_mu=112,
            ram_pow_latency_mu=80,
            ram_rearm_duration_mu=4000,
            ram_phase_update_duration_mu=480,
            **shared,
        )
        mapping[q_asf.raman] = "asf_dds"
        qubits.append(q_asf)
    return qubits, LabConfig(
        DeviceSetup(devices, name="mixed-paths-offline-unmeasured"),
        SignalMap(mapping),
    )


def shaped(duration_ns=800):
    return FlatTopGaussian(
        0 * ns,
        sigma=duration_ns / 8 * ns,
        cutoff_sigma=4,
        sample_interval=4 * ns,
        amplitude=0.5,
        frequency=83.123456e6,
    )


def build_mixed(
    *,
    duration_ns=800,
    count=4,
    include_asf=False,
    selector="urukul_rev8",
    polar_phases=(0.0, math.pi / 2),
    square_duration_ns=None,
    square_count=None,
    square_offset_ns=0,
    terminal_rz=False,
    square_phases=(0.0,),
    shared_bus=False,
    shared_update=False,
):
    qubits, lab = laboratory(include_asf=include_asf, selector=selector)
    q_polar, q_square = qubits[:2]
    if shared_bus:
        lab.setup.devices["square_dds"] = replace(
            lab.setup.devices["square_dds"], bus="spi0"
        )
    if shared_update:
        lab.setup.devices["square_dds"] = replace(
            lab.setup.devices["square_dds"], update_group="board0"
        )
    square_duration_ns = (
        duration_ns if square_duration_ns is None else square_duration_ns
    )
    square_count = count if square_count is None else square_count
    exp = Experiment(
        "mixed_pulse_paths",
        qubits,
        metadata={
            "hardware_verified": False,
            "timing_contracts": "unmeasured",
            "inserted_gap_mu": 0,
            "whole_sequence_ram_concatenation": False,
        },
    )
    with exp.average(2), exp.realtime(), exp.parallel():
        with exp.serial():
            for index in range(count):
                play(
                    q_polar.raman,
                    shaped(duration_ns),
                    phase=polar_phases[index % len(polar_phases)],
                )
            if terminal_rz:
                rz(q_polar, math.pi / 3)
        with exp.serial():
            if square_offset_ns:
                delay(q_square.raman, square_offset_ns * ns)
            for index in range(square_count):
                play(
                    q_square.raman,
                    Square(
                        square_duration_ns * ns, frequency=83.123456e6, amplitude=0.4
                    ),
                    phase=square_phases[index % len(square_phases)],
                )
            if terminal_rz:
                rz(q_square, -math.pi / 4)
        if include_asf:
            with exp.serial():
                for index in range(count):
                    play(qubits[2].raman, shaped(duration_ns))
                if terminal_rz:
                    rz(qubits[2], math.pi / 5)
    return lab.compile(exp)


def build_joint_frame(*, standalone=False):
    (q,), lab = laboratory(joint_frame=True)
    exp = Experiment("mixed_joint_hardware_rz", [q])
    with exp.average(1), exp.realtime():
        if not standalone:
            with exp.parallel():
                with exp.serial():
                    for _ in range(3):
                        play(q.raman, shaped())
                with exp.serial():
                    for _ in range(3):
                        play(q.rf, Square(800 * ns, frequency=83.123456e6))
        rz(q, math.pi / 2)
    return lab.compile(exp)


def acceptance_cases():
    return {
        "mixed_800_xy_square": build_mixed,
        "mixed_200_single": lambda: build_mixed(duration_ns=200, count=1),
        "mixed_200_offset_in_800_xy": lambda: build_mixed(
            count=2, square_duration_ns=200, square_count=1, square_offset_ns=400
        ),
        "mixed_three_paths_800": lambda: build_mixed(include_asf=True),
        "mixed_three_paths_200": lambda: build_mixed(
            include_asf=True, duration_ns=200, count=1
        ),
        "mixed_rev9_200_same_profile": lambda: build_mixed(
            duration_ns=200,
            count=4,
            polar_phases=(0.0,),
            selector="urukul_rev9",
            square_duration_ns=800,
            square_count=1,
        ),
        "mixed_three_paths_terminal_rz": lambda: build_mixed(
            count=3, include_asf=True, terminal_rz=True
        ),
        "mixed_joint_terminal_rz": build_joint_frame,
        "mixed_joint_standalone_rz": lambda: build_joint_frame(standalone=True),
    }


def rejection_cases():
    return {
        "rev8_200_xy_bandwidth": lambda: build_mixed(duration_ns=200),
        "rev9_200_xy_bandwidth": lambda: build_mixed(
            duration_ns=200, selector="urukul_rev9"
        ),
        "rev9_800_xy_bandwidth": lambda: build_mixed(selector="urukul_rev9"),
        "native_square_800_xy_bandwidth": lambda: build_mixed(
            square_phases=(0.0, math.pi / 2)
        ),
        "shared_rev8_profile_pins": lambda: build_mixed(shared_bus=True),
        "shared_io_update": lambda: build_mixed(shared_update=True),
    }


def run_suite(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    accepted, rejected = [], []
    for name, build in acceptance_cases().items():
        compiled = build()
        artifact = compiled.export_artiq(output / name)
        accepted.append(
            {
                "name": name,
                "duration_mu": compiled.points[0].duration_mu,
                "verification": artifact["verification"],
                "equivalence": verify_equivalence(compiled)["status"],
                "source": str(artifact["source"]),
                "hardware_verified": False,
            }
        )
    for name, build in rejection_cases().items():
        try:
            build()
        except CompileError as error:
            rejected.append({"name": name, "reason": str(error)})
        else:
            raise AssertionError(f"Expected exact rejection: {name}")
    report = {
        "accepted": accepted,
        "rejected": rejected,
        "timing_calibration": "explicit_unmeasured_assumptions",
        "inserted_gap_mu": 0,
        "hardware_verified": False,
        "asf_phase_alignment": (
            "112/80 mu remain unequal; examples use same-axis replay or RF-off Rz"
        ),
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "output/mixed_pulse_paths"
    report = run_suite(destination)
    print(
        f"{len(report['accepted'])} offline accepted; "
        f"{len(report['rejected'])} correctly rejected; {destination}"
    )
