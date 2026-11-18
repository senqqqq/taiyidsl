"""Opt-in POWASF acceptance cases with explicit, unmeasured timing assumptions.

Run offline with ``uv run --locked python examples/powasf_sequence.py``.
Nothing in this example connects to hardware or executes an ARTIQ kernel.
"""

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
    delay,
    ns,
    play,
    reset_frame,
    rz,
    shift_phase,
)
from taiyidsl.artiq import verify_equivalence
from taiyidsl.polar_validation import verify_polar_output


def laboratory(channels=1, *, selector="urukul_rev8", shared_bus=False):
    """Separate IO_UPDATE and profile-selection latency declarations.

    112/100 mu are synthetic test inputs, not measurements of either board.
    Rev8/Rev9 selection cursor costs come from their actual driver paths.
    """
    qubits = [Qubit(f"q{i}") for i in range(channels)]
    devices = {
        f"dds{i}": Device(
            f"dds{i}",
            "dds",
            bus=f"spi{0 if shared_bus else i}",
            update_group=f"board{i}",
            nco_mode="continuous",
            nco_epoch_latency_mu=80,
            ram_destination="powasf",
            ram_latency_mu=112,
            ram_polar_phase_latency_mu=100,
            ram_profile_control=selector,
            ram_profile_latency_mu=112,
            ram_profile_phase_latency_mu=100,
        )
        for i in range(channels)
    }
    return qubits, LabConfig(
        DeviceSetup(devices, name="powasf-offline-unmeasured-assumptions"),
        SignalMap({q.raman: f"dds{i}" for i, q in enumerate(qubits)}),
    )


def pulse(duration_ns=800, *, sample_interval_ns=4):
    return FlatTopGaussian(
        0 * ns,
        sigma=duration_ns / 10 * ns,
        cutoff_sigma=5,
        sample_interval=sample_interval_ns * ns,
        frequency=83.123456e6,
        amplitude=0.5,
    )


def _experiment(name, qubits):
    return Experiment(
        name,
        qubits,
        metadata={
            "hardware_verified": False,
            "timing_calibration": "synthetic_unmeasured",
            "inserted_gap_mu": 0,
            "full_sequence_waveform_concatenation": False,
        },
    )


def build_sequence(
    *,
    count=4,
    duration_ns=800,
    phases=(0.0, math.pi / 2),
    channels=1,
    selector="urukul_rev8",
    terminal_rz=False,
    shared_bus=False,
    sample_interval_ns=4,
):
    qs, lab = laboratory(channels, selector=selector, shared_bus=shared_bus)
    exp = _experiment("powasf_sequence", qs)
    gate = pulse(duration_ns, sample_interval_ns=sample_interval_ns)
    with exp.average(1), exp.realtime(), exp.parallel():
        for q in qs:
            with exp.serial():
                for index in range(count):
                    play(q.raman, gate, phase=phases[index % len(phases)])
                if terminal_rz:
                    rz(q, math.pi / 2)
    return lab.compile(exp)


def build_standalone_rz(*, selector="urukul_rev8"):
    (q,), lab = laboratory(selector=selector)
    exp = _experiment("powasf_standalone_rz", [q])
    with exp.average(1), exp.realtime():
        rz(q, math.pi / 2)
    return lab.compile(exp)


def build_frame_sequence():
    """User-written idles separate independent hardware frame operations."""
    (q,), lab = laboratory()
    exp = _experiment("powasf_independent_frame_sequence", [q])
    with exp.average(1), exp.realtime():
        rz(q, -math.pi / 2)
        delay(q.raman, 800 * ns)
        shift_phase(q.frame, math.pi / 4)
        delay(q.raman, 800 * ns)
        reset_frame(q.frame)
        delay(q.raman, 800 * ns)
        rz(q, math.pi / 2)
    return lab.compile(exp)


def build_merged_rz():
    (q,), lab = laboratory()
    exp = _experiment("powasf_same_boundary_rz", [q])
    with exp.average(1), exp.realtime():
        play(q.raman, pulse())
        rz(q, -math.pi / 2)
        play(q.raman, pulse())
    return lab.compile(exp)


def build_short_parallel():
    (q0, q1), lab = laboratory(2)
    exp = _experiment("powasf_200ns_parallel_800ns", [q0, q1])
    with exp.average(1), exp.realtime(), exp.parallel():
        with exp.serial():
            delay(q0.raman, 400 * ns)
            play(q0.raman, pulse(200), phase=math.pi / 2)
        with exp.serial():
            play(q1.raman, pulse())
            play(q1.raman, pulse(), phase=math.pi / 2)
    return lab.compile(exp)


def acceptance_cases():
    return {
        "rev8_800_xy_1024": lambda: build_sequence(count=1024),
        "rev8_800_xyyx": lambda: build_sequence(
            phases=(0, math.pi / 2, math.pi / 2, 0), count=12
        ),
        "rev8_800_xy_two_boards": lambda: build_sequence(channels=2),
        "rev8_200_single": lambda: build_sequence(duration_ns=200, count=1),
        "rev9_200_single": lambda: build_sequence(
            duration_ns=200, count=1, selector="urukul_rev9"
        ),
        "rev9_800_same_axis": lambda: build_sequence(
            phases=(0,), count=8, selector="urukul_rev9"
        ),
        "rev8_800_terminal_rz": lambda: build_sequence(count=2, terminal_rz=True),
        "rev9_standalone_rz": lambda: build_standalone_rz(selector="urukul_rev9"),
        "rev8_independent_frames": build_frame_sequence,
        "rev8_same_boundary_rz": build_merged_rz,
        "rev8_200_parallel_800": build_short_parallel,
    }


def rejection_cases():
    return {
        "rev9_800_xy_selector_bandwidth": lambda: build_sequence(
            selector="urukul_rev9"
        ),
        "rev8_200_xy_selector_bandwidth": lambda: build_sequence(duration_ns=200),
        "rev8_shared_profile_bus": lambda: build_sequence(channels=2, shared_bus=True),
        "phase_shift_not_integral_samples": lambda: build_sequence(
            sample_interval_ns=20
        ),
    }


def run_suite(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    accepted, rejected = [], []
    for name, build in acceptance_cases().items():
        compiled = build()
        output_model = verify_polar_output(compiled)
        artifact = compiled.export_artiq(output / name)
        accepted.append(
            {
                "name": name,
                "duration_mu": compiled.points[0].duration_mu,
                "output_model": output_model,
                "source": str(artifact["source"]),
                "manifest": str(artifact["manifest"]),
                "export_verification": artifact["verification"],
                "equivalence": verify_equivalence(compiled)["status"],
                "real_driver_audit": "run_separately",
                "real_artiq_compile": "run_separately",
                "hardware_verified": False,
            }
        )
    for name, build in rejection_cases().items():
        try:
            build()
        except CompileError as error:
            rejected.append({"name": name, "reason": str(error)})
        else:
            raise AssertionError(f"Expected rejection: {name}")
    report = {
        "accepted": accepted,
        "rejected": rejected,
        "inserted_gap_mu": 0,
        "envelope_policy": "original_ASF_words_unchanged",
        "phase_model": "three_4ns_phase_prefix_samples_for_12ns_pipeline_difference",
        "calibration_status": "explicit_unmeasured_assumptions",
        "hardware_verified": False,
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "output/powasf"
    report = run_suite(destination)
    print(
        f"{len(report['accepted'])} offline accepted; "
        f"{len(report['rejected'])} correctly rejected; {destination}"
    )
