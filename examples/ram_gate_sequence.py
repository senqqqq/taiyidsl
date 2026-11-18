"""A long shot reusing two resident amplitude envelopes.

The X/Y/X2/Y2 labels express intended rotations. These RF amplitudes are
illustrative, not calibrated pi/pi2 gates or a physical dynamics simulation.
The repeat motif includes an explicit low gap after every gate, including the
last gate. Its RAM capacity stays at 802 words regardless of the repeat count.

Run with the DSL environment: uv run --locked python examples/ram_gate_sequence.py
Then use examples/artiq_offline_compile.py in the independent ARTIQ environment.
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


def build_sequence(*, cycles=256, shots=1, gap=4112 * ns):
    q = Qubit("q0")
    pulse = FlatTopGaussian(4 * us, sigma=0.5 * us, amplitude=0.5)
    exp = Experiment(
        "ram_gate_sequence",
        [q],
        metadata={
            "description": "Repeated X/Y/X2/Y2 labels with resident RAM envelopes",
            "calibrated_rotations": False,
            "timing_contract": "Illustrative 4000 mu rearm + 112 mu matched pipeline",
            "trailing_gap": True,
        },
    )
    with exp.average(shots):
        with exp.realtime():
            with exp.repeat(cycles):
                for amplitude, phase in (
                    (0.5, 0.0),  # X label
                    (0.5, math.pi / 2),  # Y label: same RAM profile, new POW
                    (0.25, 0.0),  # X/2 label: separate scaled waveform
                    (0.25, math.pi / 2),  # Y/2 label: reuse the second profile
                ):
                    play(q.raman, pulse, amplitude=amplitude, phase=phase)
                    delay(q.raman, gap)
    lab = reference_lab([q])
    name = "urukul0_ch0"
    lab.setup.devices[name] = replace(
        lab.setup.devices[name],
        nco_mode="legacy_tracking",  # Historical RF-off rearm example.
        ram_latency_mu=112,
        ram_rearm_duration_mu=4000,
    )
    return lab.compile(exp)


def main():
    compiled = build_sequence()
    output = Path(__file__).resolve().parents[1] / "output" / "ram_gate_sequence"
    exported = compiled.export_artiq(output / "artiq")
    launches = [
        event
        for event in compiled.points[0].physical_events
        if event.kind == "dds_ram_start"
    ]
    profiles = {event.params["ram_profile"]: event.params for event in launches}
    summary = {
        "gates_per_shot": len(launches),
        "resident_profiles": len(profiles),
        "resident_words": sum(len(p["ram_asf"]) for p in profiles.values()),
        "gate_gap_mu": 4112,
        "rearm_budget_mu": 4000,
        "ram_latency_mu": 112,
        "tick_seconds": float(compiled.setup.tick_seconds),
        "trailing_gap": True,
        "calibrated_rotations": False,
        "hardware_verified": False,
        "source": str(exported["source"]),
        "offline_export_check": exported["verification"]["offline_export_check"],
        "real_artiq_compile": "not_run_by_this_example",
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
