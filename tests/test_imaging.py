import h5py
import numpy as np
import pytest

from taiyidsl import Experiment, Qubit, delay, measure, reference_lab, us
from taiyidsl.results.imaging import ImageResults


def image_results():
    q = Qubit("q0")
    from taiyidsl import SweepParameter

    exp = Experiment("images", [q])
    with exp.average(4):
        with exp.sweep(SweepParameter("duration", [0, 5e-6])):
            with exp.realtime():
                measure(q, handle="pre")
                delay(q.readout, 1 * us)
                measure(q, handle="post")
    compiled = reference_lab([q]).compile(exp)
    return ImageResults.from_compiled(
        compiled,
        acquisitions={"pre": {"camera": "cam0"}, "post": {"camera": "cam0"}},
        rois={"q0": (0, 2, 0, 2)},
        thresholds={"q0": 10},
        background=1,
        classifier_version="threshold-v1",
    )


def test_image_pipeline_missing_empty_and_loading_are_distinct():
    result = image_results()
    bright = np.full((3, 4), 5, dtype=np.uint16)
    dark = np.ones((3, 4), dtype=np.uint16)
    # Survived, lost, never loaded, and missing post image.
    for shot, pre, post in [(0, bright, bright), (1, bright, dark), (2, dark, dark)]:
        result.record_frame("pre", 0, shot, pre)
        result.record_frame("post", 0, shot, post)
    result.record_frame("pre", 0, 3, bright)
    result.record_frame("post", 0, 3, status="missing")
    bright[:] = 99
    ds = result.to_xarray()
    assert ds.roi_counts.sel(acquisition="pre", site="q0").values[0, 0] == 16
    assert ds.occupancy.sel(acquisition="post", site="q0").values[0, 1] == 0
    assert np.isnan(ds.occupancy.sel(acquisition="post", site="q0").values[0, 3])
    summary = result.survival()
    assert summary.survival.sel(site="q0").values[0] == 0.5
    assert summary.valid_counts.sel(site="q0").values[0] == 2
    assert summary.survivors.sel(site="q0").values[0] == 1
    assert summary.valid_counts.sel(site="q0").values[1] == 0
    assert np.isnan(summary.survival.sel(site="q0").values[1])
    assert result.frames[("pre", 0, 0)].dtype == np.uint16
    assert result.frames[("pre", 0, 0)][0, 0] == 5


def test_image_hdf5_roundtrip_preserves_raw_data_and_processing(tmp_path):
    result = image_results()
    result.record_frame("pre", 0, 0, np.full((3, 4), 7, np.uint16), timestamp=123.4)
    result.record_frame("post", 0, 0, np.ones((3, 4), np.uint16))
    path = result.save_hdf5(tmp_path / "images.h5")
    restored = ImageResults.load_hdf5(path)
    assert restored.context == result.context
    assert restored.acquisitions == result.acquisitions
    assert restored.classifier_version == "threshold-v1"
    assert restored.timestamps[("pre", 0, 0)] == 123.4
    np.testing.assert_equal(
        restored.to_xarray().roi_counts.values, result.to_xarray().roi_counts.values
    )
    np.testing.assert_equal(
        restored.frames[("pre", 0, 0)], result.frames[("pre", 0, 0)]
    )
    with h5py.File(path) as archive:
        assert len(archive["frames"]) == 2  # No per-site copies of raw frames.


def test_bad_roi_and_frame_are_rejected_without_partial_record():
    result = image_results()
    with pytest.raises(ValueError, match="ROI"):
        result.record_frame("pre", 0, 0, np.zeros((1, 1), np.uint16))
    assert result.frames == {}
    with pytest.raises(TypeError, match="integer"):
        result.record_frame("pre", 0, 0, np.zeros((3, 4)))
    with pytest.raises(ValueError, match="status"):
        result.record_frame("pre", 0, 0, status="atom_lost")
    with pytest.raises(ValueError, match="different"):
        result.survival(pre="pre", post="pre")


