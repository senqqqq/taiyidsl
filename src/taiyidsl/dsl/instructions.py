"""Record operations as unscheduled intent; never compile or touch devices."""

from dataclasses import replace

from ..config.models import Qubit
from ._context import _builder, _has_scope, _in_section, _reject_section_boundary
from .pulses import FlatTopGaussian, SampledPulse, Square
from .values import SweepParameter, _signal, validate_value

_UNSET = object()


def play(
    signal,
    pulse,
    *,
    amplitude=_UNSET,
    phase=_UNSET,
    frequency=_UNSET,
    detuning=_UNSET,
    targets=_UNSET,
    duration=_UNSET,
    flat_duration=_UNSET,
    phase_modulation=_UNSET,
    phase_modulation_mode=_UNSET,
):
    """Play a named pulse with optional overrides and a scalar frequency offset.

    ``detuning`` is added to the resolved frequency (explicit pulse/override or
    Qubit default). It changes the actual drive, not a separate phase correction.
    ``targets`` explicitly declares all qubits affected by one public-field
    pulse. The signal identifies its calibration source; no pulses are duplicated.
    """
    _append_pulse(
        "play",
        signal,
        pulse,
        amplitude=amplitude,
        phase=phase,
        frequency=frequency,
        detuning=detuning,
        targets=targets,
        duration=duration,
        flat_duration=flat_duration,
        phase_modulation=phase_modulation,
        phase_modulation_mode=phase_modulation_mode,
    )


def trigplay(
    signal,
    pulse,
    *,
    amplitude=_UNSET,
    phase=_UNSET,
    frequency=_UNSET,
    detuning=_UNSET,
    targets=_UNSET,
    duration=_UNSET,
    flat_duration=_UNSET,
    phase_modulation=_UNSET,
    phase_modulation_mode=_UNSET,
):
    """Add a Spectrum gate to the enclosing triggered_sequence program."""
    if not _in_section() and not _has_scope(_builder(), "triggered_sequence"):
        raise ValueError("trigplay requires an enclosing triggered_sequence")
    _append_pulse(
        "trigplay",
        signal,
        pulse,
        amplitude=amplitude,
        phase=phase,
        frequency=frequency,
        detuning=detuning,
        targets=targets,
        duration=duration,
        flat_duration=flat_duration,
        phase_modulation=phase_modulation,
        phase_modulation_mode=phase_modulation_mode,
    )


def trig(signal, *, width):
    """Schedule one TTL pulse, returning to the device's declared idle level.

    This only records digital output. It does not arm a receiver, wait for its
    completion, acquire a result, or establish a phase epoch.
    """
    exp = _builder()
    if not _in_section() and not _has_scope(exp, "realtime"):
        raise ValueError("trig must be inside realtime")
    if _has_scope(exp, "triggered_sequence"):
        raise ValueError("trig cannot be inside triggered_sequence")
    validate_value(width, "time", "trigger width")
    exp._append("trig", signal=_signal(signal), duration=width)


def _public_pulse_targets(signal, targets, builder):
    from .sections import _owner, _validate_targets

    if (
        not isinstance(targets, (tuple, list))
        or not targets
        or any(not isinstance(target, Qubit) for target in targets)
    ):
        raise TypeError("play targets must be a nonempty tuple of Qubit objects")
    if len({target.uid for target in targets}) != len(targets):
        raise ValueError("play targets must be unique")
    sources = [
        (target, role)
        for target in targets
        for role in target.drive_names
        if target.signal(role) == signal
    ]
    if len(sources) != 1:
        raise ValueError("play signal must identify exactly one target's drive role")
    source, role = sources[0]
    metadata = {
        "global_operation": {
            "targets": [target.uid for target in targets],
            "signals": [target.signal(role) for target in targets],
            "calibration": source.uid,
            "drive": role,
        },
        "operation_qubits": [target.uid for target in targets],
    }
    snapshots = [target.to_dict() for target in targets]
    if getattr(builder, "_is_section_collector", False):
        builder._merge_targets(snapshots)
    else:
        _validate_targets(_owner(builder), snapshots)
    return metadata


