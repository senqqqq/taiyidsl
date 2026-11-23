"""Portable, content-addressed source samples shared by the DSL and AWG replay."""

import base64
import binascii
import hashlib

import numpy as np


def validate_sample_values(samples: np.ndarray) -> None:
    # Computing a complex magnitude can round a unit phasor up by one ulp.
    limit = np.nextafter(1.0, 2.0) if np.iscomplexobj(samples) else 1.0
    if not np.all(np.isfinite(samples)) or np.any(np.abs(samples) > limit):
        raise ValueError("Samples must be finite with magnitude at most one")


def sample_digest(dtype: str, raw: bytes) -> str:
    digest = hashlib.sha256(dtype.encode("ascii"))
    digest.update(raw)
    return digest.hexdigest()


def decode_sample_asset(digest: str, asset: dict) -> np.ndarray:
    """Verify and return a read-only array backed by the decoded little-endian bytes."""
    if not isinstance(asset, dict):
        raise ValueError("Invalid sampled pulse asset")
    dtype = asset.get("dtype")
    count = asset.get("count")
    data = asset.get("data")
    if dtype not in ("<f8", "<c16") or type(count) is not int or count < 1:
        raise ValueError("Invalid sampled pulse asset dtype or count")
    size = count * np.dtype(dtype).itemsize
    if not isinstance(data, str) or len(data) != 4 * ((size + 2) // 3):
        raise ValueError("Invalid sampled pulse asset length")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid sampled pulse asset encoding") from exc
    if len(raw) != size or sample_digest(dtype, raw) != digest:
        raise ValueError("Sampled pulse asset digest or length mismatch")
    samples = np.frombuffer(raw, dtype=dtype)
    validate_sample_values(samples)
    return samples


def point_programs(point):
    """Visit card programs in a serialized compiled point, playback point or bank."""
    if "program" in point:
        yield point["program"]
    for event in point.get("physical_events", ()):
        if event["kind"] == "spectrum_shot":
            yield event["params"]["program"]
        elif event["kind"] == "spectrum_program":
            yield from (card["program"] for card in event["params"]["cards"].values())
    for group in point.get("groups", ()):
        yield from (card["program"] for card in group["cards"].values())


def pack_program_assets(programs, assets):
    """Move sources from detached program dictionaries to one artifact-wide table."""
    for program in programs:
        sources = program.pop("sample_assets", {})
        if sources:
            for digest, source in sources.items():
                if assets.setdefault(digest, source) != source:
                    raise ValueError("Conflicting sampled pulse assets for one digest")
            program["source_sample_digests"] = sorted(sources)


def restore_program_assets(programs, assets):
    """Attach shared source records by reference when reading an exported artifact."""
    for program in programs:
        refs = program.get("source_sample_digests")
        if refs is not None:
            if (
                not isinstance(refs, list)
                or not all(isinstance(key, str) for key in refs)
                or len(set(refs)) != len(refs)
            ):
                raise ValueError("Invalid sampled pulse asset references")
            if any(key not in assets for key in refs):
                raise ValueError("Referenced sampled pulse asset is missing")
            program["sample_assets"] = {key: assets[key] for key in refs}
            del program["source_sample_digests"]


def _manifest_programs(manifest):
    for collection in ("points", "banks"):
        for point in manifest.get(collection, ()):
            yield from point_programs(point)


def pack_sample_manifest(manifest):
    assets = {}
    pack_program_assets(_manifest_programs(manifest), assets)
    if assets:
        manifest["sample_assets"] = assets


def expand_sample_manifest(manifest):
    """Expand an on-disk Spectrum manifest for individual program replay in memory."""
    if "sample_assets" in manifest:
        restore_program_assets(_manifest_programs(manifest), manifest["sample_assets"])
        del manifest["sample_assets"]
    return manifest
