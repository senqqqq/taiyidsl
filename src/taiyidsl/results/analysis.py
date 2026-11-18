"""Offline fits and explicit calibration suggestions from shot-resolved data."""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

import numpy as np
from scipy.optimize import OptimizeWarning, curve_fit
from scipy.signal import lombscargle

from ..config.models import CalibrationProposal
from ..dsl._serialization import json_value
from ..dsl.pulses import Square
from ..dsl.values import SweepParameter
from ..units import as_fraction


class AnalysisError(ValueError):
    """Insufficient data, ambiguous metadata or an unsuccessful fit."""


@dataclass(frozen=True)
class FitResult:
    model: str
    parameters: Mapping[str, float]
    quality: Mapping[str, float]
    valid_counts: tuple[int, ...]
    source_experiment: str
    assumptions: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "model": self.model,
            "parameters": dict(self.parameters),
            "quality": dict(self.quality),
            "valid_counts": list(self.valid_counts),
            "source_experiment": self.source_experiment,
            "assumptions": list(self.assumptions),
        }


def _series(
    results, handle, qubit_id, axis, minimum_points, postselection=None, group_axes=()
):
    if qubit_id is None:
        if len(results.qubits) != 1:
            raise AnalysisError("Select qubit_id for multi-qubit results")
        qubit_id = results.qubits[0]
    qi = results.qubits.index(qubit_id)
    if not all(axis in coord for coord in results.coordinates):
        raise AnalysisError(f"Results have no {axis!r} coordinate")
    extra_axes = (
        set().union(*(coord.keys() for coord in results.coordinates))
        - {axis}
        - set(group_axes)
    )
    if any(
        len({coord.get(key) for coord in results.coordinates}) > 1 for key in extra_axes
    ):
        raise AnalysisError(
            "Select a single slice of additional sweep axes before fitting"
        )
    # Aggregate duplicate coordinates by valid shot counts, e.g. RB sequences.
    summary = results.aggregate(handle, postselection=postselection)
    grouped = {}
    for coord, y, n in zip(
        results.coordinates, summary["mean"][:, qi], summary["valid_counts"][:, qi]
    ):
        if n > 0 and np.isfinite(y):
            x = float(coord[axis])
            sy, sn = grouped.get(x, (0.0, 0))
            grouped[x] = (sy + float(y) * int(n), sn + int(n))
    if len(grouped) < minimum_points:
        raise AnalysisError(
            f"Need at least {minimum_points} distinct valid {axis} values"
        )
    x = np.array(sorted(grouped))
    n = np.array([grouped[t][1] for t in x], dtype=int)
    y = np.array([grouped[t][0] / grouped[t][1] for t in x])
    if not np.all(np.isfinite(x)) or x[0] < 0 or np.ptp(x) <= 0 or np.ptp(y) < 1e-8:
        raise AnalysisError(
            "Fit requires nonnegative varying coordinates and a nonconstant signal"
        )
    if np.any((y < 0) | (y > 1)):
        raise AnalysisError(
            "These fit models require classified population observations in [0, 1]"
        )
    sigma = np.sqrt(np.maximum(y * (1 - y), 0.05) / n)
    return qubit_id, x, y, n, sigma


def _frequencies(x, y):
    spacing = np.min(np.diff(x))
    upper = min(100.0, 0.5 / spacing)
    if upper <= 0.25:
        raise AnalysisError("Insufficient sampling for an oscillatory fit")
    grid = np.linspace(0.25, upper, max(600, int(upper * 60)))
    power = lombscargle(x, y - np.mean(y), 2 * np.pi * grid, normalize=False)
    candidates = []
    for idx in np.argsort(power)[::-1]:
        value = float(grid[idx])
        if all(abs(value - old) > 0.3 for old in candidates):
            candidates.append(value)
        if len(candidates) >= 5:
            break
    return candidates, upper


def _best_fit(func, x, y, sigma, guesses, bounds):
    best = None
    for initial in guesses:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", OptimizeWarning)
                values, covariance = curve_fit(
                    func,
                    x,
                    y,
                    p0=initial,
                    sigma=sigma,
                    absolute_sigma=True,
                    bounds=bounds,
                    maxfev=30000,
                )
            residual = y - func(x, *values)
            score = float(np.sum((residual / sigma) ** 2))
            if (
                np.all(np.isfinite(values))
                and np.all(np.isfinite(covariance))
                and (best is None or score < best[0])
            ):
                best = score, values, covariance, residual
        except (ValueError, RuntimeError, FloatingPointError, OptimizeWarning):
            continue
    if best is None:
        raise AnalysisError(
            "No finite, converged fit was found; no calibration was suggested"
        )
    _, values, covariance, residual = best
    variance = float(np.sum((y - y.mean()) ** 2))
    quality = {
        "r_squared": float(1 - np.sum(residual**2) / variance),
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "point_count": int(len(x)),
    }
    return values, covariance, quality


