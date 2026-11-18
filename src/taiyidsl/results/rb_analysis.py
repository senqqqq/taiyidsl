"""RB decay fits and paired sequence bootstrap; never a hardware calibration.

IRB estimates and the separate systematic bound follow Magesan et al., PRL 109,
080505 (2012), Eqs. (4)-(5), https://arxiv.org/abs/1203.4550. The bound assumes
the paper's noise model. Statistical intervals do not remove model bias, loss
postselection bias or an uncalibrated implementation of the intended CZ.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy.optimize import OptimizeWarning, curve_fit

from .analysis import AnalysisError


@dataclass(frozen=True)
class RBDecayFit:
    variant: str
    dimension: int
    amplitude: float
    alpha: float
    offset: float
    epc: float
    lengths: tuple[int, ...]
    probabilities: tuple[float, ...]
    sequence_counts: tuple[int, ...]
    rmse: float
    reduced_chi_squared: float

    def to_dict(self):
        return {
            "variant": self.variant,
            "dimension": self.dimension,
            "model": "A * alpha**length + B",
            "A": self.amplitude,
            "alpha": self.alpha,
            "B": self.offset,
            "epc": self.epc,
            "lengths": list(self.lengths),
            "probabilities": list(self.probabilities),
            "sequence_counts": list(self.sequence_counts),
            "rmse": self.rmse,
            "reduced_chi_squared": self.reduced_chi_squared,
            "quality_flags": [
                label
                for condition, label in (
                    (self.amplitude < 0, "increasing_decay"),
                    (self.reduced_chi_squared > 5, "single_exponential_fit_is_poor"),
                )
                if condition
            ],
            "averaging": "equal sequence weight, not pooled-shot weight",
        }


def _validate_records(records, *, allow_duplicates=False):
    if not records:
        raise AnalysisError("RB fit requires per-sequence joint counts")
    if len({record.qubits for record in records}) != 1:
        raise AnalysisError("RB records must have the same ordered qubits")
    if len({(r.loss_policy, r.invalid_policy) for r in records}) != 1:
        raise AnalysisError("RB outcome policies must be consistent")
    keys = [(r.length, r.sequence, r.variant) for r in records]
    if not allow_duplicates and len(keys) != len(set(keys)):
        raise AnalysisError("Duplicate RB sequence records")
    if any(r.denominator == 0 for r in records):
        raise AnalysisError("An RB sequence has no included shots")


def _fit(records, variant, *, allow_duplicates=False):
    _validate_records(records, allow_duplicates=allow_duplicates)
    records = [record for record in records if record.variant == variant]
    if not records:
        raise AnalysisError(f"No {variant} RB observations")
    lengths = sorted({record.length for record in records})
    if len(lengths) < 4:
        raise AnalysisError("RB fit needs at least four distinct lengths")
    grouped = [[r for r in records if r.length == length] for length in lengths]
    y = np.array([np.mean([r.probability for r in group]) for group in grouped])
    if np.ptp(y) < 1e-8:
        raise AnalysisError("RB decay is constant; alpha is not identifiable")
    # Each random sequence is one statistical cluster. Weighting includes the
    # observed cluster scatter and a finite-shot floor, including endpoint data.
    variances = []
    for group in grouped:
        p = np.array([r.probability for r in group])
        shot_floor = np.mean(
            [
                max(r.probability * (1 - r.probability), 0.25 / r.denominator)
                / r.denominator
                for r in group
            ]
        )
        cluster_var = np.var(p, ddof=1) if len(group) > 1 else 0.0
        variances.append(max(cluster_var, shot_floor) / len(group))
    sigma = np.sqrt(variances)
    x = np.asarray(lengths, dtype=float)
    best = None
    for alpha in (0.5, 0.9, 0.98, 0.995):
        initial = (max(0.05, y[0] - y[-1]), alpha, min(y[-1], 0.9))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", OptimizeWarning)
                parameters, covariance = curve_fit(
                    lambda m, a, p, b: a * p**m + b,
                    x,
                    y,
                    p0=initial,
                    sigma=sigma,
                    absolute_sigma=True,
                    bounds=([-1, 1e-8, 0], [1, 1, 1]),
                    maxfev=20000,
                )
            residual = y - (parameters[0] * parameters[1] ** x + parameters[2])
            score = float(np.sum((residual / sigma) ** 2))
            if np.all(np.isfinite(covariance)) and (best is None or score < best[0]):
                best = score, parameters, residual
        except (ValueError, RuntimeError, OptimizeWarning, FloatingPointError):
            continue
    if best is None:
        raise AnalysisError("RB decay fit did not converge to finite parameters")
    score, (amplitude, alpha, offset), residual = best
    if abs(amplitude) < 1e-6 or alpha < 1e-6 or alpha > 1 - 1e-8:
        raise AnalysisError("RB decay fit is at an unidentifiable parameter boundary")
    dimension = 2 ** len(records[0].qubits)
    return RBDecayFit(
        variant,
        dimension,
        float(amplitude),
        float(alpha),
        float(offset),
        float((dimension - 1) * (1 - alpha) / dimension),
        tuple(lengths),
        tuple(float(v) for v in y),
        tuple(len(group) for group in grouped),
        float(np.sqrt(np.mean(residual**2))),
        score / (len(lengths) - 3),
    )


def fit_rb_decay(records, *, variant="reference"):
    """Fit A and B freely; B is not fixed to the maximally mixed population."""
    return _fit(tuple(records), variant)


def interleaved_error_bound(alpha_reference, alpha_interleaved, *, dimension=4):
    """Return the raw IRB estimate and Eq. (5) systematic half-width separately.

    Negative estimates and intervals extending outside [0, 1] are preserved.
    The caller must inspect model assumptions rather than silently clipping.
    """
    p, pc, d = float(alpha_reference), float(alpha_interleaved), dimension
    if type(d) is not int or d not in (2, 4):
        raise ValueError("RB dimension must be 2 or 4")
    if not (np.isfinite(p) and np.isfinite(pc) and 0 < p <= 1 and 0 <= pc <= 1):
        raise ValueError(
            "IRB requires 0 < alpha_reference <= 1 and 0 <= alpha_int <= 1"
        )
    estimate = (d - 1) / d * (1 - pc / p)
    first = (d - 1) / d * (abs(p - pc / p) + (1 - p))
    second = (
        2 * (d * d - 1) * (1 - p) / (p * d * d)
        + 4 * np.sqrt(1 - p) * np.sqrt(d * d - 1) / p
    )
    bound = float(min(first, second))
    return {
        "estimate": float(estimate),
        "systematic_half_width": bound,
        "systematic_interval": [float(estimate - bound), float(estimate + bound)],
        "source": "https://arxiv.org/abs/1203.4550, Eqs. (4)-(5)",
        "assumptions": "Markovian noise with sufficiently small gate dependence",
        "negative_estimate": bool(estimate < 0),
    }


def analyze_rb(records, *, bootstrap_samples=200, confidence=0.95, seed=0):
    """Fit RB and resample sequence IDs, preserving reference/interleaved pairs.

    Resampling is stratified by length. All shot counts from a selected sequence
    stay together; shots are not treated as independent random circuits. The
    output retains input counts and loss policies. Failed bootstrap fits are
    reported and intervals require at least 80% successful replicates.
    """
    records = tuple(records)
    _validate_records(records)
    if type(bootstrap_samples) is not int or bootstrap_samples < 20:
        raise ValueError("bootstrap_samples must be an integer >= 20")
    if (
        isinstance(confidence, bool)
        or not np.isfinite(confidence)
        or not 0 < confidence < 1
    ):
        raise ValueError("confidence must lie strictly between zero and one")
    if type(seed) is not int or seed < 0:
        raise ValueError("bootstrap seed must be a nonnegative integer")
    variants = {r.variant for r in records}
    if "reference" not in variants:
        raise AnalysisError("RB analysis requires reference observations")
    paired = "interleaved" in variants
    fits = {variant: _fit(records, variant) for variant in sorted(variants)}
    clusters = {}
    for record in records:
        clusters.setdefault(record.length, {}).setdefault(record.sequence, {})[
            record.variant
        ] = record
    if any(
        set(pair) != variants for block in clusters.values() for pair in block.values()
    ):
        raise AnalysisError("Every length/sequence needs its matching RB variants")
    if any(len(block) < 2 for block in clusters.values()):
        raise AnalysisError(
            "Sequence bootstrap needs at least two sequences per length"
        )
    rng = np.random.default_rng(seed)
    values = {
        f"{variant}_{parameter}": []
        for variant in sorted(variants)
        for parameter in ("epc", "alpha")
    }
    if paired:
        values["cz_error"] = []
    failures = 0
    for _ in range(bootstrap_samples):
        sample = []
        for length in sorted(clusters):
            pairs = [clusters[length][key] for key in sorted(clusters[length])]
            for index in rng.integers(0, len(pairs), size=len(pairs)):
                sample.extend(pairs[int(index)].values())
        try:
            replicate = {
                variant: _fit(sample, variant, allow_duplicates=True)
                for variant in sorted(variants)
            }
        except AnalysisError:
            failures += 1
            continue
        for variant, fit in replicate.items():
            values[f"{variant}_epc"].append(fit.epc)
            values[f"{variant}_alpha"].append(fit.alpha)
        if paired:
            values["cz_error"].append(
                interleaved_error_bound(
                    replicate["reference"].alpha, replicate["interleaved"].alpha
                )["estimate"]
            )
    enough = bootstrap_samples - failures >= 0.8 * bootstrap_samples
    tail = (1 - confidence) / 2
    intervals = {
        key: [float(x) for x in np.quantile(value, [tail, 1 - tail])]
        if enough
        else None
        for key, value in values.items()
    }
    irb = (
        interleaved_error_bound(fits["reference"].alpha, fits["interleaved"].alpha)
        if paired
        else None
    )
    return {
        "fits": {key: fit.to_dict() for key, fit in fits.items()},
        "interleaved_cz": irb,
        "bootstrap": {
            "method": "paired sequence clusters stratified by length"
            if paired
            else "sequence clusters stratified by length",
            "seed": seed,
            "requested": bootstrap_samples,
            "successful": bootstrap_samples - failures,
            "failed": failures,
            "confidence": confidence,
            "status": "passed" if enough else "insufficient_successful_fits",
            "statistical_intervals": intervals,
        },
        "observations": [record.to_dict() for record in records],
        "assumptions": [
            "Classified same-shot joint bits represent the prepared zero state",
            "A single exponential is an adequate description of the RB decay",
            "Sequence pairs share the same frozen random Clifford lists",
            "Statistical intervals are separate from the IRB systematic bound",
            "Loss exclusion changes the estimand and can bias a gate-error estimate",
        ],
        "hardware_validation": "not_established_by_analysis",
    }
