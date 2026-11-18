"""Physical/model contracts used by the offline Yb171 tune-up notebook."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest
from scipy.linalg import expm

SPEC = importlib.util.spec_from_file_location(
    "yb171_tuneup_analysis",
    Path(__file__).resolve().parents[1] / "examples/yb171_tuneup_analysis.py",
)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def test_rabi_probability_resonance_and_zero_duration():
    rates = np.array([2_000.0, 5_000.0])
    np.testing.assert_allclose(analysis.rabi_probability(0, 0, rates), [0, 0])
    np.testing.assert_allclose(
        analysis.rabi_probability(1 / (2 * rates), 0, rates), [1, 1], atol=1e-14
    )
    np.testing.assert_allclose(
        analysis.rabi_probability(1 / rates, 0, rates), [0, 0], atol=1e-14
    )


def test_rabi_probability_broadcasts_and_preserves_detuning_symmetry():
    durations = np.array([20e-6, 50e-6, 110e-6])[:, None]
    detunings = np.array([-2_000.0, 0, 2_000.0])[None, :]
    probabilities = analysis.rabi_probability(durations, detunings, 5_000)
    assert probabilities.shape == (3, 3)
    np.testing.assert_allclose(probabilities[:, 0], probabilities[:, 2])
    maximum = analysis.rabi_probability(1 / (2 * np.hypot(5_000, 2_000)), 2_000, 5_000)
    assert maximum == pytest.approx(25 / 29)


def test_ramsey_probability_finite_detuned_pulses_match_matrix_exponentials():
    delays = np.array([0, 80e-6])[:, None]
    detunings = np.array([-1_200.0, 0, 3_700.0])
    rates = np.array([4_000.0, 5_000.0, 6_000.0])
    durations = np.array([35e-6, 50e-6, 65e-6])
    phases = np.array([-0.7, 0.3, np.pi / 2])
    actual = analysis.ramsey_probability(delays, detunings, rates, durations, phases)
    assert actual.shape == (2, 3)
    x = np.array([[0, 1], [1, 0]], dtype=complex)
    y = np.array([[0, -1j], [1j, 0]])
    z = np.diag([1, -1])
    expected = np.empty_like(actual)
    for row, delay in enumerate(delays[:, 0]):
        for col, (delta, rate, duration, phase) in enumerate(
            zip(detunings, rates, durations, phases, strict=True)
        ):
            first = expm(-1j * np.pi * duration * (rate * x - delta * z))
            free = expm(1j * np.pi * delta * delay * z)
            last = expm(
                -1j
                * np.pi
                * duration
                * (rate * (np.cos(phase) * x + np.sin(phase) * y) - delta * z)
            )
            expected[row, col] = abs((last @ free @ first)[1, 0]) ** 2
    np.testing.assert_allclose(actual, expected, atol=2e-14, rtol=0)
    ideal_instantaneous = (1 + np.cos(2 * np.pi * detunings * delays + phases)) / 2
    assert np.max(np.abs(actual - ideal_instantaneous)) > 0.05


def test_ramsey_probability_analysis_phase_and_signed_detuning():
    phases = np.array([0, np.pi / 2, np.pi, -np.pi / 2])
    np.testing.assert_allclose(
        analysis.ramsey_probability(100e-6, 0, 5_000, 50e-6, phases),
        [1, 0.5, 0, 0.5],
        atol=1e-14,
    )
    positive = analysis.ramsey_probability(100e-6, 1_200, 5_000, 50e-6, np.pi / 2)
    negative = analysis.ramsey_probability(100e-6, -1_200, 5_000, 50e-6, np.pi / 2)
    assert positive < negative
    assert positive == pytest.approx(
        analysis.ramsey_probability(100e-6, -1_200, 5_000, 50e-6, -np.pi / 2)
    )
    # At zero analysis phase the full finite-pulse fringe is even in detuning.
    assert analysis.ramsey_probability(100e-6, 1_200, 5_000, 50e-6) == pytest.approx(
        analysis.ramsey_probability(100e-6, -1_200, 5_000, 50e-6)
    )


@pytest.mark.parametrize(
    ("model", "arguments"),
    [
        ("rabi_probability", (-1e-6, 0, 5_000)),
        ("rabi_probability", (1e-6, 0, 0)),
        ("rabi_probability", (1e-6, np.nan, 5_000)),
        ("rabi_probability", (1e-6, 1j, 5_000)),
        ("ramsey_probability", (-1e-6, 0, 5_000, 50e-6)),
        ("ramsey_probability", (1e-6, 0, 5_000, 0)),
        ("ramsey_probability", (1e-6, 0, -5_000, 50e-6)),
        ("ramsey_probability", (1e-6, 0, 5_000, 50e-6, np.inf)),
        ("ramsey_probability", ([1e-6, 2e-6], [0, 1, 2], 5_000, 50e-6)),
    ],
)
def test_rabi_probability_and_ramsey_probability_reject_invalid_inputs(
    model, arguments
):
    with pytest.raises(ValueError):
        getattr(analysis, model)(*arguments)


def test_allxy_has_standard_three_levels_and_rotation_convention():
    np.testing.assert_allclose(
        analysis.allxy_expected(), [0] * 5 + [0.5] * 12 + [1] * 4, atol=1e-14
    )
    np.testing.assert_allclose(
        analysis.rotation(np.pi / 2, np.pi / 2) @ [1, 0],
        np.array([1, 1]) / np.sqrt(2),
        atol=1e-14,
    )


def test_fringe_recovers_signed_phase_and_rejects_missing_quadrature():
    phases = np.linspace(0, 2 * np.pi, 17, endpoint=False)
    result = analysis.fit_fringe(phases, 0.48 + 0.4 * np.cos(phases - 0.73))
    assert result["phase_rad"] == pytest.approx(-0.73)
    assert result["contrast"] == pytest.approx(0.8)
    with pytest.raises(ValueError, match="quadratures"):
        analysis.fit_fringe([0, np.pi, 2 * np.pi], [0.9, 0.1, 0.9])


def test_decay_reports_parameter_without_fidelity_and_rejects_unidentifiable_data():
    depths = np.array([0, 1, 2, 4, 8, 16, 32, 64, 128])
    result = analysis.fit_decay(depths, 0.45 * 0.982**depths + 0.48)
    assert result["p"] == pytest.approx(0.982, abs=1e-7)
    assert result["A"] == pytest.approx(0.45, abs=1e-7)
    assert result["B"] == pytest.approx(0.48, abs=1e-7)
    assert "fidelity" not in result
    with pytest.raises(ValueError, match="Constant"):
        analysis.fit_decay(depths, np.ones_like(depths) * 0.9)


def test_collective_seed_closes_and_clock_phase_removes_local_phase():
    seed = analysis.solve_collective_cz_seed()
    model = analysis.collective_cz_model(**seed)
    assert seed["delta_over_omega"] == pytest.approx(0.37737092, abs=1e-7)
    assert seed["duration_omega"] == pytest.approx(4.29268179, abs=1e-7)
    assert model["single_return"] == pytest.approx(1, abs=1e-12)
    assert model["pair_return"] == pytest.approx(1, abs=1e-12)
    assert abs(model["conditional_phase_rad"]) == pytest.approx(np.pi, abs=1e-12)
    corrected = model["logical_diag"] * np.exp(
        -1j * model["clock_return_phase_rad"] * np.array([0, 1, 1, 2])
    )
    np.testing.assert_allclose(corrected, [1, 1, 1, -1], atol=1e-11)


def test_finite_blockade_exposes_error_and_zero_blockade_is_separable():
    seed = analysis.solve_collective_cz_seed()
    unblocked = analysis.collective_cz_model(**seed, blockade_over_omega=0)
    assert unblocked["conditional_phase_rad"] == pytest.approx(0, abs=1e-12)
    finite = analysis.collective_cz_model(**seed, blockade_over_omega=10)
    # Finite blockade adds both leakage and a conditional-phase shift; a large
    # population return alone is therefore not a CZ fidelity measurement.
    assert 1 - finite["pair_return"] > 1e-5
    assert abs(abs(finite["conditional_phase_rad"]) - np.pi) > 0.01
    assert np.linalg.norm(finite["pair_state"]) == pytest.approx(1, abs=1e-12)


def test_global_mirror_recovers_identity_using_only_collective_controls():
    word = analysis.global_mirror_word(17, seed=31)
    assert word == analysis.global_mirror_word(17, seed=31)
    assert word.count("cz") == 34
    assert set(word) <= {"x90", "xm90", "y90", "ym90", "cz"}
    np.testing.assert_allclose(
        analysis.global_word_unitary(word), np.eye(4), atol=1e-13
    )
    with pytest.raises(ValueError, match="nonnegative integer"):
        analysis.global_mirror_word(-1)


def test_ssb_fixed_cost_balanced_inputs_and_recovery_without_local_controls():
    sequences = analysis.ssb_sequences(
        (2, 4, 6), random_rf_count=4, repeats_per_state=2, seed=17
    )
    assert len(sequences) == 3 * 12 * 2
    assert sequences == analysis.ssb_sequences(
        (2, 4, 6), random_rf_count=4, repeats_per_state=2, seed=17
    )
    for n in (2, 4, 6):
        selected = [s for s in sequences if s["total_cz_count"] == n]
        assert sorted(s["initial_state"] for s in selected) == [
            i for i in range(12) for _ in range(2)
        ]
    for sequence in sequences:
        word = sequence["word"]
        assert word.count("cz") == sequence["total_cz_count"]
        assert len(word) - word.count("cz") == sequence["rf_count"] == 13
        assert set(word) <= {"x90", "xm90", "y90", "ym90", "cz"}
        assert abs(analysis.global_word_unitary(word)[0, 0]) ** 2 == pytest.approx(1)
    with pytest.raises(ValueError, match="Random RF"):
        analysis.ssb_sequences((2, 7), random_rf_count=4)


def test_ssb_short_fit_has_protocol_parameter_and_no_full_space_conversion():
    depths = np.array([2, 4, 6, 8, 10])
    result = analysis.fit_ssb_short(depths, 0.94 * 0.993**depths)
    assert result["A"] == pytest.approx(0.94, abs=1e-8)
    assert result["F_ssb"] == pytest.approx(0.993, abs=1e-8)
    assert set(result) == {"A", "F_ssb", "residual_rms"}


@pytest.mark.parametrize("variant", ["cz", "clock", "idle"])
def test_global_echo_comparison_variants_recover_without_individual_rf(variant):
    word = analysis.global_echo_word(7, seed=29, variant=variant)
    assert word.count("G") == 14
    unitary = analysis.global_echo_unitary(word, variant)
    np.testing.assert_allclose(unitary, unitary[0, 0] * np.eye(4), atol=1e-13)
