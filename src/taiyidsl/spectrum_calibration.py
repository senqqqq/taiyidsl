"""Offline calibration and acceptance analysis for Spectrum output connectors.

Reference phase must come from a Kasli-referenced measurement timebase.  These
functions never infer synchronisation from a free-running oscilloscope clock,
open an instrument, or discard per-record phase/delay residuals after fitting.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path

import numpy as np


def wrap_degrees(angle):
    return (np.asarray(angle) + 180.0) % 360.0 - 180.0


def _finite(value, name, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or (nonnegative and value < 0):
        raise ValueError(f"{name} must be finite and nonnegative")
    return float(value)


def _digest(records):
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def fit_rf_trace(
    voltage,
    envelope,
    reference_phase_rad,
    *,
    phase_uncertainty_deg,
    threshold_fraction=0.1,
):
    """Estimate transfer gain and phase against a complete, known RF model.

    ``reference_phase_rad`` includes the reference epoch, carrier and requested
    modulation.  The fit estimates a phase ERROR, which is returned unremoved.
    It also returns the residual to the unadjusted reference model.  A constant
    offset is fitted as a nuisance parameter, but is not silently removed from
    the reported raw waveform error.  Envelope delay is measured separately.
    """
    uncertainty = _finite(
        phase_uncertainty_deg, "phase_uncertainty_deg", nonnegative=True
    )
    if not 0 < threshold_fraction <= 1:
        raise ValueError("threshold_fraction must lie in (0, 1]")
    y, env, phase = (
        np.asarray(value, dtype=float)
        for value in (voltage, envelope, reference_phase_rad)
    )
    if y.ndim != 1 or env.shape != y.shape or phase.shape != y.shape or y.size < 4:
        raise ValueError("RF trace arrays must be matching one-dimensional samples")
    if not all(np.all(np.isfinite(value)) for value in (y, env, phase)):
        raise ValueError("RF trace contains nonfinite values")
    if np.any(env < 0) or np.max(env) <= 0:
        raise ValueError("A nonnegative, nonzero expected envelope is required")
    mask = env >= threshold_fraction * np.max(env)
    if np.count_nonzero(mask) < 4:
        raise ValueError("Insufficient nonzero samples for phase estimation")
    basis = np.column_stack((env * np.cos(phase), env * np.sin(phase), np.ones(y.size)))
    coefficients, _, rank, singular = np.linalg.lstsq(basis[mask], y[mask], rcond=None)
    if rank != 3 or singular[-1] / singular[0] < 1e-8:
        raise ValueError("Reference record cannot resolve gain/phase/DC independently")
    cosine, sine, offset = coefficients
    gain = math.hypot(cosine, sine)
    if gain <= np.finfo(float).eps:
        raise ValueError("No measurable carrier amplitude")
    phase_error = math.degrees(math.atan2(-sine, cosine))
    raw_residual = y - basis[:, 0]
    fitted_residual = y - basis @ coefficients
    return {
        "gain": gain,
        "phase_error_deg": phase_error,
        "phase_uncertainty_deg": uncertainty,
        "dc_offset": float(offset),
        "raw_rf_residual_rms": float(np.sqrt(np.mean(raw_residual**2))),
        "rf_residual_rms": float(np.sqrt(np.mean(raw_residual**2))),
        "rf_residual_reference": "unaligned_target",
        "measurement_scope": "known_waveform_fit",
        "fit_residual_rms": float(np.sqrt(np.mean(fitted_residual**2))),
        "valid_phase_samples": int(np.count_nonzero(mask)),
        "sample_count": int(y.size),
        "phase_was_subtracted_for_acceptance": False,
    }


def load_capture_npz(path, metadata):
    """Read one capture without pickle; preserve its measured phase residual.

    Required arrays: ``voltage``, ``envelope``, ``reference_phase_rad``.
    Metadata supplies a separately measured envelope ``delay_ns``, fixed
    instrument ``deskew_ns``, uncertainties, trigger/rearm/lock IDs and time.
    Files are processed one capture at a time, not as a scan-sized RF array.
    """
    with np.load(Path(path), allow_pickle=False) as arrays:
        fit = fit_rf_trace(
            arrays["voltage"],
            arrays["envelope"],
            arrays["reference_phase_rad"],
            phase_uncertainty_deg=metadata["phase_uncertainty_deg"],
        )
    result = {**dict(metadata), **fit}
    if "measurement_scope" in metadata:
        result["measurement_scope"] = metadata["measurement_scope"]
    return result


def _validated_records(records, *, require_lock=True):
    if not isinstance(records, (list, tuple)) or not records:
        raise ValueError("At least one measurement record is required")
    result = []
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(
            record.get("channel"), str
        ):
            raise ValueError("Every record requires a channel name")
        item = dict(record)
        for key in ("phase_error_deg", "delay_ns", "time_s"):
            item[key] = _finite(record.get(key), key)
        for key in ("phase_uncertainty_deg", "timing_uncertainty_ns"):
            item[key] = _finite(record.get(key), key, nonnegative=True)
        item["deskew_ns"] = _finite(record.get("deskew_ns", 0.0), "deskew_ns")
        item["gain"] = _finite(record.get("gain", 1.0), "gain")
        if item["gain"] <= 0:
            raise ValueError("Measured transfer gain must be positive")
        keys = ["trigger_id", "rearm_id"]
        if require_lock or "lock_id" in item:
            keys.append("lock_id")
        for key in keys:
            if key not in item or not isinstance(item[key], (str, int)):
                raise ValueError(f"Measurement record requires {key}")
        result.append(item)
    return result


def resolve_channel_calibration(
    profile, card_id, channel, frequency_hz, *, require_measured=False
):
    """Select an exact measured frequency; never interpolate transfer phase.

    The per-channel timing/sample shift is common to the frequency table.
    Unmeasured offline profiles default to zero phase/delay and unity gain.
    A measured profile always requires an exact carrier match, including during
    offline compilation; a missing frequency must be measured separately.
    """
    name = f"{card_id}:{channel}"
    measured = profile.get("status") == "measured"
    if require_measured and not measured:
        raise ValueError(f"{name}: measured calibration is required")
    common = dict(profile.get("channels", {}).get(name, {}))
    if not measured:
        return {
            "phase_offset_rad": 0.0,
            "gain": 1.0,
            "delay_ns": 0.0,
            "phase_uncertainty_deg": 0.0,
            "timing_uncertainty_ns": 0.0,
            **common,
        }
    rate = _finite(common.get("sample_rate_hz"), "sample_rate_hz")
    delay = _finite(common.get("delay_ns"), "delay_ns")
    if rate <= 0 or (
        common.get("phase_reference") != "after_sample_shift"
        or type(common.get("sample_shift_samples")) is not int
        or common["sample_shift_samples"] != round(-delay * rate / 1e9)
    ):
        raise ValueError(f"{name}: phase calibration sample shift mismatch")
    points = common.get("frequency_points", [common])
    if not isinstance(points, list) or not all(isinstance(p, Mapping) for p in points):
        raise ValueError(f"{name}: invalid calibration frequency table")
    matches = [
        item for item in points if item.get("carrier_frequency_hz") == frequency_hz
    ]
    if len(matches) != 1:
        raise ValueError(f"{name}: carrier frequency lacks unique measured calibration")
    result = {
        **common,
        **{
            field: matches[0].get(field)
            for field in (
                "carrier_frequency_hz",
                "phase_offset_rad",
                "gain",
                "phase_uncertainty_deg",
            )
        },
    }
    for key in ("phase_offset_rad", "gain"):
        result[key] = _finite(result.get(key), key)
    for key in ("phase_uncertainty_deg", "timing_uncertainty_ns"):
        result[key] = _finite(result.get(key), key, nonnegative=True)
    if result["gain"] <= 0:
        raise ValueError(f"{name}: measured gain must be positive")
    return result


def calibrate_channels(
    records,
    *,
    revision,
    card_fingerprints,
    timing_calibration,
):
    """Estimate fixed corrections from a dedicated calibration dataset.

    First measure uncorrected extra output delay, independently deskewed, in
    ``timing_calibration[channel]`` (delay_ns, timing_uncertainty_ns, sample_rate_hz).
    Then move the complete RF pulse by round(-delay_ns * sample_rate_hz / 1e9)
    samples and collect these records, with sample_shift_applied and
    carrier_frequency_hz. Phase must be relative to the Kasli-derived target,
    with the measurement chain already phase-deskewed. No per-capture time or
    phase alignment may be subtracted from this target.

    phase_offset_rad corrects the RESIDUAL measured AFTER that sample shift,
    including fractional-delay carrier phase. Gain is measured transfer gain
    (render divides by it); delay_ns remains the ORIGINAL extra output delay.
    This two-step procedure avoids compensating one delay twice. It establishes
    a calibration estimate, not a passed independent stability test.
    """
    records = _validated_records(records, require_lock=False)
    locks = {str(row["lock_id"]) for row in records if "lock_id" in row}
    if len(locks) > 1 or (locks and any("lock_id" not in row for row in records)):
        raise ValueError(
            "A calibration dataset must describe one measured lock "
            "or omit lock_id throughout"
        )
    if not isinstance(revision, str) or not revision:
        raise ValueError("Calibration requires a nonempty revision")
    by_channel = defaultdict(list)
    for item in records:
        by_channel[item["channel"]].append(item)
    channels = {}
    for channel, samples in by_channel.items():
        card, output = channel.rsplit(":", 1)
        if output not in ("0", "1") or not card_fingerprints.get(card):
            raise ValueError(f"{channel}: hardware fingerprint required")
        timing = timing_calibration[channel]
        delay = _finite(timing.get("delay_ns"), "uncorrected delay_ns")
        rate = _finite(timing.get("sample_rate_hz"), "sample_rate_hz")
        timing_uncertainty = _finite(
            timing.get("timing_uncertainty_ns"),
            "timing_uncertainty_ns",
            nonnegative=True,
        )
        if rate <= 0:
            raise ValueError("sample_rate_hz must be positive")
        shift = round(-delay * rate / 1e9)
        residual_delay = delay + shift * 1e9 / rate
        by_frequency = defaultdict(list)
        for item in samples:
            if (
                type(item.get("sample_shift_applied")) is not int
                or item["sample_shift_applied"] != shift
            ):
                raise ValueError(
                    f"{channel}: phase calibration must follow the same sample shift"
                )
            measured_delay = item["delay_ns"] - item["deskew_ns"]
            if abs(measured_delay - residual_delay) > (
                item["timing_uncertainty_ns"] + timing_uncertainty + 1e-12
            ):
                raise ValueError(
                    f"{channel}: post-shift delay disagrees with timing calibration"
                )
            frequency = _finite(item.get("carrier_frequency_hz"), "carrier")
            if frequency <= 0:
                raise ValueError("Calibrate positive carrier frequencies")
            by_frequency[frequency].append(item)
        frequency_points = []
        for frequency, points in sorted(by_frequency.items()):
            angles = np.deg2rad([item["phase_error_deg"] for item in points])
            mean = np.mean(np.exp(1j * angles))
            if abs(mean) < 0.99:
                raise ValueError(
                    f"{channel}: unstable phase cannot define a fixed correction"
                )
            frequency_points.append(
                {
                    "carrier_frequency_hz": frequency,
                    "phase_offset_rad": -float(np.angle(mean)),
                    "gain": float(np.median([item["gain"] for item in points])),
                    "phase_uncertainty_deg": max(
                        item["phase_uncertainty_deg"] for item in points
                    ),
                }
            )
        channels[channel] = {
            "delay_ns": delay,
            "sample_rate_hz": rate,
            "sample_shift_samples": shift,
            "residual_delay_ns": residual_delay,
            "phase_reference": "after_sample_shift",
            "frequency_points": frequency_points,
            "timing_uncertainty_ns": max(
                timing_uncertainty,
                *(item["timing_uncertainty_ns"] for item in samples),
            ),
        }
        if len(frequency_points) == 1:
            channels[channel].update(frequency_points[0])
    return {
        "status": "measured",
        "revision": revision,
        "card_fingerprints": dict(card_fingerprints),
        # Optional capture provenance for offline validation, never used to run a shot.
        **({"measurement_lock_id": locks.pop()} if locks else {}),
        "channels": channels,
        "calibration_dataset_sha256": _digest(records),
        "physical_acceptance": "not_verified",
    }


def _waveform_evidence(item, calibration_phase_uncertainty):
    """Validate waveform evidence without inventing a lab's RF residual tolerance."""
    missing = []
    residual = limit = None
    if item.get("rf_residual_reference") != "unaligned_target":
        missing.append("RF residual is not referenced to the unaligned target")
    if "rf_residual_rms" not in item or "rf_residual_rms_limit" not in item:
        missing.append("missing full-waveform RF RMS residual and declared limit")
    else:
        residual = _finite(item["rf_residual_rms"], "rf_residual_rms", nonnegative=True)
        limit = _finite(item["rf_residual_rms_limit"], "rf_residual_rms_limit")
        if limit <= 0:
            raise ValueError("rf_residual_rms_limit must be positive")
    window_bound = None
    scope = item.get("measurement_scope")
    if scope == "phase_windows":
        windows = item.get("phase_window_bounds_deg")
        if not isinstance(windows, list) or not windows:
            missing.append("missing fixed-window phase bounds")
        else:
            window_bound = (
                max(
                    _finite(value, "phase_window_bounds_deg", nonnegative=True)
                    for value in windows
                )
                + calibration_phase_uncertainty
            )
        if item.get("phase_window_alignment_removed") is not False:
            missing.append("fixed windows lack proof of unchanged phase/time reference")
        if item.get("phase_windows_cover_effective_envelope") is not True:
            missing.append("fixed windows do not cover the complete effective envelope")
        threshold = item.get("phase_analysis_threshold_fraction")
        if not isinstance(threshold, (int, float)) or not 0 < threshold <= 0.1:
            missing.append("phase windows must cover at least the >=10% envelope")
    elif scope == "known_waveform_fit" and item.get("carrier_frequency_hz") == 30_000:
        # Six cycles cannot support reliable pointwise instantaneous-phase claims.
        # The approved nuclear-spin acceptance uses a complete known-model fit,
        # guarded by a separately declared unaligned whole-waveform residual limit.
        if item.get("phase_was_subtracted_for_acceptance") is not False:
            missing.append("known-waveform phase fit must retain its phase error")
    else:
        missing.append("missing pulse-resolved phase evidence for this carrier")
    return {
        "missing": missing,
        "scope": scope,
        "window_phase_bound_deg": window_bound,
        "rf_residual_rms": residual,
        "rf_residual_rms_limit": limit,
        "rf_limit_passed": residual is None or residual < limit,
    }


