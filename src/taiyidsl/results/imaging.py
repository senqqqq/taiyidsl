"""Offline camera images, ROI counts and occupancy for fixed tweezer arrays.

An absent atom is a valid zero occupancy. A missing frame is unavailable data.
Neither occupancy nor survival alone identifies a nuclear-spin state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from .archives import _atomic_hdf5, _jsonable
from .scalar import ResultSet

_IMAGE_STATUSES = {"valid", "missing", "failed", "unexecuted"}


class ImageResults:
    """One raw image per ``(acquisition, point, shot)``, with per-site results.

    Rectangular ROIs use ``(y_start, y_stop, x_start, x_stop)`` pixel indices and
    Python's exclusive stop convention. Counts are the ROI sum after subtracting
    a scalar background per pixel or a full background image. Occupancy is
    ``counts >= threshold``. The calibration is fixed for this result object.
    """

    def __init__(
        self,
        results,
        *,
        acquisitions,
        rois,
        thresholds,
        background=None,
        classifier_version="threshold-v1",
    ):
        if not isinstance(results, ResultSet):
            raise TypeError("results must be a ResultSet")
        if not isinstance(acquisitions, Mapping) or not acquisitions:
            raise ValueError("acquisitions must describe named camera acquisitions")
        if set(acquisitions) != set(results.handles):
            raise ValueError("Acquisition names must match the result handles")
        if (
            not isinstance(rois, Mapping)
            or not rois
            or not set(rois) <= set(results.qubits)
        ):
            raise ValueError("ROIs must use known qubit/site identifiers")
        if not isinstance(thresholds, Mapping) or set(thresholds) != set(rois):
            raise ValueError("Each ROI requires a threshold")
        for roi in rois.values():
            if (
                not isinstance(roi, (list, tuple))
                or len(roi) != 4
                or any(type(v) is not int for v in roi)
                or not 0 <= roi[0] < roi[1]
                or not 0 <= roi[2] < roi[3]
            ):
                raise ValueError("ROI must be (y_start, y_stop, x_start, x_stop)")
        if any(
            isinstance(v, bool) or not np.isscalar(v) or not np.isfinite(v)
            for v in thresholds.values()
        ):
            raise ValueError("Thresholds must be finite numeric ROI counts")
        if not isinstance(classifier_version, str) or not classifier_version:
            raise ValueError("classifier_version must be nonempty")
        background = np.asarray(0 if background is None else background, dtype=float)
        if background.ndim not in {0, 2} or not np.all(np.isfinite(background)):
            raise ValueError(
                "Background must be finite scalar or two-dimensional image"
            )
        self.results = results
        self._settings_json = json.dumps(
            _jsonable(
                {
                    "acquisitions": acquisitions,
                    "rois": rois,
                    "thresholds": thresholds,
                    "classifier_version": classifier_version,
                }
            ),
            allow_nan=False,
            ensure_ascii=False,
        )
        self._background = background.copy()
        self._background.setflags(write=False)
        self.sites = tuple(rois)
        self.frames = {}
        self.timestamps = {}
        self.roi_counts = {
            h: np.full_like(results.data[h], np.nan) for h in results.handles
        }

    @classmethod
    def from_compiled(
        cls,
        compiled,
        *,
        acquisitions,
        rois,
        thresholds,
        background=None,
        classifier_version="threshold-v1",
        metadata=None,
    ):
        """Bind one image per handle/point/shot to one acquisition event.

        A shared ``acquire(..., qubits=[...])`` is one event. Independent
        captures need distinct handles, even when their timestamps match.
        Scalar ResultSet observations retain their per-qubit handle semantics.
        """
        from ..ir import acquisition_targets

        selected = set(acquisitions)
        for point in compiled.points:
            declared = {}
            for event in point.logical_events:
                if event.kind not in {"measure", "acquire"}:
                    continue
                handle = event.params["handle"]
                if handle not in selected:
                    continue
                if handle in declared:
                    raise ValueError(
                        f"Image handle {handle!r} requires one acquisition event "
                        f"per shot at point {point.index}; use distinct handles "
                        "for independent captures, or one shared acquire()"
                    )
                declared[handle] = set(acquisition_targets(event))
            if selected - declared.keys():
                raise ValueError(
                    "Acquisition names must be declared by the compiled experiment"
                )
            for handle in acquisitions:
                if not set(rois) <= declared[handle]:
                    raise ValueError(
                        f"ROI sites are not all measured by acquisition {handle!r}"
                    )
        result = ResultSet.from_compiled(
            compiled, handles=list(acquisitions), metadata=metadata
        )
        return cls(
            result,
            acquisitions=acquisitions,
            rois=rois,
            thresholds=thresholds,
            background=background,
            classifier_version=classifier_version,
        )

    @property
    def context(self):
        return self.results.context

    @property
    def acquisitions(self):
        return json.loads(self._settings_json)["acquisitions"]

    @property
    def rois(self):
        return json.loads(self._settings_json)["rois"]

    @property
    def thresholds(self):
        return json.loads(self._settings_json)["thresholds"]

    @property
    def classifier_version(self):
        return json.loads(self._settings_json)["classifier_version"]

    def _classify(self, image):
        image = np.asarray(image)
        if image.ndim != 2 or image.dtype.kind not in "ui":
            raise TypeError("Raw camera frame must be a two-dimensional integer array")
        if image.size == 0 or np.any(image < 0):
            raise ValueError("Raw camera frame must be nonempty and nonnegative")
        if self._background.ndim == 2 and self._background.shape != image.shape:
            raise ValueError("Background image shape differs from raw frame")
        counts = {}
        for site, (y0, y1, x0, x1) in self.rois.items():
            if y1 > image.shape[0] or x1 > image.shape[1]:
                raise ValueError(f"ROI for {site!r} exceeds frame dimensions")
            background = (
                self._background[y0:y1, x0:x1]
                if self._background.ndim == 2
                else self._background
            )
            counts[site] = float((image[y0:y1, x0:x1].astype(float) - background).sum())
        return image, counts

    def record_frame(
        self, acquisition, point, shot, image=None, *, status="valid", timestamp=None
    ):
        """Import an already acquired frame; never contacts a camera or hardware."""
        if acquisition not in self.results.handles:
            raise KeyError(f"Unknown acquisition {acquisition!r}")
        if status not in _IMAGE_STATUSES:
            raise ValueError(
                "Image status must be valid, missing, failed or unexecuted"
            )
        self.results._index(point, shot, self.sites[0])
        if timestamp is not None and (
            isinstance(timestamp, bool) or not np.isfinite(timestamp)
        ):
            raise ValueError("Timestamp must be finite seconds")
        if status == "valid":
            image, counts = self._classify(image)
        elif image is not None:
            raise ValueError(
                "A missing/failed/unexecuted acquisition cannot contain a frame"
            )
        key = (acquisition, int(point), int(shot))
        if status == "valid":
            self.frames[key] = image.copy()
            self.frames[key].setflags(write=False)
        else:
            self.frames.pop(key, None)
        self.timestamps.pop(key, None)
        if timestamp is not None:
            self.timestamps[key] = float(timestamp)
        thresholds = self.thresholds
        for site in self.sites:
            idx = self.results._index(point, shot, site)
            self.roi_counts[acquisition][idx] = (
                counts[site] if status == "valid" else np.nan
            )
            self.results.record(
                acquisition,
                point,
                shot,
                site,
                float(counts[site] >= thresholds[site]) if status == "valid" else None,
                status=status,
                labels={
                    "observable": "occupancy",
                    "classifier_version": self.classifier_version,
                },
            )
        pair = (int(point), int(shot))
        if status != "unexecuted" and pair not in self.results.execution_order:
            self.results.execution_order.append(pair)

    def to_xarray(self):
        """Return ROI counts/occupancy/status/valid on acquisition,point,shot,site.

        Raw frames remain in ``frames`` to preserve dtype and avoid filling
        missing images with fake zero pixels. Different acquisitions may have
        different frame dimensions.
        """
        ds = (
            self.results.to_xarray()
            .rename({"handle": "acquisition", "qubit": "site", "value": "occupancy"})
            .sel(site=list(self.sites))
        )
        indices = [self.results.qubits.index(site) for site in self.sites]
        ds["roi_counts"] = (
            ("acquisition", "point", "shot", "site"),
            np.stack([self.roi_counts[h][..., indices] for h in self.results.handles]),
        )
        ds["roi_counts"].attrs["units"] = "background-subtracted camera counts"
        ds["occupancy"].attrs["meaning"] = "1=atom present; 0=empty; NaN=unavailable"
        frame_shape = (
            len(self.results.handles),
            len(self.results.coordinates),
            self.results.shots,
        )
        frame_valid = np.zeros(frame_shape, dtype=bool)
        timestamp = np.full(frame_shape, np.nan)
        for handle, point, shot in self.frames:
            frame_valid[self.results.handles.index(handle), point, shot] = True
        for (handle, point, shot), value in self.timestamps.items():
            timestamp[self.results.handles.index(handle), point, shot] = value
        ds["frame_valid"] = (("acquisition", "point", "shot"), frame_valid)
        ds["timestamp"] = (("acquisition", "point", "shot"), timestamp)
        ds["timestamp"].attrs["units"] = "s"
        ds["timestamp"].attrs["reference"] = (
            "caller-supplied; document clock in acquisition metadata"
        )
        ds.attrs["imaging_settings_json"] = self._settings_json
        return ds

    def survival(self, *, pre="pre", post="post"):
        """Conditional survival given pre-occupancy and valid pre/post images.

        Post-image zeros stay in the denominator. This is an occupancy statistic,
        not a loss-corrected spin population or a readout-fidelity correction.
        """
        import xarray as xr

        if pre == post:
            raise ValueError("Pre and post acquisitions must be different")
        ds = self.to_xarray()
        before, after = ds.sel(acquisition=pre), ds.sel(acquisition=post)
        valid = before.valid & after.valid & (before.occupancy == 1)
        count = valid.sum("shot")
        survived = ((after.occupancy == 1) & valid).sum("shot")
        probability = survived / count.where(count > 0)
        return xr.Dataset(
            {
                "survival": probability,
                "valid_counts": count,
                "survivors": survived,
                "standard_error": np.sqrt(
                    probability * (1 - probability) / count.where(count > 0)
                ),
            },
            attrs={
                "pre": pre,
                "post": post,
                "selection": "pre occupied, both acquisitions valid",
                "uncertainty": "binomial plug-in standard error",
            },
        )

    def save_hdf5(self, path):
        """Atomically archive raw images, processing calibration and full context."""
        with _atomic_hdf5(path) as archive:
            self.results._write_hdf5(archive)
            archive.attrs["image_schema_version"] = 1
            archive.create_dataset("imaging_settings", data=self._settings_json)
            archive.create_dataset("background", data=self._background)
            for i, handle in enumerate(self.results.handles):
                archive[f"observations/{i}"].create_dataset(
                    "roi_counts", data=self.roi_counts[handle], compression="gzip"
                )
            frames = archive.create_group("frames")
            for i, (key, image) in enumerate(self.frames.items()):
                dataset = frames.create_dataset(str(i), data=image, compression="gzip")
                dataset.attrs["key"] = json.dumps(key)
            archive.create_dataset(
                "timestamps",
                data=json.dumps(
                    [[*key, value] for key, value in self.timestamps.items()]
                ),
            )
        return Path(path)

    @classmethod
    def load_hdf5(cls, path):
        import h5py

        with h5py.File(path, "r") as archive:
            if archive.attrs.get("image_schema_version") != 1:
                raise ValueError("Unsupported image archive schema")
            result = cls(
                ResultSet._read_hdf5(archive),
                background=archive["background"][()],
                **json.loads(archive["imaging_settings"].asstr()[()]),
            )
            for i, handle in enumerate(result.results.handles):
                counts = archive[f"observations/{i}/roi_counts"][()]
                if counts.shape != result.roi_counts[handle].shape:
                    raise ValueError("ROI count shape disagrees with observations")
                result.roi_counts[handle] = counts
            for dataset in archive["frames"].values():
                key = tuple(json.loads(dataset.attrs["key"]))
                if (
                    key in result.frames
                    or len(key) != 3
                    or key[0] not in result.results.handles
                ):
                    raise ValueError("Invalid raw frame key")
                image, counts = result._classify(dataset[()])
                for site in result.sites:
                    idx = result.results._index(key[1], key[2], site)
                    if (
                        result.results.status[key[0]][idx] != "valid"
                        or result.roi_counts[key[0]][idx] != counts[site]
                        or result.results.data[key[0]][idx]
                        != float(counts[site] >= result.thresholds[site])
                    ):
                        raise ValueError(
                            "Saved image analysis disagrees with raw frame/calibration"
                        )
                image.setflags(write=False)
                result.frames[key] = image
            for handle in result.results.handles:
                for site in result.sites:
                    qi = result.results.qubits.index(site)
                    status = result.results.status[handle][..., qi]
                    counts = result.roi_counts[handle][..., qi]
                    if not set(np.unique(status)) <= _IMAGE_STATUSES:
                        raise ValueError("Invalid image status")
                    if np.any((status != "valid") & ~np.isnan(counts)):
                        raise ValueError(
                            "Invalid acquisitions cannot contain ROI counts"
                        )
                    for point, shot in zip(*np.where(status == "valid")):
                        if (handle, int(point), int(shot)) not in result.frames:
                            raise ValueError("Valid image observation has no raw frame")
            for handle, point, shot, timestamp in json.loads(
                archive["timestamps"].asstr()[()]
            ):
                if handle not in result.results.handles or not np.isfinite(timestamp):
                    raise ValueError("Invalid frame timestamp")
                result.results._index(point, shot, result.sites[0])
                result.timestamps[(handle, point, shot)] = timestamp
        return result
