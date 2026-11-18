import json
from dataclasses import replace
from hashlib import sha256

import numpy as np
import pytest

from taiyidsl.analysis import (
    AnalysisError,
    _validate_rabi_program,
    analyze_rabi,
    analyze_ramsey,
    analyze_rb,
)
from taiyidsl.dsl import json_value
from taiyidsl.models import Qubit
from taiyidsl.results import ResultSet
from taiyidsl.templates import rabi


def attach_ir(result, exp):
    result.provenance["experiment_ir_hash"] = sha256(
        json.dumps(json_value(exp.root), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def exact_results(axis, x, values, *, name, shots=8, q=None):
    q = q or Qubit("q0")
    result = ResultSet(
        axes=[
            {
                "name": axis,
                "values": list(x),
                "unit": "s" if axis != "length" else "dimensionless",
            }
        ],
        coordinates=[{axis: float(v)} for v in x],
        shots=shots,
        qubits=[q.uid],
        handles=["population"],
        metadata={"experiment_name": name},
        qubit_snapshots={q.uid: q.to_dict()},
    )
    for point, value in enumerate(values):
        for shot in range(shots):
            result.record("population", point, shot, q.uid, float(value))
    if name == "rabi":
        attach_ir(result, rabi(q, x, shots=shots))
    return result


def test_rabi_fit_proposes_updates_without_mutating_snapshot():
    q = Qubit("q0")
    x = np.linspace(0, 60e-6, 61)
    pi_duration = 11e-6
    result = exact_results(
        "duration",
        x,
        0.03 + 0.94 * np.sin(np.pi * x / (2 * pi_duration)) ** 2,
        name="rabi",
        q=q,
    )
    exp = rabi(q, x, shots=8)
    proposal = analyze_rabi(result, experiment=exp)
    assert proposal.updates["raman.pi_duration"] == pytest.approx(pi_duration, rel=1e-5)
    assert proposal.updates["raman.pi2_duration"] == pytest.approx(
        pi_duration / 2, rel=1e-5
    )
    assert proposal.quality["r_squared"] > 0.999999
    assert q.params.raman.pi_duration == 10e-6
    assert proposal.base_fingerprint == q.fingerprint
    assert tuple(proposal.analysis_metadata["valid_counts"]) == (8,) * len(x)
    assert proposal.analysis_metadata["postselection"] == {}


def test_rabi_rejects_mismatched_snapshot():
    q = Qubit("q0")
    x = np.linspace(0, 50e-6, 51)
    results = exact_results(
        "duration", x, 0.5 - 0.45 * np.cos(2 * np.pi * x / 20e-6), name="rabi", q=q
    )
    newer = replace(q, revision=1)
    with pytest.raises(AnalysisError, match="snapshots"):
        analyze_rabi(results, experiment=rabi(newer, x, shots=8))


def test_rabi_program_accepts_explicit_phase_reset_for_its_own_qubit():
    q = Qubit("q0")
    x = np.array([0, 4e-6, 8e-6])
    results = exact_results("duration", x, [0.0, 0.3, 0.8], name="rabi", q=q)
    exp = rabi(q, x, shots=8)
    body = exp.root.children[0].children[0].children[1]
    reset = next(node for node in body.children if node.kind == "reset_frame")
    reset.kind = "reset_phase"
    attach_ir(results, exp)
    assert _validate_rabi_program(results, exp, q, "population", "duration")
    reset.params["frame"] = "another_qubit"
    with pytest.raises(AnalysisError, match="another qubit"):
        _validate_rabi_program(results, exp, q, "population", "duration")


def test_ramsey_fits_absolute_frequency_and_t2_only():
    x = np.linspace(0, 160e-6, 81)
    result = exact_results(
        "tau",
        x,
        0.51 + 0.4 * np.exp(-x / 90e-6) * np.cos(2 * np.pi * 42e3 * x + 0.2),
        name="ramsey",
    )
    fit = analyze_ramsey(result)
    assert fit.parameters["fringe_frequency_abs"] == pytest.approx(42e3, rel=1e-5)
    assert fit.parameters["t2star"] == pytest.approx(90e-6, rel=1e-5)
    assert "frequency_correction" not in fit.parameters
    assert fit.quality["r_squared"] > 0.999999


def test_rb_fit_and_missing_results():
    x = np.array([0, 1, 2, 4, 8, 16, 32, 64, 128])
    result = exact_results("length", x, 0.43 * 0.983**x + 0.49, name="rb")
    result.record("population", 2, 0, "q0", status="failed")
    fit = analyze_rb(result)
    assert fit.parameters["p"] == pytest.approx(0.983, abs=1e-5)
    assert fit.valid_counts[2] == 7
    assert fit.parameters["error_per_clifford"] == pytest.approx(0.0085, abs=1e-5)
    assert "p_std_shot_only" in fit.parameters
    assert "p_std" not in fit.parameters


def test_constant_or_insufficient_data_does_not_propose_calibration():
    x = np.linspace(0, 10e-6, 10)
    result = exact_results("duration", x, np.full(10, 0.5), name="rabi")
    q = Qubit("q0")
    with pytest.raises(AnalysisError, match="nonconstant"):
        analyze_rabi(result, experiment=rabi(q, x, shots=8))


def test_rabi_override_cannot_overwrite_another_amplitude_profile():
    q = Qubit("q0")
    x = np.linspace(0, 50e-6, 51)
    result = exact_results(
        "duration", x, 0.5 - 0.45 * np.cos(2 * np.pi * x / 20e-6), name="rabi", q=q
    )
    exp = rabi(q, x, shots=8, amplitude=0.2)
    with pytest.raises(AnalysisError, match="control settings"):
        analyze_rabi(result, experiment=exp)


def test_oscillatory_fit_does_not_mix_other_sweep_axes():
    q = Qubit("q0")
    x = np.linspace(0, 50e-6, 51)
    result = exact_results(
        "duration", x, 0.5 - 0.45 * np.cos(2 * np.pi * x / 20e-6), name="rabi", q=q
    )
    for i, coord in enumerate(result.coordinates):
        coord["frequency"] = 80e6 + i % 2 * 100e3
    with pytest.raises(AnalysisError, match="single slice"):
        analyze_rabi(result, experiment=rabi(q, x, shots=8))


def test_rabi_checks_actual_pulse_without_template_metadata():
    q = Qubit("q0")
    x = np.linspace(0, 50e-6, 51)
    result = exact_results(
        "duration", x, 0.5 - 0.45 * np.cos(2 * np.pi * x / 20e-6), name="rabi", q=q
    )
    exp = rabi(q, x, shots=8, amplitude=0.2)
    exp.metadata.clear()
    attach_ir(result, exp)
    with pytest.raises(AnalysisError, match="control settings"):
        analyze_rabi(result, experiment=exp)


def test_rabi_rejects_same_name_snapshot_but_different_ir():
    q = Qubit("q0")
    x = np.linspace(0, 50e-6, 51)
    result = exact_results(
        "duration", x, 0.5 - 0.45 * np.cos(2 * np.pi * x / 20e-6), name="rabi", q=q
    )
    exp = rabi(q, x, shots=9)
    with pytest.raises(AnalysisError, match="IR hash"):
        analyze_rabi(result, experiment=exp)
