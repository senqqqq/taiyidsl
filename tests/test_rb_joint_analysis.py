import numpy as np
import pytest

from taiyidsl.analysis import AnalysisError
from taiyidsl.rb_analysis import analyze_rb, fit_rb_decay, interleaved_error_bound
from taiyidsl.rb_results import RBCounts, joint_rb_counts
from taiyidsl.results import ResultSet


def fixture_results(shots=4):
    return ResultSet(
        axes={},
        coordinates=[{"length": 1, "sequence": 0, "variant": "reference"}],
        shots=shots,
        qubits=("q0", "q1"),
        handles=("bits",),
    )


def test_joint_p00_uses_same_shot_and_preserves_all_outcomes():
    results = fixture_results()
    for shot, bits in enumerate([(0, 0), (0, 0), (1, 1), (1, 1)]):
        for q, bit in enumerate(bits):
            results.record("bits", 0, shot, q, bit)
    (counts,) = joint_rb_counts(results, "bits", qubits=("q0", "q1"))
    assert counts.counts == (2, 0, 0, 2)
    assert counts.probability == 0.5
    assert counts.probability != 0.5 * 0.5
    assert counts.to_dict()["counts"] == {"00": 2, "01": 0, "10": 0, "11": 2}


def test_loss_and_invalid_policies_are_explicit_and_recorded():
    results = fixture_results()
    for q in (0, 1):
        results.record("bits", 0, 0, q, 0)
        results.record("bits", 0, 1, q, status="atom_lost")
        results.record("bits", 0, 2, q, status="missing")
        results.record("bits", 0, 3, q, 1)
    with pytest.raises(ValueError, match="loss"):
        joint_rb_counts(results, "bits", qubits=("q0", "q1"))
    with pytest.raises(ValueError, match="invalid"):
        joint_rb_counts(
            results, "bits", qubits=("q0", "q1"), loss_policy="count_as_failure"
        )
    (all_shots,) = joint_rb_counts(
        results,
        "bits",
        qubits=("q0", "q1"),
        loss_policy="count_as_failure",
        invalid_policy="count_as_failure",
    )
    assert all_shots.probability == 1 / 4
    (selected,) = joint_rb_counts(
        results,
        "bits",
        qubits=("q0", "q1"),
        loss_policy="exclude",
        invalid_policy="exclude",
    )
    assert selected.probability == 1 / 2
    assert selected.to_dict()["excluded"] == 2
    assert selected.to_dict()["joint_status_counts"]["atom_lost/atom_lost"] == 1
    assert selected.lost == selected.invalid == 1
    assert results.status["bits"][0, 1, 0] == "atom_lost"


def test_continuous_population_is_not_implicitly_classified_as_a_bit():
    results = fixture_results(1)
    results.record("bits", 0, 0, 0, 0.2)
    results.record("bits", 0, 0, 1, 0)
    with pytest.raises(ValueError, match="classified bits"):
        joint_rb_counts(results, "bits", qubits=("q0", "q1"))


def synthetic_counts(*, n=2, alpha=0.96, ratio=0.98, paired=True, sequences=5):
    records = []
    shots = 10_000_000
    for length in (0, 1, 4, 12, 30, 60):
        for sequence in range(sequences):
            # Correlated sequence-dependent SPAM shifts must stay in each pair.
            amplitude = 0.68 + (sequence - (sequences - 1) / 2) * 0.004
            for variant in ("reference", "interleaved") if paired else ("reference",):
                decay = alpha * (ratio if variant == "interleaved" else 1)
                zero = round(shots * (amplitude * decay**length + 0.24))
                counts = (zero, shots - zero, *([0] * (2**n - 2)))
                records.append(
                    RBCounts(
                        length,
                        sequence,
                        variant,
                        tuple(f"q{i}" for i in range(n)),
                        counts,
                    )
                )
    return tuple(records)


@pytest.mark.parametrize("n", [1, 2])
def test_known_depolarizing_decay_has_correct_dimension_factor_and_free_spam(n):
    records = synthetic_counts(n=n, paired=False)
    result = fit_rb_decay(records)
    assert result.alpha == pytest.approx(0.96, abs=1e-7)
    assert result.amplitude == pytest.approx(0.68, abs=1e-6)
    assert result.offset == pytest.approx(0.24, abs=1e-6)
    assert result.epc == pytest.approx((2**n - 1) / 2**n * 0.04, abs=1e-7)


def test_irb_estimate_bound_and_paired_sequence_bootstrap_are_separate():
    result = analyze_rb(synthetic_counts(), bootstrap_samples=20, seed=90)
    assert result["interleaved_cz"]["estimate"] == pytest.approx(0.75 * 0.02, abs=1e-6)
    p, pc = 0.96, 0.96 * 0.98
    expected = min(
        0.75 * (abs(p - pc / p) + 1 - p),
        15 / 8 * (1 - p) / p + 4 * np.sqrt(15) * np.sqrt(1 - p) / p,
    )
    assert result["interleaved_cz"]["systematic_half_width"] == pytest.approx(
        expected, abs=1e-6
    )
    bootstrap = result["bootstrap"]
    assert bootstrap["method"] == "paired sequence clusters stratified by length"
    assert bootstrap["successful"] == 20
    assert bootstrap["failed"] == 0
    low, high = bootstrap["statistical_intervals"]["cz_error"]
    assert low < 0.015 < high
    assert high - low < 0.002
    again = analyze_rb(synthetic_counts(), bootstrap_samples=20, seed=90)
    assert again == result


def test_negative_irb_estimate_is_reported_without_clipping():
    result = interleaved_error_bound(0.96, 0.99)
    assert result["estimate"] < 0
    assert result["negative_estimate"] is True
    assert result["systematic_interval"][0] < 0


def test_missing_pair_insufficient_lengths_and_constant_decay_fail_explicitly():
    records = synthetic_counts()
    with pytest.raises(AnalysisError, match="matching"):
        analyze_rb(records[:-1], bootstrap_samples=20)
    with pytest.raises(AnalysisError, match="four distinct"):
        fit_rb_decay(records[:20])
    constant = [RBCounts(m, 0, "reference", ("q0",), (50, 50)) for m in (0, 1, 2, 3)]
    with pytest.raises(AnalysisError, match="constant"):
        fit_rb_decay(constant)
    with pytest.raises(AnalysisError, match="at least two sequences"):
        analyze_rb(synthetic_counts(sequences=1), bootstrap_samples=20)


def test_inconsistent_loss_policy_or_duplicate_sequences_are_not_combined():
    records = synthetic_counts(paired=False)
    with pytest.raises(AnalysisError, match="Duplicate"):
        fit_rb_decay((*records, records[0]))
    replacement = RBCounts(
        0, 0, "reference", ("q0", "q1"), (50, 0, 0, 50), loss_policy="exclude"
    )
    with pytest.raises(AnalysisError, match="policies"):
        fit_rb_decay((replacement, *records[1:]))
