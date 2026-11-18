import copy
import math

import numpy as np
import pytest

from taiyidsl.spectrum_calibration import (
    analyze_measurements,
    calibrate_channels,
    fit_rf_trace,
    load_capture_npz,
    resolve_channel_calibration,
    select_trigger_window,
)


def record(channel="a:0", trigger=0, **overrides):
    return {
        "channel": channel,
        "trigger_id": trigger,
        "rearm_id": trigger // 100,
        "lock_id": "0",
        "time_s": trigger * 0.2,
        "phase_error_deg": 0.2,
        "delay_ns": 1.0,
        "deskew_ns": 0.0,
        "gain": 1.0,
        "phase_uncertainty_deg": 0.1,
        "timing_uncertainty_ns": 0.1,
        "calibration_revision": "measured-0",
        "carrier_frequency_hz": 100e6,
        "sample_shift_applied": 0,
        "measurement_scope": "phase_windows",
        "phase_window_bounds_deg": [0.3, 0.4],
        "phase_window_alignment_removed": False,
        "phase_windows_cover_effective_envelope": True,
        "phase_analysis_threshold_fraction": 0.1,
        "rf_residual_rms": 0.001,
        "rf_residual_rms_limit": 0.01,
        "rf_residual_reference": "unaligned_target",
        **overrides,
    }


def calibration(channels=("a:0", "b:0", "b:1"), lock="0"):
    return {
        "status": "measured",
        "revision": f"measured-{lock}",
        "clock_session_ids": {"a": f"a-session-{lock}", "b": f"b-session-{lock}"},
        "channels": {
            channel: {
                "phase_offset_rad": -0.01,
                "gain": 1.01,
                "delay_ns": 0.2,
                "phase_uncertainty_deg": 0.1,
                "timing_uncertainty_ns": 0.1,
                "sample_rate_hz": 1.25e9,
                "sample_shift_samples": 0,
                "phase_reference": "after_sample_shift",
                "carrier_frequency_hz": 100e6,
            }
            for channel in channels
        },
    }


def test_six_cycle_nuclear_pulse_fit_reports_phase_without_removing_it():
    t = np.arange(25_000) * 8e-9
    phase = 2 * np.pi * 30_000 * t
    envelope = np.ones_like(t)
    observed = 0.8 * np.cos(phase + np.deg2rad(2.4)) + 0.01
    result = fit_rf_trace(observed, envelope, phase, phase_uncertainty_deg=0.08)
    assert result["phase_error_deg"] == pytest.approx(2.4)
    assert result["gain"] == pytest.approx(0.8)
    assert result["dc_offset"] == pytest.approx(0.01)
    assert result["fit_residual_rms"] < 1e-12
    assert result["raw_rf_residual_rms"] > 0.1
    assert result["phase_was_subtracted_for_acceptance"] is False


def test_known_modulated_gaussian_fit_and_npz_capture(tmp_path):
    time = np.arange(500) / 1.25e9
    u = time / 400e-9
    envelope = np.minimum(np.exp(-0.5 * ((time - 80e-9) / 20e-9) ** 2), 1)
    envelope[(time >= 80e-9) & (time <= 320e-9)] = 1
    envelope[time > 320e-9] = np.exp(
        -0.5 * ((time[time > 320e-9] - 320e-9) / 20e-9) ** 2
    )
    phase = 2 * np.pi * 150e6 * time + np.pi * (3 * u**2 - 2 * u**3)
    observed = envelope * np.cos(phase - np.deg2rad(0.35))
    path = tmp_path / "capture.npz"
    np.savez(path, voltage=observed, envelope=envelope, reference_phase_rad=phase)
    result = load_capture_npz(path, record())
    assert result["phase_error_deg"] == pytest.approx(-0.35)
    assert result["valid_phase_samples"] < 500
    assert result["channel"] == "a:0"


def test_unresolvable_phase_or_nonfinite_trace_rejected():
    with pytest.raises(ValueError, match="cannot resolve"):
        fit_rf_trace(np.ones(100), np.ones(100), np.zeros(100), phase_uncertainty_deg=0)
    with pytest.raises(ValueError, match="nonfinite"):
        fit_rf_trace([0, 1, 2, np.nan], [1] * 4, [0, 1, 2, 3], phase_uncertainty_deg=0)


def test_calibration_has_correct_correction_sign_gain_and_deskew():
    rows = [
        record(
            trigger=i,
            phase_error_deg=12.0,
            gain=0.8,
            delay_ns=2.2,
            deskew_ns=2.0,
            sample_shift_applied=-6,
        )
        for i in range(20)
    ]
    profile = calibrate_channels(
        rows,
        revision="bench",
        card_fingerprints={"a": "fingerprint"},
        clock_session_ids={"a": "session"},
        timing_calibration={
            "a:0": {
                "delay_ns": 5.0,
                "timing_uncertainty_ns": 0.1,
                "sample_rate_hz": 1.25e9,
            }
        },
    )
    item = profile["channels"]["a:0"]
    assert item["phase_offset_rad"] == pytest.approx(-math.radians(12))
    assert item["gain"] == pytest.approx(0.8)
    assert item["delay_ns"] == pytest.approx(5)
    assert item["residual_delay_ns"] == pytest.approx(0.2)
    assert item["sample_shift_samples"] == -6
    assert profile["physical_acceptance"] == "not_verified"
    report = analyze_measurements(rows, profile, corrections_applied=False)
    assert report["status"] == "not_verified"
    assert report["channels"]["a:0"]["max_phase_error_deg"] < 1e-10
    assert "acceptance reuses its calibration dataset" in report["incomplete"]


