"""Offline spin locking with explicit axes and virtual-frame bookkeeping.

R_phi(theta)=exp[-i theta(cos(phi)X+sin(phi)Y)/2]. From |0> (+Z),
Ry(+pi/2) prepares +X, Rx(lock angle) preserves that eigenstate, and
Ry(-pi/2) analyses it back onto +Z. The ideal population is not measured data.
The illustrative 30 kHz gate convention is 200 us, pi amplitude 0.4 and
pi/2 amplitude 0.2. All amplitudes and rotation angles need lab calibration.

The perpendicular prepare/lock axes follow the spin-lock construction documented
by Zurich Instruments; our rotated axes and return-to-|0> analysis sign are
explicitly chosen here and independently checked with SU(2) matrices:
https://docs.zhinst.com/labone_q_user_manual/applications_library/reference/
contrib/experiments/spin_locking.html
"""

import argparse
import math
from pathlib import Path

from taiyidsl import (
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    play,
    reset_frame,
    rz,
    us,
)


def build_experiment(
    *,
    lock_times_us=(0, 100, 500, 2000),
    lock_amplitude=0.12,
    averages=2,
    restore_frame=True,
):
    if type(restore_frame) is not bool:
        raise TypeError("restore_frame must be boolean")
    q = Qubit("q")
    exp = Experiment(
        "hybrid_nuclear_spin_locking",
        [q],
        metadata={
            "offline_only": True,
            "hardware_verified": False,
            "calibrated_rotations": False,
            "sequence": "+Y90 -> +X lock -> -Y90",
            "initial_logical_frames": {"F_rad": 0},
            "frame_convention": "drive_phase = base + F; reset_frame clears all of F",
            "restore_final_virtual_frame": restore_frame,
            "theoretical_reference_only": "ideal resonant sequence returns |0>",
        },
    )
    lock_times_us = tuple(lock_times_us)
    lock_time = SweepParameter("lock_time", lock_times_us, "us")
    pulse = Square(200 * us, amplitude=0.4, frequency=30_000, phase=0)

    with exp.average(averages, mode="cyclic"):
        with exp.sweep(lock_time):
            with exp.realtime():
                play("cooling", Square(20 * us, amplitude=0.2, frequency=80e6))
                with exp.triggered_sequence(group="qubit_awg"):
                    # Each independent sequence starts from its carrier reference.
                    rz(q, -math.pi / 2)  # F=+pi/2: base X becomes physical +Y.
                    play(q.rf, pulse, amplitude=0.2)  # +Y90 prepares +X.

                    reset_frame(q.frame)  # Clear the full frame; lock along +X.
                    for duration in dict.fromkeys(lock_times_us):
                        with exp.case(lock_time, duration):
                            if duration != 0:  # Zero lock is an explicit reference.
                                play(
                                    q.rf,
                                    pulse,
                                    amplitude=lock_amplitude,
                                    duration=lock_time,
                                )

                    rz(q, math.pi / 2)  # F=-pi/2: base X becomes physical -Y.
                    play(q.rf, pulse, amplitude=0.2)  # -Y90 analyses +X to +Z.
                    if restore_frame:
                        rz(q, -math.pi / 2)  # Explicitly restore V=0, no RF or time.
                    # With restore_frame=False the final V=+pi/2 is preserved in
                    # compiled.virtual_z_frames(); Z readout is insensitive to it.
                play("readout", Square(10 * us))
    return exp


def main(argv=None):
    from taiyidsl.reporting.experiment_report import export_experiment_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("output/artiq_spectrum/spin_locking")
    )
    parser.add_argument(
        "--persist-frame",
        action="store_true",
        help="retain the terminal +pi/2 virtual frame instead of restoring it",
    )
    args = parser.parse_args(argv)
    lab = LabConfig.load(Path(__file__).with_name("lab_simulated.json"))
    compiled = lab.compile(build_experiment(restore_frame=not args.persist_frame))
    report = export_experiment_report(compiled, args.output, source_path=__file__)
    print(f"Offline hybrid spin-locking report: {report['index'].resolve()}")
    return report


if __name__ == "__main__":
    main()