def _append_pulse(kind, signal, pulse, **values):
    if not isinstance(pulse, (Square, FlatTopGaussian, SampledPulse)):
        raise NotImplementedError(
            "Supported pulses are Square, FlatTopGaussian and SampledPulse"
        )
    overrides = {name: value for name, value in values.items() if value is not _UNSET}
    if isinstance(pulse, SampledPulse):
        if "duration" in overrides or "flat_duration" in overrides:
            raise TypeError(
                "Sampled pulse duration is fixed by sample count and interval"
            )
        if pulse.kind == "waveform" and overrides.keys() & {
            "frequency",
            "phase",
            "detuning",
        }:
            raise ValueError(
                "A final waveform cannot accept frequency, detuning or phase"
            )
    detuning = overrides.pop("detuning", _UNSET)
    targets = overrides.pop("targets", _UNSET)
    parameters = {}
    if detuning is not _UNSET:
        if isinstance(detuning, SweepParameter):
            raise TypeError("play detuning must be a scalar frequency offset")
        validate_value(detuning, "frequency", "detuning", minimum=None)
        parameters["detuning"] = detuning
    if isinstance(pulse, FlatTopGaussian) and "duration" in overrides:
        raise TypeError("Override flat_duration for FlatTopGaussian, not duration")
    if isinstance(pulse, Square) and "flat_duration" in overrides:
        raise TypeError("flat_duration only applies to FlatTopGaussian")
    if overrides:
        pulse = replace(pulse, **overrides)
    builder, signal = _builder(), _signal(signal)
    if targets is not _UNSET:
        parameters.update(_public_pulse_targets(signal, targets, builder))
    builder._append(kind, signal=signal, pulse=pulse, **parameters)


def move(qubits, *, to, duration, profile=None, settle=0):
    """Record serial atom transport; resolve its source at each executed occurrence.

    A batch explicitly pairs each Qubit with a Position or IZ site. This never
    changes Qubit.position. Timing and hardware execution remain compiler work.
    """
    from ..config.models import Position
    from ..config.transport import MoveProfile, SiteRef
    from .sections import _owner, _validate_targets

    qubits = (qubits,) if isinstance(qubits, Qubit) else tuple(qubits)
    destinations = (to,) if isinstance(to, (Position, SiteRef)) else tuple(to)
    if not qubits or any(not isinstance(q, Qubit) for q in qubits):
        raise TypeError("move requires one or more Qubit objects")
    if len({q.uid for q in qubits}) != len(qubits):
        raise ValueError("move qubits must be unique")
    if len(qubits) != len(destinations) or any(
        not isinstance(target, (Position, SiteRef)) for target in destinations
    ):
        raise ValueError("move requires one Position or SiteRef per qubit")
    profile = MoveProfile.linear() if profile is None else profile
    if not isinstance(profile, MoveProfile):
        raise TypeError("move profile must be a MoveProfile")
    validate_value(duration, "time", "move duration")
    validate_value(settle, "time", "move settle")
    builder = _builder()
    snapshots = [q.to_dict() for q in qubits]
    if getattr(builder, "_is_section_collector", False):
        builder._merge_targets(snapshots)
    else:
        _validate_targets(_owner(builder), snapshots)
    builder._append(
        "move",
        qubits=[q.uid for q in qubits],
        destinations=destinations,
        duration=duration,
        settle=settle,
        profile=profile,
        operation_qubits=[q.uid for q in qubits],
    )


def delay(signal=_UNSET, duration=_UNSET):
    """Wait on a signal or branch, or extend a declared native timed prelude."""
    unqualified = signal is _UNSET or duration is _UNSET
    if signal is _UNSET and duration is _UNSET:
        raise TypeError("delay requires a duration")
    if duration is _UNSET:
        duration = signal
    validate_value(duration, "time", "duration")
    if unqualified:
        exp = _builder()
        if _in_section():
            exp._append("section_delay", duration=duration)
            return
        if exp._stack[-1].kind == "realtime" and any(
            node.kind == "native_call" and node.params["timing"] == "timed"
            for node in exp._stack[-1].children
        ):
            exp._append("native_delay", duration=duration)
            return
        if not _has_scope(exp, "realtime"):
            raise ValueError("delay(duration) requires an enclosing realtime block")
        # The target is selected after construction. Hybrid compilation checks
        # for its triggered_sequence, standalone compilation uses the whole shot.
        exp._append("scope_delay", duration=duration)
        return
    _builder()._append("delay", signal=_signal(signal), duration=duration)