def analyze_measurements(
    records,
    calibration,
    *,
    corrections_applied=True,
    required_channels=None,
    lock_calibrations=None,
    phase_limit_deg=1.0,
    skew_limit_ns=5.0,
):
    """Judge independent output measurements without per-shot alignment removal.

    With corrections_applied=True, measurements must already be relative to
    the intended logical RF output after physical correction.  With False the
    fixed phase correction is applied analytically to captures already using
    the calibrated integer sample shift; that is a prediction and never
    constitutes physical acceptance. Per-record phase fits are retained as
    errors, not subtracted before judging the limit.

    Each capture additionally requires rf_residual_rms, rf_residual_rms_limit
    (same voltage units), and rf_residual_reference="unaligned_target". These
    compare the ENTIRE RF trace to its fixed target, without fitted gain, DC,
    phase or time subtraction; fit_residual_rms is diagnostic only.
    For measurement_scope="phase_windows", phase_window_bounds_deg contains an
    upper bound on maximum absolute residual + measurement uncertainty in every
    fixed analysis window, not the window's averaged or best-fit phase.
    Calibration uncertainty is added here. All windows share the original
    target reference (phase_window_alignment_removed=False), cover the complete
    effective envelope (phase_windows_cover_effective_envelope=True), and use a
    phase_analysis_threshold_fraction <=0.1. A scalar global fit cannot replace
    that evidence. Only 30 kHz permits measurement_scope="known_waveform_fit"
    with phase_was_subtracted_for_acceptance=False, still guarded by full RF RMS.
    """
    records = _validated_records(records)
    if type(corrections_applied) is not bool:
        raise TypeError("corrections_applied must be boolean")
    required = set(required_channels or calibration.get("channels", {}))
    if not required:
        raise ValueError("Specify the channels requiring acceptance")
    if not 0 < phase_limit_deg <= 180 or skew_limit_ns <= 0:
        raise ValueError("Invalid acceptance limits")
    channels, captures = defaultdict(list), defaultdict(dict)
    lock_calibrations = dict(lock_calibrations or {})
    unproven_locks = set()
    uncalibrated_frequencies = set()
    waveform_missing = defaultdict(set)
    for item in records:
        name = item["channel"]
        if name not in required:
            raise ValueError(f"Unexpected measured channel: {name}")
        record_calibration = lock_calibrations.get(str(item["lock_id"]), calibration)
        if name not in record_calibration.get("channels", {}):
            raise ValueError(f"Missing fixed calibration for {name}")
        if (
            str(item["lock_id"]) not in lock_calibrations
            or record_calibration.get("status") != "measured"
            or item.get("calibration_revision") != record_calibration.get("revision")
            or record_calibration.get("measurement_lock_id") != str(item["lock_id"])
            or not record_calibration.get("calibration_dataset_sha256")
        ):
            unproven_locks.add(item["lock_id"])
        card, channel = name.rsplit(":", 1)
        try:
            fixed = resolve_channel_calibration(
                record_calibration, card, int(channel), item.get("carrier_frequency_hz")
            )
        except ValueError:
            if not corrections_applied:
                raise
            uncalibrated_frequencies.add(name)
            fixed = {"phase_uncertainty_deg": 0.0, "timing_uncertainty_ns": 0.0}
        phase = item["phase_error_deg"]
        delay = item["delay_ns"] - item["deskew_ns"]
        if not corrections_applied:
            if item.get("sample_shift_applied") != fixed.get("sample_shift_samples"):
                raise ValueError(
                    "Phase prediction requires the calibrated sample shift"
                )
            phase += math.degrees(fixed["phase_offset_rad"])
        residual = {
            **item,
            "phase_residual_deg": float(wrap_degrees(phase)),
            "delay_residual_ns": delay,
            "phase_u95_deg": item["phase_uncertainty_deg"]
            + fixed["phase_uncertainty_deg"],
            "time_u95_ns": item["timing_uncertainty_ns"]
            + fixed["timing_uncertainty_ns"],
        }
        evidence = _waveform_evidence(item, fixed["phase_uncertainty_deg"])
        waveform_missing[name].update(evidence["missing"])
        residual["waveform_evidence"] = evidence
        channels[name].append(residual)
        key = (item["lock_id"], item["rearm_id"], item["trigger_id"])
        if name in captures[key]:
            raise ValueError("Duplicate channel within one physical capture")
        captures[key][name] = residual
    summaries = {}
    for name, rows in channels.items():
        phase = np.asarray([row["phase_residual_deg"] for row in rows])
        lock_spans = {}
        for lock in {row["lock_id"] for row in rows}:
            times = [row["time_s"] for row in rows if row["lock_id"] == lock]
            lock_spans[str(lock)] = max(times) - min(times)
        window_bounds = [
            row["waveform_evidence"]["window_phase_bound_deg"]
            for row in rows
            if row["waveform_evidence"]["window_phase_bound_deg"] is not None
        ]
        rf_ratios = [
            row["waveform_evidence"]["rf_residual_rms"]
            / row["waveform_evidence"]["rf_residual_rms_limit"]
            for row in rows
            if row["waveform_evidence"]["rf_residual_rms"] is not None
        ]
        summaries[name] = {
            "records": len(rows),
            "max_phase_error_deg": float(np.max(np.abs(phase))),
            "rms_phase_error_deg": float(np.sqrt(np.mean(phase**2))),
            "phase_bound_deg": max(
                [abs(row["phase_residual_deg"]) + row["phase_u95_deg"] for row in rows]
                + window_bounds
            ),
            "max_window_phase_bound_deg": max(window_bounds, default=None),
            "max_rf_residual_to_limit_ratio": max(rf_ratios, default=None),
            "measurement_scopes": sorted(
                {row["waveform_evidence"]["scope"] or "unspecified" for row in rows}
            ),
            "rearm_count": len({(row["lock_id"], row["rearm_id"]) for row in rows}),
            "longest_single_lock_span_s": max(lock_spans.values()),
            "gain_min": min(row["gain"] for row in rows),
            "gain_max": max(row["gain"] for row in rows),
        }
    pair_bounds, pair_counts = {}, defaultdict(int)
    for capture in captures.values():
        for a, b in itertools.combinations(sorted(capture), 2):
            left, right = capture[a], capture[b]
            bound = (
                abs(left["delay_residual_ns"] - right["delay_residual_ns"])
                + left["time_u95_ns"]
                + right["time_u95_ns"]
            )
            key = f"{a}|{b}"
            pair_bounds[key] = max(pair_bounds.get(key, 0), bound)
            pair_counts[key] += 1
    required_pairs = {
        f"{a}|{b}" for a, b in itertools.combinations(sorted(required), 2)
    }
    elapsed = max(row["time_s"] for row in records) - min(
        row["time_s"] for row in records
    )
    rearms = {(row["lock_id"], row["rearm_id"]) for row in records}
    locks = {row["lock_id"] for row in records}
    stable_spans = []
    for lock in locks:
        times = [row["time_s"] for row in records if row["lock_id"] == lock]
        stable_spans.append(max(times) - min(times))
    incomplete = []
    if set(channels) != required:
        incomplete.append("missing channels")
    if any(summaries.get(name, {}).get("records", 0) < 10_000 for name in required):
        incomplete.append("fewer than 10000 triggers per channel")
    if any(summaries.get(name, {}).get("rearm_count", 0) < 100 for name in required):
        incomplete.append("fewer than 100 stop/rearm cycles per channel")
    if any(
        summaries.get(name, {}).get("longest_single_lock_span_s", 0) < 1800
        for name in required
    ):
        incomplete.append("less than 30 minutes in one lock session per channel")
    if len(locks) < 3:
        incomplete.append("fewer than 3 independently recalibrated lock sessions")
    if unproven_locks:
        incomplete.append("missing independently bound calibration for lock sessions")
    if uncalibrated_frequencies:
        incomplete.append("missing exact-frequency or sample-shift calibration")
    measured_datasets = {
        lock_calibrations[str(lock)].get("calibration_dataset_sha256")
        for lock in locks
        if str(lock) in lock_calibrations and lock not in unproven_locks
    }
    if len(measured_datasets) < 3:
        incomplete.append("fewer than 3 independent per-lock calibration datasets")
    if any(pair_counts.get(pair, 0) < 10_000 for pair in required_pairs):
        incomplete.append("fewer than 10000 simultaneous captures per required pair")
    if any(waveform_missing.values()):
        incomplete.append("missing full-waveform or pulse-resolved phase evidence")
    if calibration.get("status") != "measured":
        incomplete.append("unmeasured calibration")
    if calibration.get("calibration_dataset_sha256") == _digest(records):
        incomplete.append("acceptance reuses its calibration dataset")
    if not corrections_applied:
        incomplete.append("analytical prediction without corrected hardware captures")
    passed_limits = (
        all(item["phase_bound_deg"] < phase_limit_deg for item in summaries.values())
        and all(value < skew_limit_ns for value in pair_bounds.values())
        and all(
            row["waveform_evidence"]["rf_limit_passed"]
            for rows in channels.values()
            for row in rows
        )
    )
    status = (
        "failed" if not passed_limits else "not_verified" if incomplete else "passed"
    )
    return {
        "schema_version": 1,
        "status": status,
        "limits": {"phase_deg": phase_limit_deg, "skew_ns": skew_limit_ns},
        "channels": summaries,
        "pair_skew_bound_ns": pair_bounds,
        "pair_capture_counts": dict(pair_counts),
        "capture_count": len(captures),
        "rearm_count": len(rearms),
        "lock_count": len(locks),
        "elapsed_s": elapsed,
        "longest_single_lock_span_s": max(stable_spans),
        "incomplete": incomplete,
        "waveform_evidence_missing": {
            name: sorted(reasons)
            for name, reasons in waveform_missing.items()
            if reasons
        },
        "measurement_sha256": _digest(records),
        "calibration_revision": calibration.get("revision"),
        "corrections_applied": corrections_applied,
        "per_record_alignment_removed": False,
    }


