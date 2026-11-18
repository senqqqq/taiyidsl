r"""Audit RAM rearm cursor costs using installed ARTIQ driver method bodies.

Run with the separate ARTIQ interpreter, for example::

    .\scripts\artiq-python.ps1 examples/artiq_ram_timing.py --proto-rev 9

This is a host-side driver cursor model, not kernel execution or hardware timing
measurement. RTIO output calls are replaced with a local recorder, and the core
object has no networking methods. No instrument is contacted. The audit includes
the two intermediate IO_UPDATE pulses; the final scheduled launch is separate.
It also audits same-profile, same-frequency phase updates and identical-register
retriggering, both of which leave RAM enabled. Equality means the actual tracked
POW register, not merely the user-supplied logical phase.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


class Cursor:
    def __init__(self):
        self.time_mu = 0
        self.output_calls = 0

    def take_time_mu(self, duration):
        self.time_mu += int(duration)

    def get_time_mu(self):
        return self.time_mu

    def set_time_mu(self, when):
        self.time_mu = int(when)

    def rtio_output(self, *args):
        self.output_calls += 1


class OfflineCore:
    """Run decorated driver bodies on the host; never compile or communicate."""

    def __init__(self, ref_period, ref_multiplier):
        self.ref_period = ref_period
        self.ref_multiplier = ref_multiplier
        self.coarse_ref_period = ref_period * ref_multiplier

    def seconds_to_mu(self, seconds):
        return round(seconds / self.ref_period)

    def mu_to_seconds(self, ticks):
        return ticks * self.ref_period

    def run(self, method, args, kwargs):
        return method.artiq_embedded.function(*args, **kwargs)


class OfflineDevices:
    def __init__(self, core):
        self.devices = {"core": core}

    def get(self, name):
        return self.devices[name]


def audit(proto_rev=9, ref_period=1e-9, ref_multiplier=8):
    """Measure driver cursor advancement without invoking hardware syscalls."""
    import artiq
    from artiq.coredevice import spi2, ttl
    from artiq.coredevice.ad9910 import AD9910, RAM_DEST_ASF
    from artiq.coredevice.urukul import CPLD
    from artiq.language import core as language

    if not artiq.__version__.startswith("9."):
        raise ValueError("This audit targets the installed ARTIQ 9 driver API")
    if proto_rev not in (8, 9):
        raise ValueError("Urukul protocol revision must be 8 or 9")
    if not math.isfinite(ref_period) or ref_period <= 0 or ref_multiplier < 1:
        raise ValueError("Invalid reference clock")
    cursor = Cursor()
    manager = OfflineDevices(OfflineCore(ref_period, ref_multiplier))
    previous_time_manager = language._time_manager
    previous_spi_output, previous_ttl_output = spi2.rtio_output, ttl.rtio_output
    try:
        language.set_time_manager(cursor)
        spi2.rtio_output = ttl.rtio_output = cursor.rtio_output
        manager.devices["spi"] = spi2.SPIMaster(manager, channel=0)
        manager.devices["io_update"] = ttl.TTLOut(manager, channel=1)
        manager.devices["switch"] = ttl.TTLOut(manager, channel=2)
        manager.devices["cpld"] = CPLD(
            manager,
            spi_device="spi",
            io_update_device="io_update",
            proto_rev=proto_rev,
        )
        # In compiled kernels these nested calls bypass decorator dispatch.
        # Host dispatch needs an explicit reference on the protocol helper.
        manager.devices["cpld"].version.core = manager.get("core")
        dds = AD9910(
            manager,
            chip_select=4,
            cpld_device="cpld",
            sw_device="switch",
            sync_delay_seed=-1,
            io_update_delay=0,
        )
        # Matched latency is configured during preload, outside the gap budget.
        dds.set_cfr2(asf_profile_enable=0, matched_latency_enable=1)
        cursor.time_mu = cursor.output_calls = 0
        actions = [
            ("disable_ram", lambda: dds.set_cfr1(ram_enable=0)),
            ("commit_disable", lambda: dds.io_update.pulse_mu(8)),
            ("select_profile", lambda: dds.cpld.set_profile(0, 1)),
            ("set_ftw", lambda: dds.set_ftw(343597384)),
            ("set_pow", lambda: dds.set_pow(12345)),
            ("commit_parameters", lambda: dds.io_update.pulse_mu(8)),
            (
                "stage_ram_enable",
                lambda: dds.set_cfr1(
                    ram_enable=1,
                    ram_destination=RAM_DEST_ASF,
                    phase_autoclear=1,
                ),
            ),
        ]
        steps = []
        for name, action in actions:
            start = cursor.time_mu
            action()
            steps.append({"operation": name, "duration_mu": cursor.time_mu - start})
        measured = cursor.time_mu
        formula = (428 if proto_rev == 9 else 368) * ref_multiplier + 16
        conservative_output_calls = cursor.output_calls
        cursor.time_mu = cursor.output_calls = 0
        dds.set_pow(23456)
        phase_duration = cursor.time_mu
        phase_output_calls = cursor.output_calls
        phase_formula = 52 * ref_multiplier
        cursor.time_mu = cursor.output_calls = 0
        dds.io_update.pulse_mu(8)
        launch_duration = cursor.time_mu
        launch_output_calls = cursor.output_calls
        fast_paths = {
            "phase_only": {
                "operations": ["set_pow"],
                "preparation_duration_mu": phase_duration,
                "expected_formula_mu": phase_formula,
                "matches_expected_formula": phase_duration == phase_formula,
                "recorded_rtio_output_calls": phase_output_calls,
                "ram_stays_enabled": True,
            },
            "identical_register_retrigger": {
                "operations": [],
                "preparation_duration_mu": 0,
                "expected_formula_mu": 0,
                "matches_expected_formula": True,
                "recorded_rtio_output_calls": 0,
                "ram_stays_enabled": True,
                "condition": "Same resident profile, FTW and actual tracked POW",
            },
            "scheduled_launch": {
                "operations": ["io_update.pulse_mu(8)"],
                "duration_mu": launch_duration,
                "expected_formula_mu": 8,
                "matches_expected_formula": launch_duration == 8,
                "recorded_rtio_output_calls": launch_output_calls,
            },
        }
        return {
            "validation": "real_driver_python_cursor_model",
            "artiq_version": artiq.__version__,
            "python": sys.version,
            "proto_rev": proto_rev,
            "ref_period_s": ref_period,
            "ref_multiplier": ref_multiplier,
            "steps": steps,
            "rearm_duration_mu": measured,
            "rearm_duration_s": measured * ref_period,
            "expected_formula_mu": formula,
            "matches_expected_formula": measured == formula,
            "recorded_rtio_output_calls": conservative_output_calls,
            "fast_paths": fast_paths,
            "all_paths_match_expected_formula": (
                measured == formula
                and all(p["matches_expected_formula"] for p in fast_paths.values())
            ),
            "final_launch_pulse_mu": 8,
            "matched_latency_enable": 1,
            "kernel_compiled": False,
            "kernel_executed": False,
            "hardware_verified": False,
            "limitations": (
                "Cursor advancement only: excludes CPU submission cost, RTIO "
                "underflow, physical SPI/DDS propagation and optical timing. "
                "Output gap must also include the separately declared RAM latency."
            ),
        }
    finally:
        spi2.rtio_output, ttl.rtio_output = previous_spi_output, previous_ttl_output
        language.set_time_manager(previous_time_manager)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proto-rev", type=int, choices=(8, 9), default=9)
    parser.add_argument("--ref-period", type=float, default=1e-9)
    parser.add_argument("--ref-multiplier", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.proto_rev, args.ref_period, args.ref_multiplier)
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)
    return 0 if report["all_paths_match_expected_formula"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
