import json

import h5py
import numpy as np
import pytest

from taiyidsl import Experiment, OpticsConfig, Qubit, measure, reference_lab
from taiyidsl.results import ResultSet, synthesize
from taiyidsl.templates import rabi


def compiled_rabi():
    q = Qubit("q0")
    exp = rabi(q, [0, 5e-6, 10e-6], shots=3)
    exp.metadata["description"] = "An explanatory experiment note"
    return reference_lab([q]).compile(exp)


def test_complete_context_is_detached_and_roundtrips(tmp_path):
    compiled = compiled_rabi()
    result = ResultSet.from_compiled(compiled, metadata={"operator_note": "offline"})
    context = result.context
    assert context["compiled_experiment"] == json.loads(json.dumps(compiled.to_dict()))
    assert context["experiment"]["ir"] == compiled.ir
    assert context["experiment"]["metadata"]["description"].startswith("An explanatory")
    assert context["lab_config"]["signals"] == compiled.signal_map.to_dict()
    assert context["qubits"]["q0"] == compiled.qubits["q0"].to_dict()
    context["experiment"]["metadata"]["description"] = "changed"
    assert result.context["experiment"]["metadata"]["description"] != "changed"
    result.record("population", 0, 0, "q0", 0)
    result.execution_order.append((0, 0))
    for restored in (
        ResultSet.load(result.save(tmp_path / "legacy")),
        ResultSet.load_hdf5(result.save_hdf5(tmp_path / "results.h5")),
    ):
        assert restored.context == result.context
        assert restored.metadata["operator_note"] == "offline"
        assert restored.execution_order == [(0, 0)]
        np.testing.assert_equal(restored.data["population"], result.data["population"])


def test_synthetic_results_also_archive_complete_context():
    result = synthesize(compiled_rabi(), max_shots=1)
    assert result.context["compiled_experiment"]["name"] == "rabi"
    assert result.metadata["synthetic"] is True


def test_optics_lab_and_original_experiment_snapshots_are_preserved(tmp_path):
    q = Qubit("q0")
    optics = OpticsConfig.from_dict(
        {
            "schema_version": 2,
            "revision": "working-point-A",
            "pump": {
                "signal": "optics.pump",
                "frequency_hz": 80e6,
                "amplitude": 0.2,
                "duration_s": 20e-6,
                "setup_wait_s": 10e-6,
                "settle_s": 10e-6,
            },
        }
    )
    exp = Experiment(
        "described",
        [q],
        parameters=optics,
        metadata={"purpose": "Readout calibration", "notes": ["test"]},
    )
    with exp.average(2):
        with exp.realtime():
            measure(q)
    lab = reference_lab([q])
    compiled = lab.compile(exp)
    result = ResultSet.from_compiled(compiled)
    expected_lab = lab.to_dict()
    lab.setup.name = "changed after compile"
    exp.metadata["notes"].append("later note")
    saved = ResultSet.load_hdf5(result.save_hdf5(tmp_path / "context.h5"))
    assert saved.context["lab_config"] == expected_lab
    assert saved.context["optics_config"] == optics.to_dict()
    assert saved.context["experiment"]["metadata"]["notes"] == ["test"]
    assert saved.context["qubits"]["q0"] == q.to_dict()


def test_xarray_preserves_coordinates_mask_and_is_independent():
    result = ResultSet.from_compiled(compiled_rabi())
    result.record("population", 1, 0, "q0", 0)
    result.record("population", 1, 1, "q0", 1)
    ds = result.to_xarray()
    series = ds["value"].where(ds["valid"]).sel(handle="population", qubit="q0")
    assert series.isel(point=1).mean("shot").item() == 0.5
    assert ds["duration"].attrs["units"] == "s"
    assert ds["duration"].values[1] == 5e-6
    ds["value"].values[:] = 99
    assert result.data["population"][1, 0, 0] == 0


def test_hdf5_invalid_data_rejected_and_failed_write_preserves_old_file(tmp_path):
    result = ResultSet.from_compiled(compiled_rabi())
    result.record("population", 0, 0, "q0", 1)
    path = result.save_hdf5(tmp_path / "run.h5")
    original = path.read_bytes()
    result.metadata["bad"] = object()
    with pytest.raises(TypeError):
        result.save_hdf5(path)
    assert path.read_bytes() == original
    with h5py.File(path, "r+") as archive:
        archive["observations/0/status"][0, 0, 0] = "failed"
    with pytest.raises(ValueError, match="Invalid results"):
        ResultSet.load_hdf5(path)


def test_version_one_directory_without_context_still_loads(tmp_path):
    result = ResultSet.from_compiled(compiled_rabi())
    result.save(tmp_path)
    path = tmp_path / "results.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("context")
    payload["schema_version"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert ResultSet.load(tmp_path).context == {}
