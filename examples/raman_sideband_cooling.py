"""Offline pulsed 3-axis RSC plus a separate sideband spectroscopy scan.

No MOT, loading, field ramp or trap waveform is synthesized. before_shot must
provide loaded, precooled atoms with the fixed bias/trap/optics conditions in
the JSON profile. RSC, final spin preparation, probe and readout share one kernel.
All supplied RF values and times are illustrative, not laboratory calibrations.
"""

from __future__ import annotations

from pathlib import Path

from taiyidsl import Experiment, LabConfig, OpticsConfig, Qubit, SweepParameter
from taiyidsl.cooling import illuminated_readout, raman_pulse, sideband_cool


def reference_rsc_lab(qubit):
    """Compatibility wrapper; the reusable profile contains all hardware wiring."""
    path = Path(__file__).parent / "config/labs/rsc.json"
    return LabConfig.load(path, qubit_id=qubit.uid)


def build_cooling_scan(
    qubit,
    parameters,
    *,
    rounds=(0, 1, 3),
    axes=("x", "y", "z"),
    cycles_per_axis=2,
    illumination=("image", "image_cooling"),
    shots=20,
    averaging="cyclic",
):
    """Compare cycle counts and interleave push-out/reference conditions."""
    rounds = tuple(rounds)
    if len(set(rounds)) != len(rounds):
        raise ValueError("rounds must contain distinct values")
    axes, illumination = tuple(axes), tuple(illumination)
    exp = Experiment(
        "rsc_round_scan",
        qubits=[qubit],
        metadata={
            "target_spin": "1S0 mF=+1/2",
            "host_precondition": parameters.get("conditions", ""),
            "readout_modes": {"0": "survival_reference", "1": "remove_target_spin"},
            "purpose": "survival/spin diagnostic, NOT a temperature measurement",
            "sequence": {
                "axes": axes,
                "cycles_per_axis": cycles_per_axis,
                "readout": "illuminated",
                "illumination": illumination,
            },
        },
        parameters=OpticsConfig.coerce(parameters),
    )
    q = exp.qubits[qubit.uid]
    p = exp.parameters
    count = SweepParameter("rounds", rounds, unit="dimensionless")
    readout = SweepParameter("readout_mode", (0, 1), unit="dimensionless")
    with exp.average(shots=shots, mode=averaging):
        with exp.sweep(count) as cnt:
            with exp.sweep(readout):
                with exp.realtime():
                    sideband_cool(
                        exp,
                        p,
                        axes=axes,
                        cycles_per_axis=cycles_per_axis,
                        rounds=cnt,
                    )
                    for mode in (0, 1):
                        with exp.case(readout, mode):
                            illuminated_readout(
                                exp, q, p, illumination=illumination, pushout=bool(mode)
                            )
    return exp


def build_thermometry_scan(
    qubit,
    parameters,
    *,
    axis="x",
    axes=("x", "y", "z"),
    cycles_per_axis=2,
    cooling_rounds=2,
    illumination=("image", "image_cooling"),
    shots=20,
    averaging="cyclic",
):
    """Raw spin-flip spectroscopy; RF samples must be replaced by calibration.

    Scan covers BOTH sides of a placeholder DDS frequency. No frequency sign is
    declared to be a physical red/blue sideband until optical mapping is known.
    After the probe, do NOT optically pump: that would erase its spin signal.
    """
    if axis not in parameters["raman"]:
        raise ValueError("No cooling stage is configured for the requested axis")
    axes, illumination = tuple(axes), tuple(illumination)
    exp = Experiment(
        f"rsc_thermometry_{axis}",
        qubits=[qubit],
        metadata={
            "probe_axis": axis,
            "probe_rf_calibration": "illustrative; map to measured Raman carrier",
            "host_precondition": parameters.get("conditions", ""),
            "sequence": {
                "axes": axes,
                "cycles_per_axis": cycles_per_axis,
                "cooling_rounds": cooling_rounds,
                "readout": "illuminated",
                "illumination": illumination,
            },
        },
        parameters=OpticsConfig.coerce(parameters),
    )
    q = exp.qubits[qubit.uid]
    p = exp.parameters
    setting = p["raman"][axis]
    pair = {role: p["beams"][setting[role]] for role in ("a", "b")}
    frequency = SweepParameter(
        "probe_rf",
        (
            79_880_000,
            79_900_000,
            79_920_000,
            80_080_000,
            80_100_000,
            80_120_000,
        ),
        unit="Hz",
    )
    readout = SweepParameter("readout_mode", (0, 1), unit="dimensionless")
    with exp.average(shots=shots, mode=averaging):
        with exp.sweep(frequency) as probe_frequency:
            with exp.sweep(readout):
                with exp.realtime():
                    sideband_cool(
                        exp,
                        p,
                        axes=axes,
                        cycles_per_axis=cycles_per_axis,
                        rounds=cooling_rounds,
                    )
                    raman_pulse(
                        exp,
                        pair,
                        duration_s=5e-6,
                        setup_wait_s=20e-6,
                        beam_b_frequency=probe_frequency,
                    )
                    for mode in (0, 1):
                        with exp.case(readout, mode):
                            illuminated_readout(
                                exp, q, p, illumination=illumination, pushout=bool(mode)
                            )
    return exp


def main():
    root = Path(__file__).resolve().parents[1]
    parameters = OpticsConfig.load(root / "examples/config/optics/rsc.json")
    qubit = Qubit("q0")
    lab = LabConfig.load(root / "examples/config/labs/rsc.json", qubit_id=qubit.uid)
    for experiment in (
        build_cooling_scan(qubit, parameters),
        build_thermometry_scan(qubit, parameters),
    ):
        output = root / "output" / experiment.name
        compiled = lab.compile(experiment)
        compiled.save(output / "compiled.json")
        compiled.visualize(output / "timeline.html")
        compiled.export_artiq(output / "artiq")
        print(f"{experiment.name}: {len(compiled.points)} points; output: {output}")


if __name__ == "__main__":
    main()