def _validate_rabi_program(results, experiment, qubit, handle, axis):
    """Restrict calibration updates to the fit model's actual pulse family.

    Names and qubit snapshots alone cannot identify a program: a same-named
    experiment could have different amplitudes, echoes or unrelated pulses.
    """
    if not hasattr(experiment, "root"):
        raise AnalysisError("Rabi calibration requires the original Experiment IR")

    def walk(node):
        yield node
        for child in node.children:
            yield from walk(child)

    nodes = list(walk(experiment.root))
    allowed = {
        "experiment",
        "average",
        "sweep",
        "serial",
        "realtime",
        "prepare",
        "reset_frame",
        "reset_phase",
        "play",
        "measure",
    }
    if any(node.kind not in allowed for node in nodes):
        raise AnalysisError(
            "Built-in Rabi calibration requires a single square-pulse program"
        )
    pulses = [node for node in nodes if node.kind == "play"]
    sweeps = [node for node in nodes if node.kind == "sweep"]
    measurements = [node for node in nodes if node.kind == "measure"]
    if len(pulses) != 1 or len(sweeps) != 1 or len(measurements) != 1:
        raise AnalysisError(
            "Built-in Rabi calibration requires one duration sweep, "
            "one pulse and one measurement"
        )
    pulse = pulses[0].params["pulse"]
    if (
        pulses[0].params["signal"] != qubit.raman
        or not isinstance(pulse, Square)
        or not isinstance(pulse.duration, SweepParameter)
        or pulse.duration.name != axis
        or pulse.duration.dimension != "time"
        or sweeps[0].params["parameter"] != pulse.duration
    ):
        raise AnalysisError(
            "Rabi duration must sweep the selected qubit's Raman square pulse"
        )
    amplitude = (
        qubit.params.raman.amplitude if pulse.amplitude is None else pulse.amplitude
    )
    frequency = (
        qubit.params.raman.frequency if pulse.frequency is None else pulse.frequency
    )
    try:
        amplitude = float(as_fraction(amplitude, "dimensionless"))
        frequency = float(as_fraction(frequency, "frequency"))
        float(as_fraction(pulse.phase, "phase"))
    except (TypeError, ValueError) as exc:
        raise AnalysisError(
            "Rabi calibration requires static amplitude, frequency and phase"
        ) from exc
    if (
        amplitude != qubit.params.raman.amplitude
        or frequency != qubit.params.raman.frequency
    ):
        raise AnalysisError(
            "Rabi control settings differ from the stored profile; "
            "do not overwrite its pulse durations"
        )
    acquisition = measurements[0].params
    if acquisition != {
        "qubit": qubit.uid,
        "protocol": "state_readout",
        "handle": handle,
    }:
        raise AnalysisError(
            "Built-in Rabi calibration requires the selected "
            "state_readout protocol and handle"
        )
    for node in nodes:
        if node.kind == "prepare" and node.params != {
            "qubit": qubit.uid,
            "protocol": "standard",
        }:
            raise AnalysisError(
                "Built-in Rabi calibration requires the standard preparation protocol"
            )
        if node.kind in {"reset_frame", "reset_phase"} and (
            node.params.get("frame") != qubit.frame
        ):
            raise AnalysisError("Rabi frame belongs to another qubit")
    digest = sha256(
        json.dumps(
            json_value(experiment.root), sort_keys=True, allow_nan=False
        ).encode()
    ).hexdigest()
    if results.provenance.get("experiment_ir_hash") != digest:
        raise AnalysisError(
            "Results do not match the original compiled experiment IR hash"
        )
    if [float(coord[axis]) for coord in results.coordinates] != [
        float(v) for v in pulse.duration.values
    ]:
        raise AnalysisError(
            "Result coordinates do not match the compiled duration sweep"
        )
    return digest