def reserve(resource, duration):
    """Reserve a logical signal or a named physical resource for this duration."""
    validate_value(duration, "time", "duration")
    _builder()._append("reserve", signal=_signal(resource), duration=duration)


def barrier(*qubits):
    """Join selected qubits/resources, or the whole current Section if omitted."""
    if not _in_section():
        raise ValueError("barrier requires an enclosing Section")
    from ..config.models import Qubit

    if any(not isinstance(q, Qubit) for q in qubits):
        raise TypeError("barrier targets must be Qubits")
    if len({q.uid for q in qubits}) != len(qubits):
        raise ValueError("barrier targets must be unique")
    if qubits:
        builder = _builder()
        if getattr(builder, "_is_section_collector", False):
            builder._merge_targets(q.to_dict() for q in qubits)
        builder._append("barrier", qubits=[q.uid for q in qubits])
    else:
        _builder()._append("barrier")


def shift_phase(frame, angle):
    """Add an angle to the persistent drive frame (radians unless unit-tagged)."""
    validate_value(angle, "phase", "angle", minimum=None)
    _builder()._append("shift_phase", frame=_signal(frame), angle=angle)


def reset_frame(frame):
    """Clear the complete drive frame, including prior virtual-Z contributions.

    Carrier evolution and inherited pulse modulation are unaffected.
    """
    _builder()._append("reset_frame", frame=_signal(frame))


def reset_phase(frame):
    """Clear the frame and carried modulation; establish a zero carrier reference.

    The reference is the final scheduled position of this operation. Pulse-local
    phases and calibration remain in force. This does not initialize a qubit's
    quantum state and must not interrupt an active pulse.
    """
    _builder()._append("reset_phase", frame=_signal(frame))


def rz(qubit, angle):
    """Apply a virtual Rz(angle) and update the hardware drive reference.

    Rz(theta) = exp(-i theta Z / 2). Positive theta subtracts theta from
    subsequent q.raman/q.rf drive phases. The operation has zero logical duration;
    the ARTIQ backend prepares and commits the phase at its exact boundary, even
    without a subsequent play. It requires a supported timed DDS phase path.
    This uses the same persistent frame as shift_phase(frame, -angle).
    reset_frame and reset_phase both clear its contribution. The Rz instruction
    remains in logical IR for provenance and gate-level composition.
    """
    if not isinstance(qubit, Qubit):
        raise TypeError("rz requires a Qubit")
    validate_value(angle, "phase", "angle", minimum=None)
    _builder()._append("rz", qubit=qubit.uid, angle=angle)


def prepare(qubit, protocol="standard"):
    _reject_section_boundary("prepare")
    exp = _builder()
    if any(n.kind == "realtime" for n in exp._stack):
        raise ValueError("prepare must be outside realtime")
    exp._append("prepare", qubit=qubit.uid, protocol=protocol)


def measure(qubit, protocol="state_readout", handle="population"):
    if not isinstance(handle, str) or not handle:
        raise ValueError("Measurement handle must be nonempty")
    _builder()._append("measure", qubit=qubit.uid, protocol=protocol, handle=handle)


def acquire(signal, *, duration, handle, qubits, metadata=None):
    """Declare one shared detector trigger and its associated site results.

    ``duration`` is the TTL trigger width, not the exposure or transfer time.
    Data are collected by the host adapter; this builder returns no measurement.
    """
    validate_value(duration, "time", "duration")
    if not isinstance(handle, str) or not handle:
        raise ValueError("Acquisition handle must be nonempty")
    if isinstance(qubits, str):
        raise TypeError("qubits must be a sequence of Qubits or identifiers")
    identifiers = tuple(getattr(q, "uid", q) for q in qubits)
    if not identifiers or any(not isinstance(q, str) or not q for q in identifiers):
        raise ValueError("Acquisition needs nonempty qubit identifiers")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Acquisition qubits must be unique")
    _builder()._append(
        "acquire",
        signal=_signal(signal),
        duration=duration,
        handle=handle,
        qubits=identifiers,
        metadata=metadata or {},
    )


def serial(exact_duration=None):
    return _builder().serial(exact_duration)


def parallel(align="start", exact_duration=None):
    return _builder().parallel(align, exact_duration)


def strict_timing():
    return _builder().serial()


Rz = rz
