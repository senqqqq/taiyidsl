from types import SimpleNamespace

import numpy as np
import pytest

from taiyidsl.config.models import Qubit
from taiyidsl.results import ResultSet, synthesize


def fake_compiled(mode="cyclic", shots=3):
    q = Qubit("q0")
    points = [
        SimpleNamespace(
            index=i,
            coordinates={"duration": t},
            axis_indices=(i,),
            logical_events=[
                SimpleNamespace(
                    kind="measure",
                    signal=q.readout,
                    params={"handle": "population", "qubit": q.uid},
                )
            ],
        )
        for i, t in enumerate([0.0, 5e-6, 10e-6])
    ]
    order = (
        [(p, s) for s in range(shots) for p in range(3)]
        if mode == "cyclic"
        else [(p, s) for p in range(3) for s in range(shots)]
    )
    return SimpleNamespace(
        name="rabi",
        points=points,
        shots=shots,
        averaging_mode=mode,
        axes=[{"name": "duration", "values": [0.0, 5e-6, 10e-6], "unit": "s"}],
        qubits={q.uid: q},
        metadata={"experiment_type": "rabi"},
        provenance={"reference_plane": "digital"},
        execution_order=lambda: iter(order),
    )


def test_averaging_modes_change_order_not_observations():
    a = synthesize(fake_compiled("cyclic", 20), seed=42)
    b = synthesize(fake_compiled("sequential", 20), seed=42)
    np.testing.assert_array_equal(a.data["population"], b.data["population"])
    assert a.execution_order[:3] == [(0, 0), (1, 0), (2, 0)]
    assert b.execution_order[:3] == [(0, 0), (0, 1), (0, 2)]


def test_partial_execution_keeps_unexecuted_and_counts():
    results = synthesize(fake_compiled(), seed=1, max_shots=2)
    assert results.execution_order == [(0, 0), (1, 0)]
    np.testing.assert_array_equal(results.valid_counts().ravel(), [1, 1, 0])
    assert np.isnan(results.means()[2, 0])
    assert results.status["population"][2, 0, 0] == "unexecuted"


def test_loss_is_not_dark_state_and_postselection():
    results = synthesize(fake_compiled(), model_parameters={"loss_probability": 1})
    assert np.all(results.status["population"] == "atom_lost")
    assert results.valid_counts().sum() == 0
    results.record(
        "population", 0, 0, "q0", 0, labels={"loaded": True, "atom_present": True}
    )
    results.record(
        "population", 0, 1, "q0", 1, labels={"loaded": True, "atom_present": False}
    )
    assert results.means()[0, 0] == 0.5
    selected = results.aggregate(postselection={"atom_present": True})
    assert selected["mean"][0, 0] == 0
    assert selected["valid_counts"][0, 0] == 1


def test_results_roundtrip_without_pickle(tmp_path):
    results = synthesize(fake_compiled(), seed=3, max_shots=4)
    results.record(
        "population",
        1,
        1,
        "q0",
        asset="images/frame_01.tif",
        labels={"classified": False},
    )
    restored = ResultSet.load(results.save(tmp_path))
    np.testing.assert_array_equal(
        restored.data["population"], results.data["population"]
    )
    np.testing.assert_array_equal(
        restored.status["population"], results.status["population"]
    )
    assert restored.execution_order == results.execution_order
    assert restored.assets == results.assets
    assert restored.qubit_snapshots == results.qubit_snapshots
    assert restored.metadata["synthetic"] is True


def test_invalid_numeric_observation_rejected():
    results = synthesize(fake_compiled(), max_shots=0)
    with pytest.raises(ValueError, match="finite"):
        results.record("population", 0, 0, "q0", np.nan)
    with pytest.raises(ValueError, match="status"):
        results.record("population", 0, 0, "q0", 0, status="dark")
    with pytest.raises(TypeError, match="indices"):
        results.record("population", 0.5, 0, "q0", 0)


def test_invalid_status_file_is_rejected(tmp_path):
    results = synthesize(fake_compiled())
    results.save(tmp_path)
    status = results.status["population"].copy()
    status[0, 0, 0] = "failed"
    np.savez_compressed(
        tmp_path / "observations.npz",
        data_0=results.data["population"],
        status_0=status,
    )
    with pytest.raises(ValueError, match="Invalid results"):
        ResultSet.load(tmp_path)
