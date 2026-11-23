# Experiment results and offline image analysis

`ResultSet` stores scalar observations for individual shots. `ImageResults` adds raw camera frames for a fixed tweezer array, ROI integrals and occupancy classification. Both operate offline. The [imaging example](../examples/offline_imaging_workflow.py) scans a flat-top duration and uses synthetic images to exercise the data path. It writes HDF5, statistical plots, a timeline and ARTIQ source. The images test the workflow and carry no measured or simulated atomic dynamics.

```powershell
uv run --locked python examples/offline_imaging_workflow.py
```

## Capture the experiment context

`ResultSet.from_compiled(compiled, metadata=...)` and `ImageResults.from_compiled(...)` capture the context used at compile time. `result.context` returns an independent copy.

| Context key | Contents |
|---|---|
| `lab_config` | Device topology, logical signal mappings, trigger sequences, calibrations and backend name |
| `optics_config` | Schema 2 optical working points, or `None` when no optical table was used |
| `qubits` | Parameters and revision of each qubit |
| `experiment` | Experiment name, metadata, parameters, qubits and structured IR |
| `compiled_experiment` | Complete compiled artifact, including scan axes, point-specific logical and physical events, parameters, diagnostics and provenance |
| `parameters` | General parameter table |

Snapshots of real `CompiledExperiment` objects carry `complete=True`. Lightweight synthetic inputs with missing compilation context carry `complete=False`. A direct `ResultSet(...)` constructor can only use the context supplied to it.

Place experiment purpose, descriptions and readout conventions in `Experiment(..., metadata={...})`. Pass acquisition notes through the result constructor's `metadata={...}`. Use JSON-serializable strings, numbers, lists and dictionaries. The context retains experiment descriptions, and `ResultSet.metadata` or `ImageResults.results.metadata` holds acquisition notes.

The archive captures serialized data and IR. It does not pickle live objects, reconstruct executable builders, or bundle experiment source, external camera SDKs or file contents referenced by external assets. Raw frames recorded in `ImageResults` are embedded in HDF5.

## One frame for several sites

This standalone offline example uses `acquire` to declare one trigger and its set of sites. `duration` determines trigger width. The exposure recorded in metadata documents a camera setting and reserves no additional time. A laboratory implementation supplies illumination and loading.

```python
from pathlib import Path

import numpy as np

from taiyidsl import (
    Experiment,
    ImageResults,
    Qubit,
    SweepParameter,
    acquire,
    delay,
    reference_lab,
    us,
)

qubits = [Qubit("q0"), Qubit("q1")]
exp = Experiment(
    "camera_example",
    qubits,
    metadata={"description": "Offline occupancy analysis", "synthetic": True},
)
hold = SweepParameter("hold_time", [2, 4], unit="us")
with exp.average(3):
    with exp.sweep(hold):
        with exp.realtime():
            acquire(
                "q0.readout",
                duration=10 * us,
                handle="pre",
                qubits=qubits,
                metadata={"detector": "camera0", "exposure_s": 100e-6},
            )
            delay("q0.readout", 100 * us)
            delay("q0.raman", hold)
            acquire(
                "q0.readout",
                duration=10 * us,
                handle="post",
                qubits=qubits,
                metadata={"detector": "camera0", "exposure_s": 100e-6},
            )
            delay("q0.readout", 100 * us)
compiled = reference_lab(qubits).compile(exp)

images = ImageResults.from_compiled(
    compiled,
    acquisitions={"pre": {"camera": "camera0"}, "post": {"camera": "camera0"}},
    rois={"q0": (0, 1, 0, 1), "q1": (0, 1, 1, 2)},
    thresholds={"q0": 5, "q1": 5},
    background=1,
    classifier_version="threshold-v1",
    metadata={"description": "Artificial test frames", "synthetic": True},
)
for point, shot in compiled.execution_order():
    images.record_frame("pre", point, shot, np.array([[9, 9]], dtype=np.uint16))
    if shot == 2:
        images.record_frame("post", point, shot, status="missing")
    else:
        frame = np.array([[9, 9 if shot == 0 else 1]], dtype=np.uint16)
        images.record_frame("post", point, shot, frame)

output = Path("output/camera_example")
archive = images.save_hdf5(output / "results.h5")
restored = ImageResults.load_hdf5(archive)
assert restored.context["experiment"]["name"] == "camera_example"
```

An ROI is `(y_start, y_stop, x_start, x_stop)` with exclusive stops. Background can be a scalar per pixel or a two-dimensional image of the same shape as the frame. Classification converts pixels to floating point, subtracts background and integrates each ROI. A site is occupied when `counts >= threshold`. ROIs, thresholds, background and classifier version accompany the archive.

Each integer frame is stored once under `(acquisition, point, shot)` in `images.frames`. Several sites integrate different ROIs of the same frame. Pass `timestamp=...` in seconds and describe its clock in acquisition metadata. Different acquisitions can have different frame shapes. The API accepts an already-read NumPy array, including one loaded with `np.load(path, allow_pickle=False)`.

