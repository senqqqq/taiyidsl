"""Parameter immutability and real transactional update behavior."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from taiyidsl.config.models import (
    CalibrationPolicy,
    CalibrationProposal,
    DriveParameters,
    Qubit,
    QubitParameters,
    QubitStore,
    ReadoutParameters,
)
from taiyidsl.units import MHz, us


def proposal_for(q, *, score=0.99, value=12e-6, success=True):
    return CalibrationProposal(
        q.uid,
        q.revision,
        q.fingerprint,
        {"raman.pi_duration": value},
        {"raman.pi_duration": "s"},
        {"r_squared": score, "relative_uncertainty": 0.01},
        "rabi_run_001",
        success,
    )


def test_snapshot_and_user_update_do_not_mutate_old_qubit():
    nested = {"lab": {"labels": ["A"], "temperature": 10.0}}
    q = Qubit("q0", QubitParameters(extensions=nested))
    snap = q.snapshot()
    nested["lab"]["labels"].append("B")
    assert snap.params.extensions["lab"]["labels"] == ("A",)
    assert snap is not q and snap.fingerprint == q.fingerprint
    with pytest.raises(TypeError):
        snap.params.extensions["lab"]["temperature"] = 5
    with pytest.raises(FrozenInstanceError):
        snap.params.raman.amplitude = 0.2
    store = QubitStore([q])
    updated = store.update("q0", {"raman.amplitude": 0.25})
    assert updated.revision == 1 and updated.params.raman.amplitude == 0.25
    assert q.params.raman.amplitude == snap.params.raman.amplitude == 0.5
    assert (q.raman, q.rf, q.readout, q.frame) == (
        "q0.raman",
        "q0.rf",
        "q0.readout",
        "q0",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frequency": 0},
        {"amplitude": 1.1},
        {"amplitude": -0.1},
        {"pi_duration": 0},
        {"pi2_duration": float("nan")},
        {"frequency": float("inf")},
        {"amplitude": True},
    ],
)
def test_invalid_drive_parameters(kwargs):
    with pytest.raises((ValueError, TypeError)):
        DriveParameters(**kwargs)


def test_ids_and_input_schema_are_validated():
    with pytest.raises(ValueError):
        Qubit("q0.raman")
    with pytest.raises(ValueError):
        Qubit("q0", revision=True)
    with pytest.raises(ValueError):
        ReadoutParameters(-1)
    with pytest.raises(ValueError):
        Qubit.from_dict({"uid": "q0", "typo": 1})
    with pytest.raises(ValueError):
        QubitStore([Qubit("q0"), Qubit("q0")])


def test_apply_save_load_and_history(tmp_path):
    path = tmp_path / "config" / "qubits.json"
    q = Qubit("q0")
    store = QubitStore([q], path=path)
    new = store.apply(proposal_for(q))
    loaded = QubitStore.load(path)
    assert loaded["q0"].fingerprint == new.fingerprint
    assert loaded["q0"].params.raman.pi_duration == 12e-6
    assert list(loaded) == ["q0"]
    assert loaded.history[0]["proposal"]["source_experiment"] == "rabi_run_001"
    assert loaded.history[0]["old_fingerprint"] == q.fingerprint
    assert loaded.history[0]["before"]["params"]["raman"]["pi_duration"] == 10e-6
    assert loaded.history[0]["after"]["params"]["raman"]["pi_duration"] == 12e-6
    with pytest.raises(TypeError):
        loaded.history[0]["kind"] = "wrong"
    assert json.loads(path.read_text())["schema_version"] == 2


def test_apply_rejects_stale_and_invalid_without_changing_state():
    q = Qubit("q0")
    store = QubitStore([q])
    proposal = proposal_for(q)
    store.update("q0", {"raman.frequency": 81e6})
    with pytest.raises(ValueError, match="Stale"):
        store.apply(proposal, persist=False)
    current = store["q0"]
    with pytest.raises(ValueError, match="positive"):
        store.apply(proposal_for(current, value=-1), persist=False)
    assert store["q0"].fingerprint == current.fingerprint
    assert len(store.history) == 1


def test_fingerprint_checks_same_revision_with_different_parameters():
    original = Qubit("q0")
    different = Qubit("q0", QubitParameters(raman=DriveParameters(amplitude=0.2)))
    with pytest.raises(ValueError, match="fingerprint"):
        QubitStore([different]).apply(proposal_for(original), persist=False)


def test_invalid_proposal_units_and_quality():
    q = Qubit("q0")
    with pytest.raises(ValueError, match="unit"):
        CalibrationProposal(
            q.uid,
            0,
            q.fingerprint,
            {"raman.pi_duration": 10},
            {"raman.pi_duration": "us"},
            {},
            "test",
        )
    with pytest.raises(ValueError, match="finite"):
        proposal_for(q, score=float("nan"))
    with pytest.raises(ValueError, match="Unknown"):
        CalibrationProposal(
            q.uid,
            0,
            q.fingerprint,
            {"raman.misspelled": 10},
            {"raman.misspelled": "s"},
            {},
            "test",
        )


def test_automatic_application_requires_explicit_quality_policy():
    q = Qubit("q0")
    store = QubitStore([q])
    policy = CalibrationPolicy(min_r_squared=0.98, max_relative_uncertainty=0.05)
    with pytest.raises(ValueError, match="quality"):
        store.apply_auto(proposal_for(q, score=0.9), policy, persist=False)
    with pytest.raises(TypeError):
        store.apply_auto(proposal_for(q), None, persist=False)
    with pytest.raises(ValueError, match="unsuccessful"):
        store.apply(proposal_for(q, success=False), persist=False)
    assert store["q0"].revision == 0 and not store.history
    store.apply_auto(proposal_for(q), policy, persist=False)
    assert store["q0"].revision == 1


def test_persistence_without_path_does_not_update_memory():
    q = Qubit("q0")
    store = QubitStore([q])
    with pytest.raises(ValueError, match="path"):
        store.apply(proposal_for(q))
    assert store["q0"].fingerprint == q.fingerprint and not store.history


def test_atomic_write_failure_preserves_memory_and_existing_file(tmp_path, monkeypatch):
    q = Qubit("q0")
    path = tmp_path / "qubits.json"
    store = QubitStore([q], path=path)
    store.save()
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("taiyidsl.config.models.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        store.apply(proposal_for(q))
    assert path.read_bytes() == before
    assert store["q0"].fingerprint == q.fingerprint
    assert not store.history
    assert list(tmp_path.iterdir()) == [path]


def test_external_file_edit_is_not_overwritten(tmp_path):
    path = tmp_path / "qubits.json"
    QubitStore([Qubit("q0")]).save(path)
    first = QubitStore.load(path)
    second = QubitStore.load(path)
    first.update("q0", {"raman.frequency": 81e6}, persist=True)
    with pytest.raises(ValueError, match="externally"):
        second.apply(proposal_for(second["q0"]))
    assert QubitStore.load(path)["q0"].params.raman.frequency == 81e6
    assert second["q0"].revision == 0


def test_reject_future_schema(tmp_path):
    path = tmp_path / "qubits.json"
    latest_supported_schema = 3  # Role-parameter stores already use version 3.
    path.write_text(
        json.dumps(
            {
                "schema_version": latest_supported_schema + 1,
                "qubits": [],
                "history": [],
            }
        )
    )
    with pytest.raises(ValueError, match="schema"):
        QubitStore.load(path)


def test_extension_updates_are_immutable_and_preserved(tmp_path):
    q = Qubit("q0", QubitParameters(extensions={"lab": {"temperature": 5}}))
    store = QubitStore([q])
    updated = store.update("q0", {"extensions.lab.temperature": 10})
    assert updated.params.extensions["lab"]["temperature"] == 10
    assert q.params.extensions["lab"]["temperature"] == 5
    with pytest.raises(ValueError, match="does not exist"):
        store.update("q0", {"extensions.lab.unknown": 3})


def test_unit_quantities_are_checked_and_saved_as_si(tmp_path):
    params = DriveParameters(frequency=80 * MHz, pi_duration=12 * us)
    assert params.frequency == 80e6 and params.pi_duration == 12e-6
    with pytest.raises(ValueError, match="Expected time"):
        DriveParameters(pi_duration=10 * MHz)
    store = QubitStore([Qubit("q0")], path=tmp_path / "qubits.json")
    updated = store.update("q0", {"raman.pi_duration": 20 * us}, persist=True)
    assert updated.params.raman.pi_duration == 20e-6
    assert QubitStore.load(store.path)["q0"].fingerprint == updated.fingerprint
    assert store.history[0]["updates"]["raman.pi_duration"] == 20e-6


def test_proposal_analysis_metadata_deep_freeze_and_roundtrip(tmp_path):
    q = Qubit("q0")
    metadata = {
        "valid_counts": [100, 98, 99],
        "postselection": {"loaded": True},
        "source_ir_hash": "abc123",
    }
    proposal = replace(proposal_for(q), analysis_metadata=metadata)
    metadata["valid_counts"].append(0)
    metadata["postselection"]["loaded"] = False
    assert proposal.analysis_metadata["valid_counts"] == (100, 98, 99)
    assert proposal.analysis_metadata["postselection"]["loaded"] is True
    with pytest.raises(TypeError):
        proposal.analysis_metadata["postselection"]["loaded"] = False
    reconstructed = CalibrationProposal.from_dict(
        json.loads(json.dumps(proposal.to_dict()))
    )
    assert reconstructed.to_dict() == proposal.to_dict()
    path = tmp_path / "qubits.json"
    QubitStore([q], path=path).apply(proposal)
    stored = QubitStore.load(path).history[0]["proposal"]["analysis_metadata"]
    assert stored["valid_counts"] == (100, 98, 99)
    assert stored["source_ir_hash"] == "abc123"


def test_legacy_proposal_without_analysis_metadata_still_loads():
    old = proposal_for(Qubit("q0")).to_dict()
    old.pop("analysis_metadata")
    assert dict(CalibrationProposal.from_dict(old).analysis_metadata) == {}
    with pytest.raises(ValueError, match="finite"):
        replace(proposal_for(Qubit("q0")), analysis_metadata={"value": float("nan")})
