"""Shared ARTIQ export errors and static device/register contracts."""

from __future__ import annotations

import math

from taiyidsl.execution import device_execution_plan


class ExportError(ValueError):
    """A compiled plan cannot be represented by the supported exporter."""


def _devices(compiled):
    used = device_execution_plan(compiled)["used_devices"]
    return {name: compiled.setup.devices[name] for name in used}


def _origin_grid(devices, coarse_mu=8):
    grids = [int(coarse_mu)] + [
        int(getattr(device, "grid_mu", 1)) for device in devices.values()
    ]
    grids += [
        int(getattr(device, "config_grid_mu", 8))
        for device in devices.values()
        if device.kind == "dds"
    ]
    return math.lcm(*grids) if grids else 1


def _words(event, device):
    p = event.params
    frequency, amplitude, phase = (
        float(p["frequency"]),
        float(p["amplitude"]),
        float(p.get("phase", 0.0)),
    )
    if not all(math.isfinite(v) for v in (frequency, amplitude, phase)):
        raise ExportError("DDS parameters must be finite")
    sysclk = float(getattr(device, "sysclk_hz", 1e9))
    if not 0 <= frequency < sysclk / 2 or not 0 <= amplitude <= 1:
        raise ExportError("DDS frequency or amplitude outside supported range")
    ftw_unsigned = round(frequency / sysclk * (1 << 32)) & 0xFFFFFFFF
    # ARTIQ AD9910 accepts an int32 bit pattern, including negative FTWs.
    ftw = ftw_unsigned if ftw_unsigned < (1 << 31) else ftw_unsigned - (1 << 32)
    pow_ = round((phase / (2 * math.pi) % 1.0) * (1 << 16)) & 0xFFFF
    asf = round(amplitude * 0x3FFF)
    return ftw, pow_, asf


def _ordered_events(point):
    # Stable ordering matters for same-timestamp edges. The compiler owns ties.
    return sorted(point.physical_events, key=lambda event: event.start_mu)


def _point_end(point):
    return max(
        int(point.duration_mu + point.origin_mu),
        max(
            (int(e.start_mu + e.duration_mu) for e in point.physical_events), default=0
        ),
    )


def square_effect_record(name, timestamp, ftw, pow_, asf):
    """Comparable output-state change, separate from RTIO submission events."""
    return {
        "kind": "dds_effect",
        "signal": name,
        "start_mu": int(timestamp),
        "ftw": int(ftw),
        "square_pow": int(pow_),
        "asf": int(asf),
    }
