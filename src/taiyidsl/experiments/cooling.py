"""Reusable Python operations over ordinary, validated optical parameter tables.

These functions build inspectable DSL nodes; they never access instruments.
Execution order and counts are explicit function arguments, not configuration types.
"""

from collections.abc import Mapping
from contextlib import contextmanager

from ..config.optics import OpticsConfig as OpticsConfig
from ..config.optics import validate_beam, validate_pulse
from ..dsl._serialization import json_value
from ..dsl.instructions import delay, measure, play
from ..dsl.pulses import Square
from ..dsl.values import validate_count


@contextmanager
def _operation(exp, name, parameters):
    with exp.serial() as node:
        node.params["operation_name"] = name
        node.params["effective_parameters"] = json_value(parameters)
        yield


def _beam(beam, duration, *, frequency=None):
    play(
        beam["signal"],
        Square(
            duration=duration,
            amplitude=beam["amplitude"],
            phase=beam.get("phase_rad", 0),
            frequency=beam["frequency_hz"] if frequency is None else frequency,
        ),
    )


def optical_pump(
    exp,
    config,
    *,
    duration=None,
    amplitude=None,
    frequency=None,
    phase=None,
    setup_wait=None,
    settle=None,
):
    """Preparation -> scattering pulse -> settling, with one-call overrides.

    For example, ``amplitude=0`` changes this pulse only. No input mapping or
    experiment snapshot is updated. Numeric arguments use SI; quantities and
    compatible static SweepParameters are accepted as explicit overrides.
    """
    values = validate_pulse(config, path="optical_pump")
    overrides = {
        "duration_s": duration,
        "amplitude": amplitude,
        "frequency_hz": frequency,
        "phase_rad": phase,
        "setup_wait_s": setup_wait,
        "settle_s": settle,
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    _pump(exp, values)


def _pump(exp, values):
    """Emit a resolved pump; DSL primitives validate effective numeric values."""
    with _operation(exp, "optical_pump", values):
        delay(values["signal"], values["setup_wait_s"])
        _beam(values, values["duration_s"])
        delay(values["signal"], values["settle_s"])


def raman_pulse(exp, pair, *, duration_s, setup_wait_s, beam_b_frequency=None):
    """Synchronous beams with an explicit preparation window and optional sweep.

    ``pair`` contains ``a`` and ``b`` beam parameter mappings. Frequencies are
    calibrated signal RF values; optical beat/sideband signs are not inferred.
    """
    if not isinstance(pair, Mapping) or set(pair) != {"a", "b"}:
        raise ValueError("Raman pair requires only a and b beam mappings")
    a = validate_beam(pair["a"], path="raman.a")
    b = validate_beam(pair["b"], path="raman.b")
    if a["signal"] == b["signal"]:
        raise ValueError("Raman beams need distinct logical signals")
    if beam_b_frequency is not None:
        b["frequency_hz"] = beam_b_frequency
    _raman(exp, a, b, duration_s=duration_s, setup_wait_s=setup_wait_s)


def _raman(exp, a, b, *, duration_s, setup_wait_s):
    """Emit resolved beams without revalidating the enclosing configuration."""
    values = {"a": a, "b": b, "duration_s": duration_s, "setup_wait_s": setup_wait_s}
    with _operation(exp, "raman_pulse", values):
        delay(a["signal"], setup_wait_s)
        with exp.parallel(align="start"):
            _beam(a, duration_s)
            _beam(b, duration_s)


def sideband_cool(
    exp, parameters, *, axes=("x", "y", "z"), cycles_per_axis=2, rounds=2
):
    """Initial spin pump, then explicitly ordered axes and dissipative resets.

    rounds=0 retains the initial pump as a control. Counts and order are execution
    arguments. Static repeats remain in IR and currently expand within each shot.
    """
    parameters = OpticsConfig.coerce(parameters)
    validate_count(cycles_per_axis, "cycles_per_axis", minimum=1)
    if isinstance(axes, str):
        raise TypeError("axes must be a sequence of axis names")
    axes = tuple(axes)
    if not axes:
        raise ValueError("At least one cooling axis is required")
    pump = parameters["pump"]
    beams = parameters["beams"]
    execution = {"axes": axes, "cycles_per_axis": cycles_per_axis, "rounds": rounds}
    with _operation(exp, "sideband_cool", execution):
        _pump(exp, pump)
        with exp.repeat(rounds):
            for axis in axes:
                spec = parameters["raman"][axis]
                with _operation(exp, f"{axis}_red_sideband", dict(spec)):
                    with exp.repeat(cycles_per_axis):
                        _raman(
                            exp,
                            beams[spec["a"]],
                            beams[spec["b"]],
                            duration_s=spec["duration_s"],
                            setup_wait_s=spec["setup_wait_s"],
                        )
                        _pump(exp, pump)


def _readout_parameters(parameters, pushout, camera_protocol):
    if not isinstance(pushout, bool):
        raise TypeError("pushout must be a build-time bool")
    if not isinstance(camera_protocol, str) or not camera_protocol:
        raise ValueError("camera_protocol must be a nonempty name")
    return parameters["pushout"], parameters["camera"]["transfer_wait_s"]


def _pushout(pulse, enabled, *, keep_zero_waits):
    if keep_zero_waits or pulse["setup_wait_s"]:
        delay(pulse["signal"], pulse["setup_wait_s"])
    if enabled:
        _beam(pulse, pulse["duration_s"])
    else:
        delay(pulse["signal"], pulse["duration_s"])
    if keep_zero_waits or pulse["settle_s"]:
        delay(pulse["signal"], pulse["settle_s"])


def trigger_readout(
    exp, qubit, parameters, *, pushout=True, handle="survivors", camera_protocol="image"
):
    """Push-out/reference -> camera trigger -> transfer wait; no illumination."""
    parameters = OpticsConfig.coerce(parameters)
    pulse, transfer = _readout_parameters(parameters, pushout, camera_protocol)
    values = {"pushout": pulse, "enabled": pushout, "transfer_wait_s": transfer}
    with _operation(exp, "trigger_readout", values):
        _pushout(pulse, pushout, keep_zero_waits=False)
        measure(qubit, protocol=camera_protocol, handle=handle)
        delay(qubit.readout, transfer)


def illuminated_readout(
    exp,
    qubit,
    parameters,
    *,
    illumination=("image", "image_cooling"),
    pushout=True,
    handle="survivors",
    camera_protocol="image",
):
    """Push-out/reference -> simultaneous illumination/trigger -> transfer wait."""
    parameters = OpticsConfig.coerce(parameters)
    pulse, transfer = _readout_parameters(parameters, pushout, camera_protocol)
    exposure = parameters["imaging"]["exposure_s"]
    if isinstance(illumination, str):
        raise TypeError("illumination must be a sequence of beam names")
    beams = [parameters["beams"][name] for name in illumination]
    if not beams or len({beam["signal"] for beam in beams}) != len(beams):
        raise ValueError("Illumination requires distinct, nonempty beam signals")
    values = {
        "pushout": pulse,
        "enabled": pushout,
        "illumination": beams,
        "exposure_s": exposure,
        "transfer_wait_s": transfer,
    }
    with _operation(exp, "illuminated_readout", values):
        _pushout(pulse, pushout, keep_zero_waits=True)
        with exp.parallel(align="start", exact_duration=exposure):
            for beam in beams:
                _beam(beam, exposure)
            measure(qubit, protocol=camera_protocol, handle=handle)
        delay(qubit.readout, transfer)
