"""Named quantum operations that build reusable Sections, without hardware IO."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Mapping
from dataclasses import replace
from functools import wraps

from ..config.models import Qubit
from ._context import _source
from ._serialization import json_value
from .instructions import delay, play, rz
from .sections import _capture_section
from .values import validate_value


def qubit_operation(function):
    """Wrap a synchronous function or method as a serial Section factory.

    Direct Qubit-valued named arguments identify the targets; passing positionally
    or by keyword has identical meaning. Targets are never inferred from strings,
    annotations, collections or closure state. Fixed signatures keep the target
    declaration explicit. A call returns a Section and, when a builder is active,
    inserts it once. The wrapped function itself must return None.
    """
    if not inspect.isfunction(function) or (
        inspect.iscoroutinefunction(function)
        or inspect.isgeneratorfunction(function)
        or inspect.isasyncgenfunction(function)
        or hasattr(function, "artiq_embedded")
    ):
        raise TypeError("qubit_operation requires a synchronous DSL function or method")
    signature = inspect.signature(function)
    if any(
        parameter.kind
        in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        for parameter in signature.parameters.values()
    ):
        raise TypeError(
            "qubit_operation requires explicit named parameters, not *args/**kwargs"
        )

    @wraps(function)
    def build(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        targets, parameters = [], {}
        for index, (name, value) in enumerate(bound.arguments.items()):
            if isinstance(value, Qubit):
                targets.append(value)
                parameters[name] = {"qubit": value.uid, "revision": value.revision}
            elif index == 0 and name in {"self", "cls"}:
                continue
            else:
                parameters[name] = json_value(value)
        if not targets:
            raise TypeError("qubit_operation needs at least one direct Qubit argument")
        try:
            json.dumps(parameters, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "Operation parameters must be portable DSL values; "
                "pass each target Qubit as a direct named argument"
            ) from exc
        with _capture_section(
            function.__name__,
            "serial",
            targets=targets,
            parameters=parameters,
            source=_source(skip=(__file__,)),
        ) as result:
            if function(*bound.args, **bound.kwargs) is not None:
                raise TypeError("A qubit_operation body must return None")
        return result

    return build


def _drive_signal(qubit, drive):
    return qubit.signal(drive)


class QubitOperations:
    """Small calibrated-pulse library using explicit immutable qubit snapshots.

    Numerical parameter presence never certifies physical gate fidelity. CZ is
    supplied by the laboratory; its phase corrections are part of that body's
    definition, and are not applied a second time by this wrapper.
    """

    def __init__(self, *, drive="rf", cz_impl=None, pair_calibrations=()):
        from ..config.pair_calibration import PairCalibration

        if not isinstance(drive, str) or not drive:
            raise ValueError("An operation library requires a drive name")
        if cz_impl is not None and not callable(cz_impl):
            raise TypeError("cz_impl must build a Section or emit DSL")
        self.drive = drive
        self.cz_impl = cz_impl
        self._pairs = {}
        for pair in pair_calibrations:
            if not isinstance(pair, PairCalibration):
                raise TypeError("pair_calibrations must contain PairCalibration")
            if pair.qubits in self._pairs:
                raise ValueError("Duplicate ordered pair calibration")
            self._pairs[pair.qubits] = pair

    def _rotation(
        self,
        q,
        rotation,
        axis_phase,
        amplitude,
        duration,
        phase,
        *,
        drive=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        from ._context import _builder
        from .pulses import FlatTopGaussian, Square

        route = self.drive if drive is None else drive
        parameters = q.drive(route)
        shape_parameters = replace(
            parameters,
            waveform=parameters.waveform if waveform is None else waveform,
            waveform_parameters=(
                parameters.waveform_parameters
                if waveform_parameters is None
                and (waveform is None or waveform == parameters.waveform)
                else waveform_parameters or {}
            ),
        )
        gate_duration = (
            getattr(parameters, f"{rotation}_duration")
            if duration is None
            else duration
        )
        gate_amplitude = (
            parameters.rotation_amplitude(rotation) if amplitude is None else amplitude
        )
        gate_frequency = parameters.frequency if frequency is None else frequency
        missing = [
            name
            for name, value in (
                (f"{rotation}_duration", gate_duration),
                (f"{rotation}_amplitude", gate_amplitude),
                ("frequency", gate_frequency),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"Qubit {q.uid} parameters.{route} missing required gate parameters: "
                + ", ".join(missing)
            )
        arguments = {
            "amplitude": gate_amplitude,
            "frequency": gate_frequency,
            "phase": axis_phase if phase is None else phase,
        }
        if shape_parameters.waveform == "square":
            pulse = Square(gate_duration, **arguments)
        else:
            from ..units import as_fraction

            shape = dict(shape_parameters.waveform_parameters)
            cutoff = shape.get("cutoff_sigma", 4.0)
            flat = as_fraction(gate_duration, "time") - 2 * as_fraction(
                shape["sigma"], "time"
            ) * as_fraction(cutoff, "dimensionless")
            if flat < 0:
                raise ValueError(
                    "Calibrated total duration is shorter than Gaussian edges"
                )
            pulse = FlatTopGaussian(flat_duration=flat, **shape, **arguments)
        _builder().root.params["calibration"] = {
            "qubit": q.uid,
            "revision": q.revision,
            "fingerprint": q.fingerprint,
            "drive": route,
            "rotation": rotation,
            "parameters": parameters.to_dict(),
            "effective_pulse": json_value(pulse),
            "physical_acceptance": "not_verified",
        }
        play(q.signal(route), pulse)

    def global_x90(
        self,
        *qubits,
        calibration,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        """Emit one reference-calibrated pulse on a declared public field.

        All affected qubits are reserved, but only the explicitly selected
        calibration qubit supplies pulse parameters and control frame. This
        does not assert that the other atoms experience a calibrated X90.
        The compiler verifies that the targets cover the physical public field.
        """
        from ._context import _builder

        if not qubits or any(not isinstance(q, Qubit) for q in qubits):
            raise TypeError("global_x90 requires explicit Qubit targets")
        if len({q.uid for q in qubits}) != len(qubits):
            raise ValueError("global_x90 targets must be unique")
        if not isinstance(calibration, Qubit) or not any(
            calibration.to_dict() == q.to_dict() for q in qubits
        ):
            raise ValueError("calibration must be one of the target snapshots")
        with _capture_section(
            "global_x90",
            "serial",
            targets=qubits,
            parameters={"calibration": calibration.uid, "drive": self.drive},
            source=_source(skip=(__file__,)),
        ) as result:
            _builder().root.params["global_operation"] = {
                "targets": [q.uid for q in qubits],
                "signals": [q.signal(self.drive) for q in qubits],
                "calibration": calibration.uid,
                "drive": self.drive,
            }
            self._rotation(
                calibration,
                "pi2",
                0,
                amplitude,
                duration,
                phase,
                frequency=frequency,
                waveform=waveform,
                waveform_parameters=waveform_parameters,
            )
        return result

    @qubit_operation
    def x90(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi2",
            0,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def xm90(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi2",
            math.pi,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def y90(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi2",
            math.pi / 2,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def ym90(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi2",
            -math.pi / 2,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def x180(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi",
            0,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def xm180(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi",
            math.pi,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def y180(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi",
            math.pi / 2,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def ym180(
        self,
        q,
        *,
        amplitude=None,
        duration=None,
        phase=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        self._rotation(
            q,
            "pi",
            -math.pi / 2,
            amplitude,
            duration,
            phase,
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def z90(self, q):
        rz(q, math.pi / 2)

    @qubit_operation
    def z180(self, q):
        rz(q, math.pi)

    @qubit_operation
    def clock(
        self,
        q,
        *,
        rotation="pi",
        phase=0,
        amplitude=None,
        duration=None,
        frequency=None,
        waveform=None,
        waveform_parameters=None,
    ):
        if rotation not in {"pi", "pi2"}:
            raise ValueError("clock rotation must be explicitly pi or pi2")
        self._rotation(
            q,
            rotation,
            0,
            amplitude,
            duration,
            phase,
            drive="clock",
            frequency=frequency,
            waveform=waveform,
            waveform_parameters=waveform_parameters,
        )

    @qubit_operation
    def cz(self, q0, q1):
        from ._context import _builder
        from .sections import Section, add

        if q0.uid == q1.uid:
            raise ValueError("CZ requires two distinct qubits")
        if self.cz_impl is None:
            raise ValueError(
                "CZ requires an explicitly supplied laboratory gate implementation"
            )
        pair = self._pairs.get((q0.uid, q1.uid))
        if self._pairs and pair is None:
            raise ValueError("No calibration for this ordered qubit pair")
        if pair is not None:
            pair.validate(q0, q1)
            _builder().root.params["calibration"] = pair.to_dict()
        else:
            _builder().root.params["calibration"] = {
                "physical_acceptance": "not_verified"
            }
        builder = _builder()
        builder._section_insertions = []
        try:
            result = self.cz_impl(q0, q1)
            if isinstance(result, Section):
                if not any(item is result for item in builder._section_insertions):
                    add(result)
            elif result is not None:
                raise TypeError("cz_impl must return Section or None")
        finally:
            del builder._section_insertions


@qubit_operation
def rabi_body(q, pulse, *, drive="rf"):
    """Play one explicitly supplied pulse; its parameters may be sweep values.

    Preparation, averaging, trigger ownership and readout belong to the caller.
    No pulse area or calibration is inferred from the operation's name.
    """
    play(_drive_signal(q, drive), pulse)


@qubit_operation
def ramsey_body(
    q, pi2_pulse, dark_time, *, drive="rf", final_phase=None, virtual_z=None
):
    """Emit pi/2, dark time, optional logical Rz, and the second pi/2 pulse.

    ``dark_time`` excludes the finite pulses. ``final_phase`` is an absolute
    override of the final pulse's base phase; None retains the supplied pulse.
    ``virtual_z`` updates the logical frame and remains active after this body.
    All angles are radians. The caller supplies calibrated pulses explicitly.
    """
    signal = _drive_signal(q, drive)
    play(signal, pi2_pulse)
    delay(signal, dark_time)
    if virtual_z is not None:
        rz(q, virtual_z)
    if final_phase is None:
        play(signal, pi2_pulse)
    else:
        play(signal, pi2_pulse, phase=final_phase)


@qubit_operation
def echo_body(q, pi2_pulse, pi_pulse, half_wait, *, drive="rf"):
    """Emit pi/2, wait, pi, wait, pi/2 using the supplied pulse axes.

    ``half_wait`` is each dark interval, excluding pulse durations. For a
    Y-axis refocusing pulse, supply a pi pulse with phase=pi/2. This body does
    not change frames, prepare a state or make a calibrated-gate claim.
    """
    signal = _drive_signal(q, drive)
    play(signal, pi2_pulse)
    delay(signal, half_wait)
    play(signal, pi_pulse)
    delay(signal, half_wait)
    play(signal, pi2_pulse)


_RB_PHASES = {
    "x90": 0.0,
    "xm90": math.pi,
    "y90": math.pi / 2,
    "ym90": -math.pi / 2,
    "x180": 0.0,
    "y180": math.pi / 2,
}


@qubit_operation
def rb_body(q, sequence, pi2_pulse, pi_pulse, *, drive="rf", inter_gate_delay=0):
    """Emit one fixed gate word returned by templates.rb_sequences.

    Generate sequences once with an explicit seed, then select them with the
    caller's sweep/case structure. This function never draws random numbers.
    It retains the supplied sequence descriptor in Section metadata, including
    Clifford indices and recovery. Gate words set absolute X/Y base phases,
    overriding the two supplied pulses' base phases; duration, amplitude,
    frequency and local modulation are retained. Gaps are explicit and may be
    zero when the selected backend can implement the resulting sequence.
    """
    if not isinstance(sequence, Mapping) or not isinstance(
        sequence.get("gates"), (list, tuple)
    ):
        raise TypeError("sequence must contain the gates from rb_sequences")
    gates = sequence["gates"]
    if any(not isinstance(gate, str) or gate not in _RB_PHASES for gate in gates):
        raise ValueError("RB sequence contains an unsupported gate name")
    validate_value(inter_gate_delay, "time", "inter_gate_delay")
    signal = _drive_signal(q, drive)
    for index, gate in enumerate(gates):
        pulse = pi_pulse if gate.endswith("180") else pi2_pulse
        play(signal, pulse, phase=_RB_PHASES[gate])
        if index + 1 < len(gates):
            delay(signal, inter_gate_delay)
