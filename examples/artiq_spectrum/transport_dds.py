"""Offline three-card RF / clock / AOD round trip using native Spectrum DDS.

All serials, coordinates, calibration tables and timing are illustrative. This
example never connects to instruments. C keeps nonzero trap tones throughout
the control sequence; both atoms return home before the next host shot.
"""

import argparse
import json
from pathlib import Path

from taiyidsl import (
    IZ,
    DeviceSetup,
    Experiment,
    LabConfig,
    MoveProfile,
    Position,
    Qubit,
    Square,
    TransportConfig,
    kHz,
    move,
    play,
    um,
    us,
)


def build_lab():
    """Build a fully declared offline fixture, without opening SDK devices."""
    q0, q1 = (
        Qubit(
            uid,
            signals={"rf": f"{uid}.rf", "clock": f"{uid}.clock"},
            parameters={
                "rf": {"frequency": 30 * kHz, "frame": "spin"},
                "clock": {"frequency": 200 * kHz, "frame": "clock_control"},
            },
            position=Position(x * um, 0, 0, "tweezer_plane"),
        )
        for uid, x in (("q0", 0), ("q1", 6))
    )
    iz = IZ(
        "iz0",
        sites=(
            Position(30 * um, 10 * um, 0, "tweezer_plane"),
            Position(36 * um, 10 * um, 0, "tweezer_plane"),
        ),
    )
    device_db = {
        "core": {
            "type": "local",
            "module": "artiq.coredevice.core",
            "class": "Core",
            "arguments": {"host": None, "ref_period": 1e-9},
        },
        **{
            f"trigger_{name.lower()}": {
                "type": "local",
                "module": "artiq.coredevice.ttl",
                "class": "TTLOut",
                "arguments": {"channel": channel},
            }
            for name, channel in (("A", 1), ("B", 2), ("C", 3))
        },
    }
    spec_db = {
        "A": {"serial_number": "SIM_A", "mode": "awg"},
        "B": {"serial_number": "SIM_B", "mode": "awg"},
        "C": {
            "serial_number": "SIM_C",
            "mode": "dds",
            "dds_routing": "16/5",
            # Illustrative declaration, never a measured synchronization claim.
            "dds_trigger_latency_ns": 560.0,
        },
    }
    descriptor = {
        "name": "three_card_transport_offline",
        "backend": "artiq-spectrum",
        "instruments": {
            "artiq": {"kind": "artiq", "device": "core"},
            **{name: {"kind": "spectrum", "device": name} for name in ("A", "B", "C")},
        },
        "channels": {
            "nuclear_rf": {"instrument": "A", "channel": 0, "sharing": "global"},
            "clock": {
                "instrument": "B",
                "channel": 0,
                "coverage": {"zones": ["iz0"]},
            },
            "aod_x": {"instrument": "C", "channel": 0},
            "aod_y": {"instrument": "C", "channel": 1},
        },
        "signals": {
            uid: {"rf": "nuclear_rf", "clock": "clock"} for uid in ("q0", "q1")
        },
        "sync_groups": {
            "qubit_control": {
                "cards": ["A", "B", "C"],
                "triggers": {
                    name: f"trigger_{name.lower()}" for name in ("A", "B", "C")
                },
                # 1200 - 560 = 640 ns = 100 DDS timing ticks of pre-roll.
                "timing": {"trigger_lead_mu": 1200},
            },
        },
    }
    setup = DeviceSetup.from_descriptor(
        descriptor, device_db=device_db, spec_db=spec_db
    )
    transport = TransportConfig(
        card="C",
        x_calibration=((0, 70e6), (40e-6, 90e6)),
        y_calibration=((0, 70e6), (15e-6, 85e6)),
        bindings={
            "q0": {"x_core": 0, "y_core": 8},
            "q1": {"x_core": 1, "y_core": 8},
        },
        initial_amplitudes={0: 0.08, 1: 0.08, 8: 0.1},
        zones=(iz,),
        position_tolerance=1e-9,
        frequency_tolerance_hz=1.0,
        min_separation=1e-6,
    )
    return LabConfig(setup, qubits=(q0, q1), transport=transport), q0, q1, iz


def transport_experiment(
    q0,
    q1,
    iz,
    *,
    rf_pulse,
    clock_pulse,
    transport_profile,
    duration=100 * us,
    settle=10.24 * us,
    shots=2,
):
    """Global RF -> coordinated move -> zone clock -> return home -> global RF."""
    exp = Experiment(
        "native_dds_transport",
        [q0, q1],
        metadata={
            "offline_only": True,
            "hardware_verified": False,
            "transport_layout": "planned_not_measured",
        },
    )
    with (
        exp.average(shots),
        exp.realtime(),
        exp.triggered_sequence(group="qubit_control"),
    ):
        play(q0.rf, rf_pulse)
        move(
            (q0, q1),
            to=(iz[0], iz[1]),
            duration=duration,
            profile=transport_profile,
            settle=settle,
        )
        play(q0.clock, clock_pulse)
        move(
            (q0, q1),
            to=(q0.position, q1.position),
            duration=duration,
            profile=transport_profile,
            settle=settle,
        )
        play(q0.rf, rf_pulse)
    return exp


def build_demo():
    """Return a laboratory and uncompiled experiment for notebooks or checks."""
    lab, q0, q1, iz = build_lab()
    rf_pulse = Square(100 * us, amplitude=0.2)
    clock_pulse = Square(200 * us, amplitude=0.2)
    transport_profile = MoveProfile.minimum_jerk(segments=32)
    exp = transport_experiment(
        q0,
        q1,
        iz,
        rf_pulse=rf_pulse,
        clock_pulse=clock_pulse,
        transport_profile=transport_profile,
    )
    return lab, exp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/transport_dds")
    )
    parser.add_argument(
        "--report", action="store_true", help="Write the offline report"
    )
    args = parser.parse_args()
    lab, exp = build_demo()
    compiled = lab.compile(exp)
    if args.report:
        compiled.export_report(args.output, source_path=__file__)
    else:
        compiled.export(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "verification": "offline_compilation_not_hardware_acceptance",
                "points": len(compiled.points),
                "moves": sum(
                    event.kind == "move"
                    for point in compiled.points
                    for event in point.logical_events
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
