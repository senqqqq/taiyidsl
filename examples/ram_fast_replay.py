"""Same-envelope RAM replay with phase-only writes or IO_UPDATE alone.

All timings are illustrative contracts, not measured hardware minima. 125 MHz
is deliberately exactly representable at the example's 1 GHz DDS clock, so
identical gates on the 8 ns grid also have identical tracking-corrected POWs.
"""

import json
import math
from dataclasses import replace
from pathlib import Path

from taiyidsl import (
    Experiment,
    FlatTopGaussian,
    Qubit,
    delay,
    ns,
    play,
    reference_lab,
    us,
)
from taiyidsl.backends.artiq.codegen_ram import ram_bank_summary


def build_sequence(
    *, count=1024, alternating=True, gap_mu=None, frequency=125e6, phase_budget=480
):
    if gap_mu is None:
        gap_mu = phase_budget + 112 if alternating else 112
    q = Qubit("q0")
    pulse = FlatTopGaussian(4 * us, sigma=0.5 * us, amplitude=0.5)
    exp = Experiment(
        "ram_fast_xy" if alternating else "ram_fast_repeat",
        [q],
        metadata={
            "calibrated_rotations": False,
            "hardware_verified": False,
            "timing_contract": "Assumed phase write budget and 112 mu RAM latency",
            "gap_mu": gap_mu,
        },
    )
    with exp.average(1):
        with exp.realtime():
            for index in range(count):
                if index:
                    delay(q.raman, gap_mu * ns)
                play(
                    q.raman,
                    pulse,
                    frequency=frequency,
                    phase=math.pi / 2 if alternating and index % 2 else 0,
                )
    lab = reference_lab([q])
    name = "urukul0_ch0"
    lab.setup.devices[name] = replace(
        lab.setup.devices[name],
        nco_mode="legacy_tracking",  # Historical RF-off tracking path.
        ram_latency_mu=112,
        ram_rearm_duration_mu=4000,
        ram_phase_update_duration_mu=phase_budget,
    )
    return lab.compile(exp)


def main():
    output = Path(__file__).resolve().parents[1] / "output" / "ram_fast_replay"
    summaries = []
    for name, alternating in (("xy", True), ("repeat", False)):
        compiled = build_sequence(alternating=alternating)
        exported = compiled.export_artiq(output / name)
        debug = build_sequence(count=8, alternating=alternating)
        debug.export_artiq(output / f"{name}_unrolled", mode="unrolled")
        summaries.append(
            {
                "case": name,
                "banks": ram_bank_summary(compiled),
                "source": str(exported["source"]),
                "offline_export_check": exported["verification"][
                    "offline_export_check"
                ],
                "real_artiq_compile": "not_run_by_this_example",
                "hardware_verified": False,
            }
        )
    (output / "summary.json").write_text(
        json.dumps(summaries, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
