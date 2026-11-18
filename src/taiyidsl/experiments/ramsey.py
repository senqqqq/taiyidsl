"""A small native-preparation / AWG Ramsey experiment with explicit controls.

These functions build DSL only. They neither import the native ARTIQ package
nor connect instruments, and never rewrite the qubit's calibration snapshot.
"""

from contextlib import ExitStack
from numbers import Real

from ..dsl._context import _builder
from ..dsl.experiment import Experiment
from ..dsl.instructions import delay, reset_phase
from ..dsl.operations import QubitOperations, qubit_operation
from ..dsl.values import SweepParameter, validate_value
from ..units import Hz, Quantity, as_fraction, rad, us


@qubit_operation
def ramsey_gates(q, tau, *, detuning=0 * Hz, second_phase=0 * rad):
    """Build two RF pi/2 pulses separated by an edge-to-edge dark time ``tau``.

    Both pulse frequencies are the calibrated waveform frequency + detuning.
    The second pulse's local phase is second_phase; the backend accumulates the
    carrier during both pulses and idle. No extra 2*pi*detuning*tau is applied.
    A detuning sweep uses static case branches, preserving its original axis
    name and coordinates without adding a second, derived frequency sweep.
    """
    validate_value(tau, "time", "tau")
    validate_value(detuning, "frequency", "detuning", minimum=None)
    validate_value(second_phase, "phase", "second_phase", minimum=None)
    parameters = q.drive("rf")
    if parameters.frequency is None:
        raise ValueError("Ramsey requires a calibrated RF waveform frequency")
    base_frequency = as_fraction(parameters.frequency, "frequency")
    frame = q.drive_frame("rf")
    if frame is None:
        raise ValueError("Ramsey requires an explicitly associated RF phase frame")
    ops = QubitOperations(drive="rf")

    def gates(value):
        frequency = base_frequency + as_fraction(value, "frequency")
        validate_value(frequency, "frequency", "Ramsey drive frequency")
        reset_phase(frame)
        ops.x90(q, frequency=frequency, phase=0 * rad)
        delay(q.rf, tau)
        ops.x90(q, frequency=frequency, phase=second_phase)

    if isinstance(detuning, SweepParameter):
        for value in dict.fromkeys(detuning.values):
            # SweepParameter.values are already SI; retain a Quantity so case()
            # does not apply the display unit (e.g. kHz) a second time.
            with _builder().case(detuning, Quantity(value, "frequency")):
                gates(value)
    else:
        gates(detuning)


def ramsey_experiment(
    *,
    qubit,
    native=None,
    prepare=None,
    readout=None,
    delays,
    detuning=0 * Hz,
    second_phase=0 * rad,
    shots=1,
    preparation_to_gate=10 * us,
    group="qubit_awg",
):
    """Build native prepare -> Ramsey -> native readout, without hardware IO.

    Pass the already-declared DSL calls as ``prepare=prepare_MOT`` and
    ``readout=readout``. Helpers run while building DSL; their native proxies
    only append call nodes, never execute the ARTIQ kernels here.
    Alternatively, ``native`` may supply prepare_MOT() and readout() methods.
    The two forms cannot be mixed.
    Delays may be a scalar duration, a SweepParameter or an iterable of durations
    (bare values are seconds). Detuning/phase may be scalar or SweepParameter.
    The sweep nesting is delay, detuning, second_phase, outside the realtime body.
    Readout means the declared native operation, not automatic camera retrieval.
    """
    if native is not None:
        if prepare is not None or readout is not None:
            raise ValueError("Use native or direct prepare/readout calls, not both")
        prepare = getattr(native, "prepare_MOT", None)
        readout = getattr(native, "readout", None)
        if not callable(prepare) or not callable(readout):
            raise TypeError(
                "native must provide declared prepare_MOT() and readout() calls"
            )
    elif not callable(prepare) or not callable(readout):
        raise TypeError("prepare and readout must both be declared DSL callables")
    if isinstance(delays, (SweepParameter, Quantity, Real)):
        dark_time = delays
    else:
        dark_time = SweepParameter("delay", tuple(delays), "s")
    validate_value(dark_time, "time", "delays")
    validate_value(detuning, "frequency", "detuning", minimum=None)
    validate_value(second_phase, "phase", "second_phase", minimum=None)
    validate_value(preparation_to_gate, "time", "preparation_to_gate")
    axes = [
        value
        for value in (dark_time, detuning, second_phase)
        if isinstance(value, SweepParameter)
    ]
    if len({axis.name for axis in axes}) != len(axes):
        raise ValueError("Ramsey sweep axes must have distinct names")
    exp = Experiment(
        "greenmot_ramsey",
        [qubit],
        metadata={
            "detuning_mode": "drive_frequency_offset",
            "tau_definition": "first_pulse_end_to_second_pulse_start",
            "second_phase_definition": "additional_local_phase_of_second_pulse",
            "readout_data": "native_call_only; detector_adapter_required_for_data",
            "hardware_verified": False,
        },
    )
    with exp.average(shots, mode="cyclic"):
        with ExitStack() as stack:
            for axis in axes:
                stack.enter_context(exp.sweep(axis))
            with exp.realtime():
                prepare()
                delay(preparation_to_gate)
                with exp.triggered_sequence(group=group):
                    ramsey_gates(
                        qubit, dark_time, detuning=detuning, second_phase=second_phase
                    )
                readout()
    return exp
