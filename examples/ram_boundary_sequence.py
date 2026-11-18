"""Continuous-NCO resident RAM replay without added gaps or envelope changes.

The default is a same-axis 800 ns train at a non-integer-cycle carrier. Strict
X/Y playback is rejected with unequal illustrative POW/envelope latencies.
Equal latencies can be requested only as an explicit offline counterfactual.
"""

import json
import math
from dataclasses import replace
from pathlib import Path

from taiyidsl import (
    CompileError,
    Experiment,
    FlatTopGaussian,
    Qubit,
    ns,
    play,
    reference_lab,
    reset_frame,
    shift_phase,
)
from taiyidsl.artiq_ram import ram_bank_summary


def build_sequence(
    *,
    count=1024,
    frequency=83e6,
    channels=1,
    terminal_frame=False,
    duration_ns=800,
    phase_pattern="repeat",
    counterfactual_equal_latency=False,
):
    qubits = [Qubit(f"q{i}") for i in range(channels)]
    # 800 ns uses 201 words; 200 ns uses 51. No plateau or added zero samples.
    pulse = FlatTopGaussian(
        0 * ns,
        sigma=(duration_ns / 10) * ns,
        cutoff_sigma=5,
        sample_interval=4 * ns,
        amplitude=0.5,
        frequency=frequency,
    )
    exp = Experiment(
        f"ram_continuous_{phase_pattern}",
        qubits,
        metadata={
            "calibrated_rotations": False,
            "hardware_verified": False,
            "timing_contract": (
                "Assumed RAM 112 mu/POW 80 mu delays and 480 mu POW-write budget; "
                "strict phase boundaries, continuous NCO, no zero-tail exception"
            ),
            "inserted_gap_mu": 0,
            "independent_boards": channels,
            "gate_duration_ns": duration_ns,
            "counterfactual_equal_latency": counterfactual_equal_latency,
        },
    )
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qubits:
                    with exp.serial():
                        for index in range(count):
                            if index and phase_pattern == "xy":
                                if index % 2:
                                    shift_phase(q.frame, math.pi / 2)
                                else:
                                    reset_frame(q.frame)
                            play(q.raman, pulse)
                        if terminal_frame:
                            reset_frame(q.frame)
    lab = reference_lab(qubits)
    for index in range(channels):
        # This experiment has one drive per qubit. Do not create an unused RF
        # phase target on the same exclusive RAM IO_UPDATE group.
        del lab.signal_map[qubits[index].rf]
        name = f"urukul{index}_ch0"
        lab.setup.devices[name] = replace(
            lab.setup.devices[name],
            ram_latency_mu=112,
            ram_pow_latency_mu=112 if counterfactual_equal_latency else 80,
            ram_rearm_duration_mu=4000,
            ram_phase_update_duration_mu=480,
        )
    return lab.compile(exp)


def main():
    output = Path(__file__).resolve().parents[1] / "output" / "ram_boundary_sequence"
    compiled = build_sequence()
    exported = compiled.export_artiq(output / "repeat")
    build_sequence(count=8).export_artiq(output / "repeat_unrolled", mode="unrolled")
    requests = []
    for duration_ns, channels in ((800, 1), (200, 1), (800, 2)):
        request = {
            "gate_duration_ns": duration_ns,
            "channels": channels,
            "hardware_verified": False,
        }
        try:
            candidate = build_sequence(
                count=8, duration_ns=duration_ns, channels=channels, phase_pattern="xy"
            )
        except CompileError as error:
            request.update(compile_status="rejected", reason=str(error))
        else:
            artifact = candidate.export_artiq(
                output / f"xy_{duration_ns}ns_{channels}q"
            )
            request.update(
                compile_status="accepted_under_declared_model",
                source=str(artifact["source"]),
                offline_export_check=artifact["verification"]["offline_export_check"],
            )
        requests.append(request)
    summary = {
        "banks": ram_bank_summary(compiled),
        "duration_mu": compiled.points[0].duration_mu,
        "inserted_gap_mu": 0,
        "source": str(exported["source"]),
        "offline_export_check": exported["verification"]["offline_export_check"],
        "real_artiq_compile": "not_run_by_this_example",
        "hardware_verified": False,
        "gate_duration_ns": 800,
        "additional_requests": requests,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