def test_shared_acquisition_analyzes_two_sites_without_duplicate_frames(tmp_path):
    from taiyidsl.dsl import acquire
    from taiyidsl.results import synthesize

    qubits = [Qubit("q0"), Qubit("q1")]
    exp = Experiment("array", qubits, metadata={"experiment_type": "rabi"})
    with exp.average(2):
        with exp.realtime():
            for handle in ("pre", "post"):
                acquire(
                    "q0.readout",
                    duration=10 * us,
                    handle=handle,
                    qubits=qubits,
                    metadata={"detector": "cam0"},
                )
                delay("q0.readout", 1 * us)
    compiled = reference_lab(qubits).compile(exp)
    result = ImageResults.from_compiled(
        compiled,
        acquisitions={"pre": {}, "post": {}},
        rois={"q0": (0, 1, 0, 1), "q1": (0, 1, 1, 2)},
        thresholds={"q0": 5, "q1": 5},
    )
    result.record_frame("pre", 0, 0, np.array([[9, 9]], np.uint16))
    result.record_frame("post", 0, 0, np.array([[9, 0]], np.uint16))
    np.testing.assert_equal(result.survival().survival.values, [[1, 0]])
    ds = result.to_xarray()
    assert ds.frame_valid.sel(acquisition="post").values.tolist() == [[True, False]]
    restored = ImageResults.load_hdf5(result.save_hdf5(tmp_path / "shared.h5"))
    assert len(restored.frames) == 2
    assert (
        restored.context["compiled_experiment"]["ir"]
        == result.context["compiled_experiment"]["ir"]
    )
    synthetic = synthesize(compiled, max_shots=1)
    assert synthetic.valid_counts("pre").tolist() == [[1, 1]]


def test_archive_detects_changed_raw_image_and_unknown_acquisition(tmp_path):
    result = image_results()
    result.record_frame("pre", 0, 0, np.full((3, 4), 7, np.uint16))
    path = result.save_hdf5(tmp_path / "changed.h5")
    with h5py.File(path, "r+") as archive:
        archive["frames/0"][0, 0] = 0
    with pytest.raises(ValueError, match="disagrees"):
        ImageResults.load_hdf5(path)
    with pytest.raises(KeyError, match="Unknown"):
        result.record_frame("undeclared", 0, 0, np.ones((3, 4), np.uint16))


@pytest.mark.parametrize("kind", ["serial_acquire", "parallel_acquire", "measure"])
def test_independent_captures_cannot_alias_one_image_handle(kind):
    from taiyidsl import ResultSet, acquire

    qubits = [Qubit("q0"), Qubit("q1")]
    exp = Experiment("independent_images", qubits)
    with exp.average(1):
        with exp.realtime():
            block = exp.serial() if kind == "serial_acquire" else exp.parallel()
            with block:
                for q in qubits:
                    with exp.serial():
                        if kind == "measure":
                            measure(q, handle="population")
                        else:
                            signal = (
                                qubits[0].readout
                                if kind == "serial_acquire"
                                else q.readout
                            )
                            acquire(
                                signal,
                                duration=10 * us,
                                handle="population",
                                qubits=[q],
                            )
                            delay(signal, 1 * us)
    compiled = reference_lab(qubits).compile(exp)
    # Independent scalar observations may share a handle across different sites.
    assert ResultSet.from_compiled(compiled).qubits == ("q0", "q1")
    with pytest.raises(ValueError, match="population.*one acquisition event.*point 0"):
        ImageResults.from_compiled(
            compiled,
            acquisitions={"population": {}},
            rois={"q0": (0, 1, 0, 1), "q1": (0, 1, 1, 2)},
            thresholds={"q0": 5, "q1": 5},
        )


def test_image_acquisition_identity_is_checked_at_every_scan_point():
    from taiyidsl import SweepParameter, acquire

    qubits = [Qubit("q0"), Qubit("q1")]
    exp = Experiment("changing_capture_structure", qubits)
    mode = SweepParameter("mode", [0, 1], "dimensionless")
    with exp.average(1):
        with exp.sweep(mode):
            with exp.realtime():
                with exp.case(mode, 0):
                    acquire(
                        "q0.readout", duration=10 * us, handle="image", qubits=qubits
                    )
                with exp.case(mode, 1):
                    for q in qubits:
                        acquire(
                            "q0.readout", duration=10 * us, handle="image", qubits=[q]
                        )
                        delay("q0.readout", 1 * us)
    compiled = reference_lab(qubits).compile(exp)
    with pytest.raises(ValueError, match="image.*one acquisition event.*point 1"):
        ImageResults.from_compiled(
            compiled,
            acquisitions={"image": {}},
            rois={"q0": (0, 1, 0, 1)},
            thresholds={"q0": 5},
        )


def test_unselected_scalar_handles_do_not_restrict_camera_adapter():
    from taiyidsl import acquire

    qubits = [Qubit("q0"), Qubit("q1")]
    exp = Experiment("mixed_detectors", qubits)
    with exp.average(1):
        with exp.realtime():
            with exp.parallel():
                for q in qubits:
                    measure(q, handle="scalar")
            delay("q0.readout", 1 * us)
            acquire("q0.readout", duration=10 * us, handle="image", qubits=qubits)
    images = ImageResults.from_compiled(
        reference_lab(qubits).compile(exp),
        acquisitions={"image": {}},
        rois={"q0": (0, 1, 0, 1)},
        thresholds={"q0": 5},
    )
    images.record_frame("image", 0, 0, np.array([[9]], np.uint16))
    assert images.results.status["image"].tolist() == [[["valid", "unexecuted"]]]
