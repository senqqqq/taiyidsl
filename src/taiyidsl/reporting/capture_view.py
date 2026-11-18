"""Bounded, offline comparison of captured voltages with final packed DAC samples.

Time alignment is declared by the capture, never inferred for acceptance. The
optional fitted overlay is exploratory and leaves all unadjusted errors intact.
No analog transfer function, instrument driver or quantum-state model is used.
"""

from __future__ import annotations

import html
import json
import math
import zipfile
from pathlib import Path

import numpy as np

from .._artifact_io import file_sha256
from .output_simulator import simulate_output


def _number(value, name, *, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"Capture {name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"Capture {name} must be positive")
    return float(value)


def _capture(path, metadata, max_samples):
    if type(max_samples) is not int or max_samples < 4:
        raise ValueError("max_samples must be an integer >= 4")
    required = {
        "artifact_sha256",
        "point",
        "shot",
        "attempt",
        "run_id",
        "group",
        "card",
        "channel",
        "sample_rate_hz",
        "time_origin_s",
        "deskew_ns",
        "calibration_revision",
        "synthetic",
    }
    if not required <= metadata.keys():
        raise ValueError(
            f"Missing capture metadata: {sorted(required - metadata.keys())}"
        )
    values = dict(metadata)
    for key in ("point", "shot", "channel", "attempt"):
        if type(values[key]) is not int or values[key] < 0:
            raise ValueError(f"Capture {key} must be a nonnegative integer")
    for key in ("artifact_sha256", "group", "card", "run_id", "calibration_revision"):
        if not isinstance(values[key], str) or not values[key]:
            raise ValueError(f"Capture {key} must be nonempty text")
    if type(values["synthetic"]) is not bool:
        raise ValueError("Capture synthetic must explicitly be true or false")
    rate = _number(values["sample_rate_hz"], "sample_rate_hz", positive=True)
    origin = _number(values["time_origin_s"], "time_origin_s")
    deskew = _number(values["deskew_ns"], "deskew_ns") * 1e-9
    path = Path(path)
    # Reject oversized decompressed array members before NumPy allocates them.
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if (
                member.filename
                in {
                    "voltage.npy",
                    "time_s.npy",
                    "envelope.npy",
                    "reference_phase_rad.npy",
                }
                and member.file_size > 8 * max_samples + 65536
            ):
                raise ValueError(
                    "Capture exceeds the sample budget; export a shorter window"
                )
    with np.load(path, allow_pickle=False) as arrays:
        if "voltage" not in arrays:
            raise ValueError("Capture NPZ requires a voltage array")
        voltage = np.asarray(arrays["voltage"], dtype=float)
        if (
            voltage.ndim != 1
            or not 4 <= voltage.size <= max_samples
            or not np.all(np.isfinite(voltage))
        ):
            raise ValueError("Capture voltage must be a finite bounded 1-D trace")
        raw_time = origin + np.arange(voltage.size) / rate
        if "time_s" in arrays:
            declared = np.asarray(arrays["time_s"], dtype=float)
            if declared.shape != raw_time.shape or not np.allclose(
                declared, raw_time, atol=1e-4 / rate, rtol=0
            ):
                raise ValueError(
                    "Capture times disagree with the declared sampling clock/origin"
                )
        fit = None
        if {"envelope", "reference_phase_rad"} <= set(arrays.files):
            from ..spectrum_calibration import fit_rf_trace

            fit = fit_rf_trace(
                voltage,
                arrays["envelope"],
                arrays["reference_phase_rad"],
                phase_uncertainty_deg=values["phase_uncertainty_deg"],
            )
    return raw_time - deskew, voltage, values, fit


def _point_artifact(artifact, point):
    if hasattr(artifact, "load_chunk"):
        descriptor = artifact.chunk_for_point(point)
        compiled = artifact.load_chunk(descriptor["index"])
        return compiled, descriptor["points"].index(point), artifact.sha256
    artifact.assert_integrity()
    return artifact, point, artifact.sha256


def compare_capture(
    artifact,
    capture_npz,
    metadata,
    *,
    max_samples=100_000,
    exploratory_alignment=False,
    max_lag_samples=32,
):
    """Compare one capture window, preserving all declared references.

    ``time_origin_s`` is sample zero relative to compiled shot T0. Positive
    ``deskew_ns`` is a separately measured instrument/cable delay to subtract.
    The ideal reference is zero-order-held final DAC code at the capture times;
    it is not a model of the analog reconstruction filter or optical field.
    """
    times, voltage, meta, phase_fit = _capture(capture_npz, metadata, max_samples)
    compiled, point, digest = _point_artifact(artifact, meta["point"])
    if meta["artifact_sha256"] != digest:
        raise ValueError("Capture belongs to another compiled artifact")
    if meta["shot"] >= compiled.shots:
        raise ValueError("Capture shot is outside the compiled average")
    group = compiled.setup.spectrum.groups.get(meta["group"])
    if group is None or meta["card"] not in group.cards:
        raise ValueError("Capture group/card is absent from the artifact")
    if meta["calibration_revision"] != group.calibration.get("revision"):
        raise ValueError(
            "Capture calibration revision differs from the compiled artifact"
        )
    rate = group.cards[meta["card"]].sample_rate_hz
    begin = float(times[0] - 1 / rate)
    end = float(times[-1] + 2 / rate)
    decoded = simulate_output(
        compiled, point, begin, end - begin, max_samples=max_samples + 4
    )
    matches = [
        trace
        for trace in decoded["traces"]
        if (trace["group"], trace["card"], trace["channel"])
        == (meta["group"], meta["card"], meta["channel"])
    ]
    if len(matches) != 1 or not len(matches[0]["time_s"]):
        raise ValueError("Capture does not identify one finite DAC output")
    trace = matches[0]
    reference_time = trace["time_s"]
    if (
        times[0] < reference_time[0] - 1e-6 / rate
        or times[-1] >= reference_time[-1] + 1 / rate
    ):
        raise ValueError("Capture window extends outside the finite DAC program")
    indices = np.searchsorted(reference_time, times + 1e-6 / rate, side="right") - 1
    if np.any(indices < 0) or np.any(indices >= len(reference_time)):
        raise ValueError("Capture cannot be placed on the declared DAC timebase")
    predicted = trace["voltage_v"][indices]
    residual = voltage - predicted
    result = {
        "schema_version": 1,
        "metadata": meta,
        "capture_sha256": file_sha256(capture_npz),
        "time_s": times,
        "measured_voltage_v": voltage,
        "predicted_voltage_v": predicted,
        "dac_codes": trace["dac_codes"][indices],
        "residual_voltage_v": residual,
        "raw_residual_rms_v": float(np.sqrt(np.mean(residual**2))),
        "raw_residual_max_v": float(np.max(np.abs(residual))),
        "reference": "final_packed_DAC_zero_order_hold_nominal_voltage",
        "alignment": "declared_shot_T0_and_fixed_deskew_only",
        "physical_acceptance": "not_evaluated",
        "measurement_kind": "synthetic" if meta["synthetic"] else "instrument_capture",
        "phase_fit": phase_fit,
    }
    if exploratory_alignment:
        if type(max_lag_samples) is not int or not 0 <= max_lag_samples <= min(
            256, len(times) // 4
        ):
            raise ValueError(
                "Exploratory lag must be <=256 and at most a quarter trace"
            )
        best = None
        for lag in range(-max_lag_samples, max_lag_samples + 1):
            left, right = max(0, lag), min(len(times), len(times) + lag)
            basis = np.column_stack(
                (predicted[left - lag : right - lag], np.ones(right - left))
            )
            coefficients, _, rank, _ = np.linalg.lstsq(
                basis, voltage[left:right], rcond=None
            )
            if rank < 2:
                continue
            fitted = basis @ coefficients
            error = float(np.mean((voltage[left:right] - fitted) ** 2))
            if best is None or error < best[0]:
                best = (error, lag, left, right, coefficients, fitted)
        if best is not None:
            error, lag, left, right, coefficients, fitted = best
            result["exploratory_fit"] = {
                "time_shift_s": lag / meta["sample_rate_hz"],
                "gain": float(coefficients[0]),
                "offset_v": float(coefficients[1]),
                "rms_v": math.sqrt(error),
                "used_for_acceptance": False,
                "time_s": times[left:right],
                "voltage_v": fitted,
            }
    return result


def write_capture_comparison(artifact, capture_npz, metadata, output, **options):
    """Write a self-contained interactive HTML and a provenance/metrics JSON."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    result = compare_capture(artifact, capture_npz, metadata, **options)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        subplot_titles=(
            "Nominal DAC voltage / captured voltage",
            "Raw residual (fixed deskew only)",
            "Final DAC codes",
        ),
    )
    times_us = result["time_s"] * 1e6
    for key, name in (
        ("predicted_voltage_v", "DAC reference"),
        ("measured_voltage_v", "Capture"),
    ):
        figure.add_trace(
            go.Scatter(x=times_us, y=result[key], name=name, mode="lines"), row=1, col=1
        )
    figure.add_trace(
        go.Scatter(x=times_us, y=result["residual_voltage_v"], name="Raw residual"),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=times_us, y=result["dac_codes"], name="DAC code"), row=3, col=1
    )
    if "exploratory_fit" in result:
        fit = result["exploratory_fit"]
        figure.add_trace(
            go.Scatter(
                x=fit["time_s"] * 1e6,
                y=fit["voltage_v"],
                name="Exploratory fit (not acceptance)",
                line={"dash": "dot"},
            ),
            row=1,
            col=1,
        )
    label = "SYNTHETIC CAPTURE" if metadata["synthetic"] else "INSTRUMENT CAPTURE"
    figure.update_layout(
        title=f"{label} — physical acceptance not evaluated", height=850
    )
    figure.update_xaxes(title_text="Time relative to shot T0 (µs)", row=3, col=1)
    figure.update_yaxes(title_text="V", row=1, col=1)
    figure.update_yaxes(title_text="V", row=2, col=1)
    figure.update_yaxes(title_text="code", row=3, col=1)
    summary = {
        key: value
        for key, value in result.items()
        if not isinstance(value, np.ndarray) and key != "exploratory_fit"
    }
    if "exploratory_fit" in result:
        summary["exploratory_fit"] = {
            k: v
            for k, v in result["exploratory_fit"].items()
            if not isinstance(v, np.ndarray)
        }
    source = figure.to_html(include_plotlyjs=True, full_html=True)
    details = (
        "<details><summary>Capture provenance and unadjusted errors</summary><pre>"
        + html.escape(
            json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)
        )
        + "</pre></details>"
    )
    source = source.replace("</body>", details + "</body>")
    target.write_text(source, encoding="utf-8")
    sidecar = target.with_suffix(".json")
    sidecar.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return {"html": target, "summary": sidecar}
