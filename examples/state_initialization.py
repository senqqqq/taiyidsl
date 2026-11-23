"""Offline Yb-171 optical-pumping scan using the existing DSL, not a new API.

Run: uv run python examples/state_initialization.py
All numbers below are illustrative electronics/timing values, NOT a calibrated
atomic preparation or readout recipe. No devices are accessed.

Assumed physics: sigma+ pumping via 3P1 F'=1/2 prepares 1S0 mF=+1/2.
Readout example: blow away mF=+1/2 via 3P1 F'=3/2, then image survivors.
Reference: https://doi.org/10.1103/PhysRevX.12.021027 (Appendix D).

The host result_adapter.before_shot must supply freshly loaded/cooled atoms,
the required stable bias field/trap, and the optical paths in the correct state
on EVERY shot. after_shot retrieves the image and loading/status information.
These integrations are deliberately not implemented by this offline example.
If the delay from RSC to pumping must be deterministic, put RSC into the same
realtime sequence instead of relying on a host boundary.

The lab profile's image protocol only emits a camera trigger. Illumination, camera
exposure, field/trap changes for readout and readout calibration need a real lab
protocol. DDS frequencies are AOM RF frequencies, not optical frequencies; the
two paths must already address their respective hyperfine transitions.
"""

from __future__ import annotations

from pathlib import Path

from taiyidsl import (
    Experiment,
    LabConfig,
    OpticsConfig,
    Qubit,
    QubitParameters,
    SweepParameter,
)
from taiyidsl.experiments.cooling import optical_pump, trigger_readout


def build_initialization_scan(
    qubit,
    parameters,
    *,
    durations_us=(0, 10, 20, 50, 100, 200),
    shots=100,
    averaging="cyclic",
):
    """Build a shot sequence from shared optical parameters, independently of lab."""
    metadata = {
        "readout_modes": {"0": "no_pushout_reference", "1": "pushout_target_state"},
        "host_precondition": (
            "Fresh cooled atoms, stable trap/bias field for EVERY shot"
        ),
        "physical_calibration": "not_verified",
    }
    exp = Experiment(
        "yb171_initialization",
        qubits=[qubit],
        metadata=metadata,
        parameters=OpticsConfig.coerce(parameters),
    )
    q = exp.qubits[qubit.uid]
    p = exp.parameters
    pump_time = SweepParameter("pump_time", durations_us, unit="us")
    readout_mode = SweepParameter("readout_mode", [0, 1], unit="dimensionless")

    with exp.average(shots=shots, mode=averaging):
        with exp.sweep(pump_time) as duration:
            with exp.sweep(readout_mode):
                with exp.realtime():
                    optical_pump(exp, p["pump"], duration=duration)
                    for mode in (0, 1):
                        with exp.case(readout_mode, mode):
                            trigger_readout(exp, q, p, pushout=bool(mode))
    return exp


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "output" / "state_initialization"
    q0 = Qubit(
        "q0",
        QubitParameters(
            extensions={
                "initialization": {
                    "target_mF": "+1/2",
                    "manifold": "1S0",
                    "recipe": "sigma_plus_Fprime_half",
                }
            }
        ),
    )
    lab = LabConfig.load(
        root / "examples/config/labs/initialization.json", qubit_id=q0.uid
    )
    parameters = OpticsConfig.load(root / "examples/config/optics/initialization.json")
    exp = build_initialization_scan(q0, parameters)
    compiled = lab.compile(exp)
    compiled.save(output / "compiled.json")
    compiled.visualize(output / "timeline.html")
    compiled.export_artiq(output / "artiq")
    print(f"{len(compiled.points)} points; output: {output}")


if __name__ == "__main__":
    main()
