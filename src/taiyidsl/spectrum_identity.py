"""Pure Spectrum identity hashes shared by compilation and host execution."""

import hashlib
import json
from collections.abc import Mapping

from .config.spectrum import INTERNAL_PLL_BENCH


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def calibration_fingerprint(profile: Mapping) -> str:
    """Bind already rendered RF samples to the exact calibration that made them."""
    return hashlib.sha256(_canonical(profile).encode()).hexdigest()


def hardware_fingerprint(contract: Mapping, observed: Mapping | None = None) -> str:
    """Bind calibration to wiring, firmware, clock and analog output settings.

    ``firmware`` comes from a read-only probe when creating measured profiles.
    A declared cable ID is an operator-managed identifier, not a cable sensor.
    Pass the complete exported card.contract, including the group's trigger
    path and ARTIQ epoch/grid fields, not only dataclasses.asdict(SpectrumCard).
    """
    values = {
        key: contract.get(key)
        for key in (
            "model",
            "serial_number",
            "firmware",
            "sample_rate_hz",
            "reference_clock_hz",
            "active_channels",
            "output_range_mv",
            "cable_id",
            "trigger_latency_samples",
            "trigger_device",
            "epoch_grid_mu",
            "epoch_offset_mu",
            "trigger_width_mu",
            "artiq_tick_seconds",
        )
    }
    values["serial_number"] = str(values["serial_number"])
    values["active_channels"] = list(values["active_channels"] or ())
    if contract.get("clock_policy") == INTERNAL_PLL_BENCH:
        values["clock_policy"] = INTERNAL_PLL_BENCH
    else:
        values["reference_clock_hz"] = values["reference_clock_hz"] or 125_000_000
    if contract.get("mode", "awg") == "dds":
        values.pop("trigger_latency_samples", None)
        values.update(
            mode="dds",
            dds_routing=contract.get("dds_routing"),
            dds_trigger_latency_ns=contract.get("dds_trigger_latency_ns"),
        )
    if observed is not None:
        values["firmware"] = observed.get("firmware")
    return hashlib.sha256(_canonical(values).encode()).hexdigest()
