"""Route resolved frame updates and quantize DDS carrier references."""

import copy
import math

from taiyidsl.config.hardware import DeviceCalibration
from taiyidsl.ir import CompileError


def hardware_frame_events(logical, setup, mapping):
    """Route the unified frame without applying its phase a second time.

    The scheduler has already resolved both play and command phases. Only
    mapped Raman/RF drives participate in qubit frame commands.
    """
    result = copy.deepcopy(logical)
    for event in sorted(result, key=lambda item: item.start_mu):
        if event.kind in {"rz", "shift_phase", "reset_frame", "reset_phase"}:
            uid, kind = event.signal, event.kind
            targets = set()
            signals = event.params.get(
                "initial_states", {f"{uid}.raman": {}, f"{uid}.rf": {}}
            )
            for signal in signals:
                if signal not in mapping:
                    continue
                for binding in mapping.bindings(signal):
                    if binding.device not in setup.devices:
                        raise CompileError(f"Unknown mapped device {binding.device}")
                    device = setup.devices[binding.device]
                    if device.kind != "dds":
                        continue
                    targets.add(device.name)
            if not targets:
                raise CompileError("Hardware phase command has no mapped Raman/RF DDS")
            event.kind = (
                "reset_frame"
                if kind in {"reset_frame", "reset_phase"}
                else "shift_phase"
            )
            event.params.update(
                hardware_frame_resolved=True,
                command_kind=kind,
                target_devices=sorted(targets),
            )
        elif event.kind == "play":
            uid = event.params.get("frame", event.params.get("qubit"))
            if uid is None or (
                "frame" not in event.params
                and event.signal not in {f"{uid}.raman", f"{uid}.rf"}
            ):
                continue
            event.params["hardware_frame_resolved"] = True
    return result


def copy_carrier_reference(params, command):
    """Keep synthesized held-state updates at the command's resolved reference.

    A state copied from a future play must not bring a future reset backwards.
    """
    params.pop("carrier_reference_physical_mu", None)
    if "carrier_reference_mu" in command.params:
        params["carrier_reference_mu"] = command.params["carrier_reference_mu"]
    else:
        params.pop("carrier_reference_mu", None)


def carrier_reference(params, device, setup):
    """Return the DDS output-plane reference; the logical IR stays unchanged.

    A reset is submitted early by the calibrated output delay, just like play.
    The default shot reference remains zero for backwards compatibility.
    """
    reference = 0
    if "carrier_reference_mu" in params:
        calibration = setup.calibration.get(device.name, DeviceCalibration())
        reference = (
            params["carrier_reference_mu"] - device.latency_mu - calibration.latency_mu
        )
        params["carrier_reference_physical_mu"] = reference
    return reference


def carrier_phase_word(params, device, setup):
    """A phase-offset target before the once-per-shot NCO epoch correction.

    Retain the reference's low 16 bits until the final POW quantization, rather
    than rounding a carrier reset and the NCO epoch independently.
    """
    reference = carrier_reference(params, device, setup)
    cycles = round(device.sysclk_hz * setup.tick_seconds)
    ftw = round(params["frequency"] / device.sysclk_hz * 2**32)
    phase = round(params["phase"] / (2 * math.pi) * 2**16)
    return ((phase << 16) - reference * ftw * cycles) % 2**32
