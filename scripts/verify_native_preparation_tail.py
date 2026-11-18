"""Trace official Zotino timing offline; never instantiate connected devices.

Run with ``scripts/artiq-python.ps1 scripts/verify_native_preparation_tail.py``.
The real driver bodies execute under ARTIQ's interpreter time manager. Only
the RTIO sink, TTL output and core time conversion are simulated. This checks
digital scheduling, not physical output latency, analog settling or slack.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch


class TraceTime:
    """Minimal serial timeline; unexpected parallel operations fail closed."""

    def __init__(self):
        self.cursor = 0
        self.moves = []

    def get_time_mu(self):
        return self.cursor

    def set_time_mu(self, value):
        self.moves.append({"from_mu": self.cursor, "to_mu": int(value)})
        self.cursor = int(value)

    def take_time_mu(self, duration):
        self.set_time_mu(self.cursor + int(duration))

    def take_time(self, duration):
        self.take_time_mu(round(duration / 1e-9))


def _bind_embedded(instance, name):
    function = getattr(type(instance), name).artiq_embedded.function
    setattr(instance, name, MethodType(function, instance))


def verify():
    project = Path(__file__).resolve().parents[1]
    toolchain = project / ".toolchains" / "artiq9"
    if not Path(sys.executable).resolve().is_relative_to(toolchain.resolve()):
        raise RuntimeError("Run only with scripts/artiq-python.ps1 (official ARTIQ9)")

    import artiq
    from artiq.coredevice import ad53xx, spi2, zotino
    from artiq.language import core as language_core

    if not artiq.__version__.startswith("9."):
        raise RuntimeError(f"Expected ARTIQ9, got {artiq.__version__}")

    timeline = TraceTime()
    events = []

    def rtio_output(target, data):
        events.append(
            {
                "kind": "spi_write",
                "time_mu": timeline.cursor,
                "target": int(target),
                "data": int(data),
            }
        )

    class TraceTTL:
        def on(self):
            events.append({"kind": "ldac_high", "time_mu": timeline.cursor})

        def off(self):
            events.append({"kind": "ldac_low", "time_mu": timeline.cursor})

    def forbid_hardware(*_args, **_kwargs):
        raise RuntimeError("Hardware kernel execution is forbidden in this trace")

    core = SimpleNamespace(
        seconds_to_mu=lambda seconds: round(seconds / 1e-9), run=forbid_hardware
    )
    # Allocate the genuine SPI class without its device-manager constructor.
    # Its portable transfer-duration formula and embedded write body stay real.
    bus = spi2.SPIMaster.__new__(spi2.SPIMaster)
    bus.channel = 1
    bus.ref_period_mu = 8
    bus.core = core
    _bind_embedded(bus, "write")
    devices = {"core": core, "spi": bus, "ldac": TraceTTL()}
    driver = zotino.Zotino(
        SimpleNamespace(get=devices.__getitem__), "spi", ldac_device="ldac"
    )
    for name in ("set_dac", "set_dac_mu", "write_dac_mu", "load"):
        _bind_embedded(driver, name)

    previous_manager = language_core._time_manager
    language_core.set_time_manager(timeline)
    try:
        with patch.object(spi2, "rtio_output", rtio_output):
            # The actual preparation's final wait and AOM-off edge precede DAC.
            language_core.delay_mu(15_000_000)
            events.append({"kind": "aom_off", "time_mu": timeline.cursor})
            entry = language_core.now_mu()
            driver.set_dac([-0.95, 0.0], [2, 3])
            returned = language_core.now_mu()
            language_core.delay_mu(2 * bus.ref_period_mu)
            wrapper_end = language_core.now_mu()
    finally:
        language_core.set_time_manager(previous_manager)

    spi_writes = [event for event in events if event["kind"] == "spi_write"]
    ldac_low = [event["time_mu"] for event in events if event["kind"] == "ldac_low"]
    ldac_high = [event["time_mu"] for event in events if event["kind"] == "ldac_high"]
    expected_write_times = [
        entry - 1500 - 2 * bus.xfer_duration_mu,
        entry - 1500 - bus.xfer_duration_mu,
    ]
    checks = {
        "two_spi_prewrites_at_expected_times": [e["time_mu"] for e in spi_writes]
        == expected_write_times,
        "spi_transfers_finish_before_ldac": bool(spi_writes)
        and all(e["time_mu"] + bus.xfer_duration_mu < entry for e in spi_writes),
        "ldac_falls_at_entry_cursor": ldac_low == [entry],
        "ldac_rises_16_mu_after_entry": ldac_high == [entry + 16],
        "set_dac_returns_unchanged_cursor": returned == entry,
        "explicit_drain_reaches_last_digital_edge": ldac_high == [wrapper_end],
        "strict_tail_duration_matches_declaration": wrapper_end == 15_000_016,
    }
    if not all(checks.values()):
        raise AssertionError(f"Unexpected official driver timing: {checks}")

    sources = {}
    for module in (ad53xx, zotino, spi2, language_core):
        path = Path(inspect.getfile(module)).resolve()
        sources[module.__name__] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {
        "passed": True,
        "mode": "offline_interpretation_of_official_embedded_driver_functions",
        "artiq_version": artiq.__version__,
        "python_executable": sys.executable,
        "driver_sources": sources,
        "hardware_connected": False,
        "hardware_kernels_executed": False,
        "time_unit_seconds": 1e-9,
        "spi_ref_period_mu": bus.ref_period_mu,
        "spi_transfer_duration_mu": bus.xfer_duration_mu,
        "set_dac_entry_mu": entry,
        "set_dac_return_mu": returned,
        "wrapper_end_mu": wrapper_end,
        "checks": checks,
        "events_in_submission_order": events,
        "cursor_movements": timeline.moves,
        "limitations": [
            "SPI is assumed already configured for 24 bits, divider 4.",
            "RTIO writes and TTL edges are recorded, not sent to hardware.",
            "No CPU execution time, slack, underflow or transport model.",
            "Digital scheduling only; no analog settling or optical calibration.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "output/native_preparation_tail_verification.json",
    )
    args = parser.parse_args()
    result = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output), **result["checks"]}, indent=2))


if __name__ == "__main__":
    main()
