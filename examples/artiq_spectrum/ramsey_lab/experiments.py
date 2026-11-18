"""Explicit laboratory experiments, built from DSL without instrument access."""

from taiyidsl import (
    Experiment,
    QubitOperations,
    Square,
    SweepParameter,
    delay,
    kHz,
    play,
    qubit_operation,
    reset_phase,
    us,
)
from taiyidsl.units import as_fraction


@qubit_operation
def clock_2pi(q):
    """Emit one continuous square pulse with twice the declared clock pi time.

    This demonstration assumes constant resonant Rabi rate and an ideal square
    envelope: twice the pi duration gives twice its pulse area. It is not a
    general arbitrary-angle calibration rule or evidence of a physical 2pi gate.
    The clock optical transition/resonance is not inferred from AWG frequency.
    """
    p = q.parameters.clock
    if p.waveform != "square":
        raise ValueError("This clock_2pi demonstration requires a square waveform")
    if any(value is None for value in (p.pi_duration, p.pi_amplitude, p.frequency)):
        raise ValueError(
            "clock_2pi requires clock pi_duration, pi_amplitude, frequency"
        )
    play(
        q.clock,
        Square(2 * p.pi_duration, amplitude=p.pi_amplitude, frequency=p.frequency),
    )


def clock_ramsey_experiment(
    *,
    qubit,
    prepare,
    readout,
    second_phases=None,
    shots=2,
    preparation_to_gate=10 * us,
    group="qubit_awg",
    detuning=1 * kHz,
):
    """Native prepare -> RF pi/2 -> clock 2pi -> RF pi/2 -> native readout.

    There is no extra dark delay between these three serial pulses. Clock uses
    its own declared control frame. No multilevel phase-transfer correction is
    inferred; the second RF pulse's explicit local phase is the scan coordinate.
    """
    if not callable(prepare) or not callable(readout):
        raise TypeError("prepare and readout must be declared DSL callables")
    if second_phases is None:
        second_phases = SweepParameter("second_phase", [0, 45, 90, 135, 180], "deg")
    if not isinstance(second_phases, SweepParameter):
        raise TypeError("second_phases must be a SweepParameter with phase units")
    rf_frequency = as_fraction(qubit.parameters.rf.frequency, "frequency")
    rf_frequency += as_fraction(detuning, "frequency")
    rf_frame, clock_frame = qubit.drive_frame("rf"), qubit.drive_frame("clock")
    if rf_frame is None or clock_frame is None:
        raise ValueError("RF and clock must both declare their control phase frame")
    ops = QubitOperations(drive="rf")
    exp = Experiment(
        "clock_ramsey",
        [qubit],
        metadata={
            "detuning_mode": "rf_drive_frequency_offset",
            "second_phase_definition": "additional_local_phase_of_second_rf_pulse",
            "clock_2pi_definition": "square_pulse_at_pi_amplitude_for_twice_pi_time",
            "clock_2pi_assumption": "constant_resonant_Rabi_rate; unverified",
            "automatic_multilevel_phase_transfer": False,
            "readout_data": "native_call_only; detector_adapter_required_for_data",
            "hardware_verified": False,
        },
    )
    with exp.average(shots, mode="cyclic"), exp.sweep(second_phases):
        with exp.realtime():
            prepare()
            delay(preparation_to_gate)
            with exp.triggered_sequence(group=group):
                reset_phase(rf_frame)
                reset_phase(clock_frame)
                ops.x90(qubit, frequency=rf_frequency, phase=0)
                clock_2pi(qubit)
                ops.x90(qubit, frequency=rf_frequency, phase=second_phases)
            readout()
    return exp
