"""Offline capability matrix for 800 ns/200 ns gates with no inserted gaps.

The report includes rejected requests. A compile or mock pass validates only the
declared timing model; the 80/112 ns output latencies are not hardware calibration.
Equal RAM carrier/envelope latency is a labelled counterfactual diagnostic and
never produces an exported experiment in this example.
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
    Square,
    ns,
    play,
    reset_frame,
    rz,
    shift_phase,
)
from taiyidsl.artiq import verify_equivalence
from taiyidsl.waveforms import ad9910_amplitude_ram

DURATIONS_NS = (800, 200)
XY_FREQUENCY_HZ = 83.123456e6
REPEAT_FREQUENCY_HZ = XY_FREQUENCY_HZ
DRIVER_CURSOR_REFERENCE = {
    "spi_ref_period_mu": 8,
    "square_profile_write_mu": 156 * 8,
    "ram_pow_write_mu": 52 * 8,
    "square_declared_prepare_mu": 1280,
    "ram_declared_prepare_mu": 480,
    "meaning": (
        "ARTIQ driver cursor costs at the declared SPI clock; "
        "not measured output latency or sustained hardware throughput"
    ),
}


def ram_tail_summary(duration_ns, *, carrier_lead_ns=32):
    """Inspect the same quantized envelope used by the compiler, without padding."""
    parameters = {
        "sample_interval_s": 4e-9,
        "sigma_s": duration_ns * 1e-10,
        "cutoff_sigma": 5,
        "amplitude": 0.5,
    }
    sampled = ad9910_amplitude_ram(parameters, 1e9, duration_ns * 1e-9)
    samples = sampled["ram_asf"]
    last_nonzero = max(index for index, value in enumerate(samples) if value)
    zero_tail_ns = duration_ns - (last_nonzero + 1) * 4
    return {
        "duration_ns": duration_ns,
        "sigma_ns": duration_ns / 10,
        "cutoff_sigma": 5,
        "flat_duration_ns": 0,
        "sample_interval_ns": 4,
        "resident_words": len(samples),
        "unchanged_zero_tail_ns": zero_tail_ns,
        "declared_carrier_lead_ns": carrier_lead_ns,
        "zero_tail_covers_declared_skew": zero_tail_ns >= carrier_lead_ns,
    }


def build_sequence(
    shape,
    duration_ns,
    *,
    channels=1,
    phase_style="direct",
    pattern="xy",
    counterfactual_equal_latency=False,
):
    """Build X/Y/X or three identical gates on one or two independent boards."""
    if shape not in {"square", "ram"}:
        raise ValueError("shape must be square or ram")
    if duration_ns not in DURATIONS_NS or channels not in (1, 2):
        raise ValueError("Use 800 ns or 200 ns and one or two independent boards")
    if phase_style not in {"direct", "frame", "rz"} or pattern not in {"xy", "repeat"}:
        raise ValueError("Unknown phase style or gate pattern")
    if pattern == "repeat" and phase_style != "direct":
        raise ValueError("The exact-phase baseline uses repeated play without frames")
    if counterfactual_equal_latency and shape != "ram":
        raise ValueError("Equal-latency diagnostic applies only to RAM")
    qubits = [Qubit(f"q{i}") for i in range(channels)]
    devices, signals = {}, {}
    for index, q in enumerate(qubits):
        name = f"dds{index}"
        devices[name] = Device(
            name,
            "dds",
            bus=f"spi{index}",
            update_group=f"board{index}",
            square_latency_mu=80,
            square_prepare_duration_mu=1280,
            nco_mode="continuous",
            nco_epoch_latency_mu=80,
            ram_latency_mu=112,
            ram_pow_latency_mu=112 if counterfactual_equal_latency else 80,
            ram_rearm_duration_mu=4000,
            ram_phase_update_duration_mu=480,
            spi_ref_period_mu=8,
        )
        signals[q.raman] = name
    lab = LabConfig(
        DeviceSetup(devices, name="short-gates-offline-assumptions"),
        SignalMap(signals),
    )
    frequency = REPEAT_FREQUENCY_HZ if pattern == "repeat" else XY_FREQUENCY_HZ
    pulse = (
        Square(duration_ns * ns, amplitude=0.5, frequency=frequency)
        if shape == "square"
        else FlatTopGaussian(
            0 * ns,
            sigma=(duration_ns / 10) * ns,
            cutoff_sigma=5,
            sample_interval=4 * ns,
            amplitude=0.5,
            frequency=frequency,
        )
    )
    exp = Experiment(
        f"{shape}_{duration_ns}ns_{channels}board_{pattern}_{phase_style}"
        + ("_equal_latency_counterfactual" if counterfactual_equal_latency else ""),
        qubits,
        metadata={
            "hardware_verified": False,
            "inserted_gap_mu": 0,
            "timing_contract": "Illustrative assumptions, not hardware calibration",
            "counterfactual_equal_latency": counterfactual_equal_latency,
        },
    )
    phases = (0, math.pi / 2, 0) if pattern == "xy" else (0, 0, 0)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qubits:
                    with exp.serial():
                        for index, phase in enumerate(phases):
                            if phase_style == "frame" and index:
                                if index == 1:
                                    shift_phase(q.frame, math.pi / 2)
                                else:
                                    reset_frame(q.frame)
                            elif phase_style == "rz" and index:
                                # Positive Rz subtracts its angle from future drives.
                                rz(q, -math.pi / 2 if index == 1 else math.pi / 2)
                            play(
                                q.raman,
                                pulse,
                                phase=phase if phase_style == "direct" else 0,
                            )
    return lab.compile(exp)


def matrix_cases():
    for shape in ("square", "ram"):
        for duration_ns in DURATIONS_NS:
            for channels in (1, 2):
                for phase_style in ("direct", "frame", "rz"):
                    yield dict(
                        shape=shape,
                        duration_ns=duration_ns,
                        channels=channels,
                        phase_style=phase_style,
                        pattern="xy",
                        counterfactual_equal_latency=False,
                    )
                yield dict(
                    shape=shape,
                    duration_ns=duration_ns,
                    channels=channels,
                    phase_style="direct",
                    pattern="repeat",
                    counterfactual_equal_latency=False,
                )
    for duration_ns in DURATIONS_NS:
        for channels in (1, 2):
            for phase_style in ("direct", "frame"):
                yield dict(
                    shape="ram",
                    duration_ns=duration_ns,
                    channels=channels,
                    phase_style=phase_style,
                    pattern="xy",
                    counterfactual_equal_latency=True,
                )


def run_matrix(output_dir=None):
    """Return every acceptance/rejection; export only accepted declared cases."""
    output = Path(output_dir) if output_dir is not None else None
    rows = []
    for case in matrix_cases():
        row = {
            **case,
            "frequency_hz": REPEAT_FREQUENCY_HZ
            if case["pattern"] == "repeat"
            else XY_FREQUENCY_HZ,
            "requested_duration_mu": 3 * case["duration_ns"],
            "hardware_verified": False,
            "exported": False,
            "offline_export_check": "not_run",
            "real_artiq_compile": "not_run_by_this_example",
            "timing_contract_status": "counterfactual_NOT_physical_calibration"
            if case["counterfactual_equal_latency"]
            else "assumed_not_measured",
        }
        cursor = DRIVER_CURSOR_REFERENCE[
            "square_profile_write_mu"
            if case["shape"] == "square"
            else "ram_pow_write_mu"
        ]
        budget = DRIVER_CURSOR_REFERENCE[
            "square_declared_prepare_mu"
            if case["shape"] == "square"
            else "ram_declared_prepare_mu"
        ]
        row["preparation"] = {
            "current_backend_serializes_blocking_calls": False,
            "driver_cursor_mu_per_changed_channel": cursor,
            "declared_budget_mu_per_changed_channel": budget,
            "xy_boundary_serial_driver_cursor_mu": case["channels"] * cursor,
            "xy_boundary_serial_declared_budget_mu": case["channels"] * budget,
            "independent_bus_prepare_span_mu": budget,
            "gate_period_mu": case["duration_ns"],
            "note": (
                "Sums apply to a shared bus; independent buses overlap preparation. "
                "Continuous same-phase replay needs no per-boundary POW writes, "
                "including at non-integer-cycle carrier frequencies."
            ),
        }
        if case["shape"] == "ram":
            row["ram_envelope_latency_mu"] = 112
            row["ram_carrier_latency_mu"] = (
                112 if case["counterfactual_equal_latency"] else 80
            )
            row["envelope"] = ram_tail_summary(
                case["duration_ns"],
                carrier_lead_ns=112 - row["ram_carrier_latency_mu"],
            )
        try:
            compiled = build_sequence(**case)
        except CompileError as error:
            row.update(compile_status="rejected", reason=str(error))
        else:
            row.update(
                compile_status="accepted_under_declared_model",
                actual_duration_mu=compiled.points[0].duration_mu,
                equivalence_check=verify_equivalence(compiled)["status"],
            )
            if case["counterfactual_equal_latency"]:
                row["export_reason"] = "Counterfactual diagnostic; no artifact exported"
            elif output is not None:
                artifact = compiled.export_artiq(output / compiled.name)
                row.update(
                    exported=True,
                    source=str(artifact["source"]),
                    manifest=str(artifact["manifest"]),
                    offline_export_check=artifact["verification"][
                        "offline_export_check"
                    ],
                )
        rows.append(row)
    report = {
        "purpose": "800ns/200ns current capability matrix, including rejections",
        "hardware_verified": False,
        "inserted_gap_mu": 0,
        "driver_cursor_reference": DRIVER_CURSOR_REFERENCE,
        "counterfactual_warning": (
            "Lp=La isolates preparation limits from strict phase/envelope alignment. "
            "It is not a measured or recommended hardware timing contract."
        ),
        "cases": rows,
    }
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / "summary.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    return report


def main():
    output = Path(__file__).resolve().parents[1] / "output" / "short_gate_timing"
    report = run_matrix(output)
    for row in report["cases"]:
        diagnostic = " counterfactual" if row["counterfactual_equal_latency"] else ""
        print(
            f"{row['shape']:6} {row['duration_ns']:3}ns "
            f"{row['channels']}board {row['pattern']:6} {row['phase_style']:6}"
            f"{diagnostic}: {row['compile_status']}"
        )
        if row["compile_status"] == "rejected":
            print(f"  {row['reason']}")
    print(f"Report: {output / 'summary.json'}")
    print("Offline assumptions only; no hardware execution or calibration.")


if __name__ == "__main__":
    main()