def test_fractional_delay_phase_is_measured_after_shift_and_frequency_exact():
    # A 4.4 ns extra delay is advanced 6 samples (4.8 ns): residual -0.4 ns.
    # The corresponding phase ADVANCE at 200 MHz is +28.8 degrees, not zero.
    rows = [
        record(
            trigger=i,
            phase_error_deg=28.8 * frequency / 200e6,
            carrier_frequency_hz=frequency,
            delay_ns=-0.4,
            sample_shift_applied=-6,
        )
        for i, frequency in enumerate((100e6, 200e6))
    ]
    kwargs = {
        "revision": "fractional",
        "card_fingerprints": {"a": "fingerprint"},
        "clock_session_ids": {"a": "session"},
        "timing_calibration": {
            "a:0": {
                "delay_ns": 4.4,
                "timing_uncertainty_ns": 0.1,
                "sample_rate_hz": 1.25e9,
            }
        },
    }
    profile = calibrate_channels(rows, **kwargs)
    high = resolve_channel_calibration(profile, "a", 0, 200e6)
    low = resolve_channel_calibration(profile, "a", 0, 100e6)
    assert high["phase_offset_rad"] == pytest.approx(-math.radians(28.8))
    assert low["phase_offset_rad"] == pytest.approx(-math.radians(14.4))
    assert high["delay_ns"] == 4.4
    with pytest.raises(ValueError, match="carrier frequency"):
        resolve_channel_calibration(profile, "a", 0, 150e6)
    rows[0].pop("sample_shift_applied")
    with pytest.raises(ValueError, match="sample shift"):
        calibrate_channels(rows, **kwargs)


def test_uncorrected_phase_dataset_cannot_be_marked_measured():
    with pytest.raises(ValueError, match="sample shift"):
        calibrate_channels(
            [record(delay_ns=4.4)],
            revision="bad",
            card_fingerprints={"a": "fingerprint"},
            clock_session_ids={"a": "session"},
            timing_calibration={
                "a:0": {
                    "delay_ns": 4.4,
                    "timing_uncertainty_ns": 0.1,
                    "sample_rate_hz": 1.25e9,
                }
            },
        )


def test_small_dataset_cannot_be_declared_hardware_acceptance():
    rows = [record(channel=channel) for channel in calibration()["channels"]]
    report = analyze_measurements(rows, calibration())
    assert report["status"] == "not_verified"
    assert any("10000" in reason for reason in report["incomplete"])
    assert report["per_record_alignment_removed"] is False


@pytest.mark.parametrize("error", [0.8, 1.0, 57.6, -57.6])
def test_phase_limit_includes_uncertainty_and_rejects_sample_jumps(error):
    rows = [
        record(channel=channel, phase_error_deg=error)
        for channel in calibration()["channels"]
    ]
    result = analyze_measurements(rows, calibration())
    assert result["status"] == "failed"


def test_common_delay_is_not_interchannel_skew_and_bound_is_strict():
    rows = [
        record(channel=channel, delay_ns=1000) for channel in calibration()["channels"]
    ]
    report = analyze_measurements(rows, calibration())
    assert report["status"] == "not_verified"
    assert max(report["pair_skew_bound_ns"].values()) == pytest.approx(0.4)
    rows[-1]["delay_ns"] += 4.6
    assert analyze_measurements(rows, calibration())["status"] == "failed"


def campaign():
    profiles = {str(i): calibration(lock=str(i)) for i in range(3)}
    rows = []
    for index in range(10_000):
        lock = "0" if index < 9980 else "1" if index < 9990 else "2"
        for channel in calibration()["channels"]:
            rows.append(
                record(
                    channel,
                    index,
                    lock_id=lock,
                    calibration_revision=f"measured-{lock}",
                )
            )
    return rows, profiles


def test_complete_independent_measurement_campaign_passes():
    rows, profiles = campaign()
    report = analyze_measurements(rows, profiles["0"], lock_calibrations=profiles)
    assert report["status"] == "passed"
    assert report["capture_count"] == 10_000
    assert report["lock_count"] == 3
    assert report["longest_single_lock_span_s"] >= 1800
    wrong = copy.deepcopy(profiles)
    wrong["2"]["clock_session_ids"] = wrong["1"]["clock_session_ids"]
    assert (
        analyze_measurements(rows, profiles["0"], lock_calibrations=wrong)["status"]
        == "not_verified"
    )