def analyze_rabi(
    results,
    *,
    experiment,
    handle="population",
    qubit_id=None,
    axis="duration",
    postselection=None,
):
    """Suggest square-pulse pi/pi2 durations. Does not modify a Qubit or file."""
    uid, x, y, counts, sigma = _series(
        results, handle, qubit_id, axis, 8, postselection
    )
    if uid not in experiment.qubits:
        raise AnalysisError("Results qubit is absent from the experiment snapshot")
    q = experiment.qubits[uid]
    metadata = getattr(experiment, "metadata", {})
    if (
        metadata.get("calibration_amplitude", q.params.raman.amplitude)
        != q.params.raman.amplitude
        or metadata.get("calibration_frequency", q.params.raman.frequency)
        != q.params.raman.frequency
    ):
        raise AnalysisError(
            "Rabi control settings differ from the stored profile; "
            "do not overwrite its pulse durations"
        )
    snapshot = results.qubit_snapshots.get(uid)
    if snapshot is not None and snapshot != q.to_dict():
        raise AnalysisError(
            "Results and calibration experiment use different Qubit snapshots"
        )
    if results.metadata.get("experiment_name", experiment.name) != experiment.name:
        raise AnalysisError("Results belong to a different experiment")
    ir_hash = _validate_rabi_program(results, experiment, q, handle, axis)
    scale = float(np.ptp(x))
    xn = x / scale
    candidates, upper = _frequencies(xn, y)

    def model(t, offset, amplitude, frequency):
        return offset + amplitude * np.cos(2 * np.pi * frequency * t)

    guesses = [[float(y.mean()), -float(np.ptp(y)) / 2, f] for f in candidates]
    values, covariance, quality = _best_fit(
        model, xn, y, sigma, guesses, ([0, -1, 0.01], [1, 1, upper])
    )
    pi_duration = scale / (2 * values[2])
    quality.update(
        {
            "pi_duration_std": float(
                scale * np.sqrt(covariance[2, 2]) / (2 * values[2] ** 2)
            ),
            "relative_uncertainty": float(np.sqrt(covariance[2, 2]) / values[2]),
            "valid_shots": int(counts.sum()),
            "min_valid_shots": int(counts.min()),
        }
    )
    return CalibrationProposal(
        qubit_id=uid,
        base_revision=q.revision,
        base_fingerprint=q.fingerprint,
        updates={
            "raman.pi_duration": float(pi_duration),
            "raman.pi2_duration": float(pi_duration / 2),
        },
        units={"raman.pi_duration": "s", "raman.pi2_duration": "s"},
        quality=quality,
        source_experiment=experiment.name,
        success=True,
        analysis_metadata={
            "coordinate_axis": axis,
            "coordinate_values": x.tolist(),
            "valid_counts": counts.tolist(),
            "postselection": dict(postselection or {}),
            "source_ir_hash": ir_hash,
            "source_provenance": results.provenance,
        },
    )


def analyze_ramsey(
    results, *, handle="population", qubit_id=None, axis="tau", postselection=None
):
    """Fit absolute fringe frequency and T2*. Never infer a signed correction."""
    _, x, y, counts, sigma = _series(results, handle, qubit_id, axis, 10, postselection)
    scale = float(np.ptp(x))
    xn = x / scale
    candidates, upper = _frequencies(xn, y)

    def model(t, offset, contrast, t2, frequency, phase):
        return offset + contrast * np.exp(-t / t2) * np.cos(
            2 * np.pi * frequency * t + phase
        )

    guesses = [
        [0.5, min(0.49, max(0.1, float(np.ptp(y)) / 2)), 1.0, f, phase]
        for f in candidates
        for phase in (0.0, np.pi)
    ]
    values, covariance, quality = _best_fit(
        model,
        xn,
        y,
        sigma,
        guesses,
        ([0, 0, 0.01, 0.01, -2 * np.pi], [1, 1, 1000, upper, 2 * np.pi]),
    )
    quality["valid_shots"] = int(counts.sum())
    return FitResult(
        "ramsey",
        {
            "offset": float(values[0]),
            "contrast": float(values[1]),
            "t2star": float(values[2] * scale),
            "fringe_frequency_abs": float(values[3] / scale),
            "phase": float(values[4]),
            "t2star_std": float(np.sqrt(covariance[2, 2]) * scale),
        },
        quality,
        tuple(int(n) for n in counts),
        results.metadata.get("experiment_name", "unknown"),
        (
            "Exponential envelope and a single sinusoidal fringe.",
            "Fringe frequency alone does not identify the sign of a "
            "resonance-frequency correction.",
        ),
    )


def analyze_rb(
    results, *, handle="population", qubit_id=None, axis="length", postselection=None
):
    """Fit A*p**m+B after aggregating sequences by valid shot counts."""
    _, x, y, counts, sigma = _series(
        results, handle, qubit_id, axis, 5, postselection, group_axes=("sequence",)
    )

    def model(length, amplitude, p, offset):
        return amplitude * p**length + offset

    guesses = [[0.45, p, 0.5] for p in (0.9, 0.95, 0.98, 0.995, 0.999)]
    values, covariance, quality = _best_fit(
        model, x, y, sigma, guesses, ([0, 0, 0], [1, 1, 1])
    )
    quality["valid_shots"] = int(counts.sum())
    return FitResult(
        "rb",
        {
            "amplitude": float(values[0]),
            "p": float(values[1]),
            "offset": float(values[2]),
            "p_std_shot_only": float(np.sqrt(covariance[1, 1])),
            "error_per_clifford": float((1 - values[1]) / 2),
        },
        quality,
        tuple(int(n) for n in counts),
        results.metadata.get("experiment_name", "unknown"),
        (
            "Single-qubit Clifford RB decay model A*p**m+B.",
            "p_std_shot_only uses independent shot noise and excludes "
            "between-sequence variation; it is not a sequence-aware "
            "confidence interval.",
            "Error-per-Clifford interpretation assumes the applicable RB noise/SPAM "
            "model; loss/postselection may bias it.",
        ),
    )