def select_trigger_window(
    records,
    *,
    period_mu,
    required_cards,
    min_records_per_phase=100,
    max_delay_spread_ns=0.4,
    max_phase_spread_deg=0.5,
):
    """Select the center of the widest shared stable circular RTIO phase window."""
    if type(period_mu) is not int or not 0 < period_mu <= 100_000:
        raise ValueError("period_mu must be a positive bounded integer")
    if not required_cards or len(set(required_cards)) != len(required_cards):
        raise ValueError("Specify distinct required cards")
    if type(min_records_per_phase) is not int or min_records_per_phase < 2:
        raise ValueError("At least two records are needed to estimate stability")
    for limit in (max_delay_spread_ns, max_phase_spread_deg):
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("Stability limits must be finite and positive")
    by_phase = defaultdict(lambda: defaultdict(list))
    for record in records:
        phase = record["phase_mu"]
        if type(phase) is not int or not 0 <= phase < period_mu:
            raise ValueError("phase_mu is outside the scanned trigger period")
        by_phase[phase][record["card"]].append(record)
    stable = []
    for phase in range(period_mu):
        good = True
        for card in required_cards:
            rows = by_phase[phase].get(card, [])
            if len(rows) < min_records_per_phase:
                good = False
                break
            delays = np.asarray([_finite(row["delay_ns"], "delay_ns") for row in rows])
            angles = np.asarray(
                [_finite(row["phase_error_deg"], "phase_error_deg") for row in rows]
            )
            center = math.degrees(np.angle(np.mean(np.exp(1j * np.deg2rad(angles)))))
            if (
                np.ptp(delays) > max_delay_spread_ns
                or np.ptp(wrap_degrees(angles - center)) > max_phase_spread_deg
            ):
                good = False
                break
        if good:
            stable.append(phase)
    if not stable:
        return {"status": "failed", "stable_phases_mu": [], "selected_phase_mu": None}
    stable_set = set(stable)
    best = []
    for start in stable:
        run = []
        for offset in range(period_mu):
            phase = (start + offset) % period_mu
            if phase not in stable_set:
                break
            run.append(phase)
        if len(run) > len(best):
            best = run
    return {
        "status": "candidate",
        "stable_phases_mu": stable,
        "window_mu": best,
        "selected_phase_mu": best[(len(best) - 1) // 2],
        "physical_acceptance": "not_verified",
    }