@pytest.mark.parametrize("missing", ["pair", "rearm", "stability"])
def test_campaign_cannot_borrow_other_channels_coverage(missing):
    rows, profiles = campaign()
    affected = [row for row in rows if row["channel"] == "b:1"]
    if missing == "pair":
        for row in affected[:10]:
            row["trigger_id"] += 100_000
        reason = "10000 simultaneous captures per required pair"
    elif missing == "rearm":
        for row in affected:
            row["rearm_id"] = 0
        reason = "100 stop/rearm cycles per channel"
    else:
        for row in affected:
            row["time_s"] *= 0.1
        reason = "30 minutes in one lock session per channel"
    report = analyze_measurements(rows, profiles["0"], lock_calibrations=profiles)
    assert report["status"] == "not_verified"
    assert any(reason in item for item in report["incomplete"])
    if missing == "pair":
        assert report["pair_capture_counts"]["a:0|b:1"] == 9990
    elif missing == "rearm":
        assert report["channels"]["b:1"]["rearm_count"] == 3
        assert report["rearm_count"] >= 100
    else:
        assert report["channels"]["b:1"]["longest_single_lock_span_s"] < 1800
        assert report["longest_single_lock_span_s"] >= 1800


def test_global_phase_fit_cannot_cancel_local_modulation_error():
    phase = 2 * np.pi * np.arange(1000) / 10
    observed = np.cos(phase + np.r_[np.full(500, 0.1), np.full(500, -0.1)])
    fit = fit_rf_trace(observed, np.ones(1000), phase, phase_uncertainty_deg=0.1)
    assert abs(fit["phase_error_deg"]) < 1e-10
    row = {**record(), **fit}
    # A global phase near zero and an artificially loose RF tolerance still
    # cannot establish pulse-resolved phase quality for a high-frequency pulse.
    row["rf_residual_rms_limit"] = 0.5
    report = analyze_measurements([row], calibration())
    assert report["status"] == "not_verified"
    assert report["waveform_evidence_missing"]["a:0"]
    row.update(measurement_scope="phase_windows", phase_window_bounds_deg=[5.8, 5.8])
    assert analyze_measurements([row], calibration())["status"] == "failed"
    row.update(phase_window_bounds_deg=[0.2, 0.2], rf_residual_rms_limit=0.01)
    assert analyze_measurements([row], calibration())["status"] == "failed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("measurement_scope", None),
        ("rf_residual_rms_limit", None),
        ("rf_residual_reference", "after_fitted_alignment"),
        ("phase_window_bounds_deg", []),
        ("phase_window_alignment_removed", True),
        ("phase_windows_cover_effective_envelope", False),
        ("phase_analysis_threshold_fraction", 0.5),
    ],
)
def test_incomplete_waveform_evidence_is_not_verified(field, value):
    row = record()
    if value is None:
        row.pop(field)
    else:
        row[field] = value
    report = analyze_measurements([row], calibration())
    assert report["status"] == "not_verified"
    assert report["waveform_evidence_missing"]["a:0"]


def test_low_frequency_known_model_still_requires_whole_rf_residual():
    profile = calibration(channels=("a:0",))
    profile["channels"]["a:0"]["carrier_frequency_hz"] = 30_000
    row = record(
        carrier_frequency_hz=30_000,
        measurement_scope="known_waveform_fit",
        phase_was_subtracted_for_acceptance=False,
    )
    report = analyze_measurements([row], profile)
    assert not report["waveform_evidence_missing"]
    row["rf_residual_rms"] = row["rf_residual_rms_limit"]
    assert analyze_measurements([row], profile)["status"] == "failed"


def test_window_bound_includes_fixed_calibration_uncertainty():
    row = record(phase_window_bounds_deg=[0.9])
    report = analyze_measurements([row], calibration())
    assert report["status"] == "failed"
    assert report["channels"]["a:0"]["max_window_phase_bound_deg"] == 1.0


def test_duplicate_measurement_does_not_inflate_evidence():
    with pytest.raises(ValueError, match="Duplicate"):
        analyze_measurements([record(), record()], calibration())


def test_trigger_window_requires_stability_on_both_cards():
    rows = []
    for phase in range(8):
        for card in ("a", "b"):
            for index in range(100):
                stable = phase in ({7, 0, 1, 2} if card == "a" else {6, 7, 0, 1})
                rows.append(
                    {
                        "phase_mu": phase,
                        "card": card,
                        "delay_ns": 400.0 + (0 if stable else 0.8 * (index % 2)),
                        "phase_error_deg": 179.99 if index % 2 else -179.99,
                    }
                )
    result = select_trigger_window(rows, period_mu=8, required_cards=["a", "b"])
    assert set(result["stable_phases_mu"]) == {7, 0, 1}
    assert result["selected_phase_mu"] == 0
    assert result["physical_acceptance"] == "not_verified"
    missing = select_trigger_window(rows, period_mu=8, required_cards=["a", "missing"])
    assert missing["status"] == "failed"
