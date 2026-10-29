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

The image protocol below only emits a camera trigger. Illumination, camera
exposure, field/trap changes for readout and readout calibration need a real lab
protocol. DDS frequencies are AOM RF frequencies, not optical frequencies; the
two paths must already address their respective hyperfine transitions.
"""

from __future__ import annotations

import json
from pathlib import Path

from taiyidsl import (
    Device,
    DeviceSetup,
    Experiment,
    Protocol,
    ProtocolStep,
    Qubit,
    QubitParameters,
    SignalMap,
    Square,
    SweepParameter,
    compile_experiment,
    delay,
    measure,
    play,
    us,
)
from taiyidsl.artiq import verify_equivalence


def build_initialization_scan(qubit, config, *, durations_us=(0, 10, 20, 50, 100, 200)):
    exp = Experiment(
        "yb171_initialization",
        qubits=[qubit],
        metadata={
            "optics": config,
            "readout_modes": {"0": "no_pushout_reference", "1": "pushout_target_state"},
            "host_precondition": (
                "Fresh cooled atoms, stable trap/bias field for EVERY shot"
            ),
            "physical_calibration": "not_verified",
        },
    )
    q = exp.qubits[qubit.uid]
    cfg = exp.metadata["optics"]  # Use the copied experiment configuration.
    pump_time = SweepParameter("pump_time", durations_us, unit="us")
    readout_mode = SweepParameter("readout_mode", [0, 1], unit="dimensionless")

    with exp.average(shots=100, mode="cyclic"):
        with exp.sweep(pump_time):
            with exp.sweep(readout_mode):
                with exp.realtime():
                    delay("optics.pump_556", cfg["initial_idle_us"] * us)
                    play(
                        "optics.pump_556",
                        Square(
                            duration=pump_time,
                            frequency=cfg["pump_dds_hz"],
                            amplitude=cfg["pump_amplitude"],
                        ),
                    )
                    delay("optics.pump_556", cfg["post_pump_wait_us"] * us)

                    # Both modes have the same readout window duration.
                    # case is STATIC sweep-point selection, not measurement feedback.
                    with exp.case(readout_mode, 0):
                        delay("optics.pushout_556", cfg["pushout_us"] * us)
                    with exp.case(readout_mode, 1):
                        play(
                            "optics.pushout_556",
                            Square(
                                duration=cfg["pushout_us"] * us,
                                frequency=cfg["pushout_dds_hz"],
                                amplitude=cfg["pushout_amplitude"],
                            ),
                        )
                    measure(q, protocol="image", handle="survivors")
                    delay(q.readout, cfg["camera_wait_us"] * us)
    return exp


def main():
    output = Path(__file__).resolve().parents[1] / "output" / "state_initialization"
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
    # Placeholder parameters for OFFLINE compiler demonstration only.
    config = {
        "pump_dds_hz": 80_000_000,
        "pump_amplitude": 0.1,
        "pushout_dds_hz": 90_000_000,
        "pushout_amplitude": 0.2,
        "initial_idle_us": 20,
        "post_pump_wait_us": 20,
        "pushout_us": 100,
        "camera_wait_us": 1000,
        "polarization": "sigma+ relative to bias field",
        "bias_field": "LAB_CALIBRATION_REQUIRED",
        "optical_frequency_mapping": "LAB_CALIBRATION_REQUIRED",
    }
    setup = DeviceSetup(
        name="initialization-offline-only",
        devices={
            "urukul0_ch0": Device(
                "urukul0_ch0", "dds", bus="spi0", update_group="urukul0"
            ),
            "urukul0_ch1": Device(
                "urukul0_ch1", "dds", bus="spi0", update_group="urukul0"
            ),
            "ttl0": Device("ttl0", "ttl", grid_mu=1, min_duration_mu=1),
        },
        protocols={
            "image": Protocol(
                (ProtocolStep("{qubit}.readout", 10 * us),),
                "Camera trigger only; illumination/data transfer require lab adapters",
            ),
        },
    )
    mapping = SignalMap(
        {
            "optics.pump_556": "urukul0_ch0",
            "optics.pushout_556": "urukul0_ch1",
            "q0.readout": "ttl0",
        }
    )
    exp = build_initialization_scan(q0, config)
    compiled = compile_experiment(exp, setup, mapping)
    compiled.save(output / "compiled.json")
    compiled.visualize(output / "timeline.html")
    compiled.export_artiq(output / "artiq")
    report = verify_equivalence(compiled)
    (output / "verification.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        f"{len(compiled.points)} points; "
        f"{report['auto']['kernel_count']} shared kernel(s); "
        f"offline export equivalence: {report['status']}; output: {output}"
    )


if __name__ == "__main__":
    main()