At each point `ImageResults.from_compiled` requires exactly one logical acquisition event per selected handle. One `acquire(..., qubits=[q0, q1])` associates both sites with one frame. Two calls to `acquire` or `measure` declare two events, including when they occur simultaneously. Give each independent trigger its own handle. Ambiguous handles raise an error when constructing the image results.

Every handle in an `ImageResults` object must cover all its ROIs. For handles addressing different site sets, create separate objects with the matching `acquisitions` and `rois`. Calling `record_frame` again with the same key explicitly replaces the earlier frame. Scalar observations retain their `(handle, qubit)` indexing in `ResultSet`.

## Slice, average and plot with xarray

Continue the preceding example:

```python
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ds = restored.to_xarray()
post = ds.sel(acquisition="post", site="q0")
first_points = post.isel(point=slice(0, 2))
counts = first_points.roi_counts.where(first_points.valid)
counts.mean(dim="shot").plot(x="hold_time")
plt.savefig(output / "mean_counts.png", dpi=150)
plt.close()

summary = restored.survival(pre="pre", post="post")
print(summary.sel(site="q1")["survival"].values)  # [0.5, 0.5]
print(summary.sel(site="q1")["valid_counts"].values)  # [2, 2]
```

`ImageResults.to_xarray()` returns `roi_counts`, `occupancy`, `status` and `valid` over dimensions `acquisition, point, shot, site`. `frame_valid` and `timestamp` omit the site dimension. Raw pixels live in `frames` and the HDF5 archive. Missing frames have no synthetic pixel matrix.

Scan values are SI coordinates of the `point` dimension. Units live in each coordinate's `attrs["units"]`, so `hold_time` contains seconds. Stable integer point indices preserve repeated scan values. For example, `ds.where(ds.hold_time <= 3e-6, drop=True)` selects points by duration. The xarray view owns its arrays independently of the archived result.

`ResultSet.to_xarray()` exposes `value`, `status` and `valid` over `handle, point, shot, qubit`. Average numeric observations with `value.where(valid).mean("shot")` or call `aggregate()` for the mean and valid count together. Retain status strings for filtering.

## Empty sites, missing frames and the survival denominator

| Observation | Interpretation |
|---|---|
| `occupancy=0, status="valid"` | Successful image classified as an empty site |
| `status="missing"` or `"failed"` | Unavailable acquisition, with NaN occupancy and ROI counts |
| `status="unexecuted"` | Acquisition has yet to run |

Use initial occupancy to select successfully loaded sites. A valid final image of an empty site contributes to the survival denominator. `survival()` includes samples with an initially occupied site and valid pre- and post-images, then counts the subset with final occupancy as survivors. It returns `survival`, `valid_counts`, `survivors` and `standard_error`. The probability is NaN when the denominator is zero. The error is the binomial plug-in standard error. Confidence intervals and readout corrections require separate analysis. The archive retains every raw shot.

After push-out, an empty site may reflect state-selective removal. Inferring nuclear-spin populations from occupancy or survival requires the readout protocol, appropriate references and calibrated readout response.

## HDF5 and directory formats

`save_hdf5()` writes a temporary file, flushes, closes and fsyncs it before replacing the destination. A failed write leaves the published archive intact. An image archive has the following layout.

```text
/manifest                   JSON dimensions, notes, provenance and complete context
/observations/<index>/data  per-shot scalars, or occupancy for an image archive
/observations/<index>/status
/observations/<index>/roi_counts   image archives only
/imaging_settings           acquisition descriptions, ROIs, thresholds and classifier version
/background
/frames/<index>             raw integer images with acquisition/point/shot in the key attribute
/timestamps
```

For this TaiyiDSL HDF5 format, read with `ResultSet.load_hdf5()` or `ImageResults.load_hdf5()`, then call `to_xarray()`. The loader checks raw frames, ROIs and thresholds against the stored image analysis. `xr.open_dataset()` expects a supported dataset format such as NetCDF and does not implement this schema.

`ResultSet.save(directory)` also supports an NPZ and JSON directory archive. Schema 2 includes context, and `ResultSet.load(directory)` also accepts schema 1. Use the loading method that corresponds to the chosen format.

The archive design draws on shot-file analysis, run datasets and labeled arrays. These references describe the respective systems' data models. [labscript/lyse shot files and analysis results](https://docs.labscriptsuite.org/projects/lyse/en/stable/api/_autosummary/lyse.Run/), [ARTIQ datasets and run archives](https://m-labs.hk/artiq/manual/using_data_interfaces.html), [xarray indexing](https://docs.xarray.dev/en/stable/user-guide/indexing.html) and [I/O](https://docs.xarray.dev/en/stable/user-guide/io.html)
