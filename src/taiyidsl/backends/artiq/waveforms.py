"""Deterministic sampling of normalized envelopes for the AD9910 RAM path."""

import math

from taiyidsl.ir import CompileError


def ad9910_amplitude_ram(parameters, sysclk_hz, duration_s):
    """Return the actual ASF samples; the final zero occurs at pulse end."""
    interval = parameters["sample_interval_s"]
    raw_step = interval * sysclk_hz / 4
    step = round(raw_step)
    if not 1 <= step <= 65535 or not math.isclose(raw_step, step, abs_tol=1e-8):
        raise CompileError("RAM sample interval must be 4*step/sysclk, step=1..65535")
    interval = 4 * step / sysclk_hz
    n = round(duration_s / interval)
    edge = parameters["sigma_s"] * parameters["cutoff_sigma"]
    edge_n = round(edge / interval)
    if not math.isclose(duration_s / interval, n, abs_tol=1e-8) or not math.isclose(
        edge / interval, edge_n, abs_tol=1e-8
    ):
        raise CompileError(
            "Gaussian edge and total duration must match RAM sample interval"
        )
    if n + 1 > 1024:
        raise CompileError(
            "AD9910 waveform exceeds 1024 RAM words; choose a coarser sample_interval"
        )
    if edge_n < 2:
        raise CompileError("Gaussian edge requires at least two RAM sample intervals")
    sigma = parameters["sigma_s"]
    baseline = math.exp(-0.5 * parameters["cutoff_sigma"] ** 2)
    denominator = -math.expm1(-0.5 * parameters["cutoff_sigma"] ** 2)
    peak = parameters["amplitude"]
    samples = []
    for i in range(n + 1):
        distance = max(edge - min(i, n - i) * interval, 0)
        envelope = (math.exp(-0.5 * (distance / sigma) ** 2) - baseline) / denominator
        samples.append(round(peak * max(0, min(1, envelope)) * 0x3FFF))
    samples[0] = samples[-1] = 0
    return {"ram_step": step, "ram_asf": samples, "ram_sample_interval_s": interval}
