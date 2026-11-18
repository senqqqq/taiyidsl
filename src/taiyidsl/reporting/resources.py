"""Locate bundled viewers and exact runtime source without importing drivers.

Runtime locations are explicit: reorganizing an implementation only changes
this catalog, not report assembly or the archived report paths.
"""

from dataclasses import dataclass
from importlib.resources import files

_ASSETS = frozenset(
    {
        "experiment_report.html",
        "output_simulator.js",
        "paged_output.js",
        "viewer_execution.js",
    }
)


def asset_bytes(name: str) -> bytes:
    """Read an installed package resource, including from a zipped package."""
    if name not in _ASSETS:
        raise ValueError(f"Unknown reporting asset: {name}")
    return files("taiyidsl.reporting").joinpath("assets", name).read_bytes()


def asset_text(name: str) -> str:
    """Decode viewer source using the same universal newlines as read_text."""
    return asset_bytes(name).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class RuntimeSource:
    """One implementation copied verbatim into a report for inspection."""

    key: str
    package_path: str
    report_path: str


RUNTIME_SOURCES = (
    RuntimeSource(
        "spectrum_runtime", "runtime/spectrum/awg.py", "runtime/spectrum_runtime.py"
    ),
    RuntimeSource(
        "spectrum_identity", "spectrum_identity.py", "runtime/spectrum_identity.py"
    ),
    RuntimeSource(
        "spectrum_dds",
        "backends/artiq_spectrum/dds.py",
        "runtime/spectrum_dds.py",
    ),
    RuntimeSource(
        "spectrum_dds_runtime",
        "runtime/spectrum/dds.py",
        "runtime/spectrum_dds_runtime.py",
    ),
)


def runtime_source_bytes(source: RuntimeSource) -> bytes:
    """Read code as data; no native module, SDK, or instrument is loaded."""
    return files("taiyidsl").joinpath(*source.package_path.split("/")).read_bytes()
