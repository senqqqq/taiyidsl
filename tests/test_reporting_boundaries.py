"""Compatibility paths must share the canonical reporting implementation."""

from importlib import import_module


def test_legacy_reporting_modules_are_canonical_aliases():
    # Identity also keeps a monkeypatch through either spelling visible to
    # functions whose globals belong to the canonical module.
    for name in (
        "visualization",
        "experiment_report",
        "output_simulator",
        "paged_output",
        "viewer_execution",
        "capture_view",
        "dds_view",
        "native_view",
        "spectrum_report",
        "spectrum_bank_report",
        "spectrum_dds_report",
    ):
        legacy = import_module(f"taiyidsl.{name}")
        canonical = import_module(f"taiyidsl.reporting.{name}")
        assert legacy is canonical
        assert canonical.__name__ == f"taiyidsl.reporting.{name}"
