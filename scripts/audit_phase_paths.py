"""Audit installed AD9910/Urukul driver cursor costs without operating hardware.

Run with ``scripts/artiq-python.ps1 scripts/audit_phase_paths.py``. This executes
real driver Python bodies against the existing virtual RTIO clock. The output
contains driver hashes and digital submissions, not measured analog latencies.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

_COMMON_PATH = Path(__file__).with_name("verify_square_driver.py")
_spec = importlib.util.spec_from_file_location("_phase_driver_common", _COMMON_PATH)
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)


def _database(proto_rev):
    def local(module, cls, **arguments):
        return {
            "module": f"artiq.coredevice.{module}",
            "class": cls,
            "arguments": arguments,
        }

    return {
        "spi": local("spi2", "SPIMaster", channel=0),
        "update": local("ttl", "TTLOut", channel=1),
        "cpld": local(
            "urukul",
            "CPLD",
            spi_device="spi",
            io_update_device="update",
            refclk=125e6,
            clk_div=0,
            proto_rev=proto_rev,
        ),
        "dds": local(
            "ad9910",
            "AD9910",
            chip_select=4,
            cpld_device="cpld",
            pll_en=1,
            sync_delay_seed=-1,
            io_update_delay=0,
        ),
    }


def audit_phase_paths(*, coarse_mu=8):
    import artiq
    from artiq.coredevice import core as core_driver
    from artiq.coredevice import spi2, ttl, urukul
    from artiq.coredevice.ad9910 import (
        PHASE_MODE_ABSOLUTE,
        PHASE_MODE_CONTINUOUS,
        PHASE_MODE_TRACKING,
    )
    from artiq.language import core as language

    if type(coarse_mu) is not int or coarse_mu <= 0:
        raise ValueError("coarse_mu must be a positive integer")
    clock = common.VirtualClock(1e-9)
    core = common.VirtualCore(clock, coarse_mu)
    events = []

    def emit(target, value):
        events.append(
            dict(timestamp_mu=clock.cursor, target=int(target), value=int(value))
        )

    def forbid_input(*args, **kwargs):
        raise RuntimeError("Hardware input is forbidden in this offline audit")

    report = {
        "validation": "real_driver_python_trace",
        "status": "failed",
        "artiq_version": artiq.__version__,
        "tick_seconds": 1e-9,
        "coarse_mu": coarse_mu,
        "hardware_executed": False,
        "kernel_executed": False,
        "driver_sha256": {
            name: hashlib.sha256(
                Path(importlib.import_module(name).__file__).read_bytes()
            ).hexdigest()
            for name in (
                "artiq.coredevice.ad9910",
                "artiq.coredevice.spi2",
                "artiq.coredevice.ttl",
                "artiq.coredevice.urukul",
            )
        },
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "virtual_clock_helper_sha256": hashlib.sha256(
            _COMMON_PATH.read_bytes()
        ).hexdigest(),
        "spi_dds_write_divider": urukul.SPIT_DDS_WR,
        "spi_cfg_write_divider": urukul.SPIT_CFG_WR,
        "limitations": [
            "Cursor duration is not analog output latency or CPU execution time",
            "No hardware input, initialization, gateware or analog validation",
            "Profile pin latch timing is not measured by this SPI cursor audit",
            "Batched profile configuration is a proposed call pattern, not DSL support",
        ],
        "operations": [],
    }
    saved = (
        language._time_manager,
        spi2.rtio_output,
        ttl.rtio_output,
        spi2.rtio_input_data,
    )
    try:
        language.set_time_manager(clock)
        spi2.rtio_output = ttl.rtio_output = emit
        spi2.rtio_input_data = forbid_input
        for revision in (8, 9):
            manager = common.LocalDriverManager(_database(revision), core)
            dds = manager.get("dds")

            def sequential_profiles():
                for channel in range(4):
                    dds.cpld.set_profile(channel, channel + 1)

            def batched_profiles():
                # Rev9 exposes four 3-bit profile fields in one CFG register.
                mask = ((1 << 12) - 1) << urukul.CFG_PROFILE
                profiles = sum((channel + 1) << (3 * channel) for channel in range(4))
                cfg = (int(dds.cpld.cfg_reg) & ~mask) | (profiles << urukul.CFG_PROFILE)
                dds.cpld.cfg_write(cfg)

            operations = [
                ("io_update_on", lambda: dds.io_update.on(), 0),
                ("io_update_pulse_8", lambda: dds.io_update.pulse_mu(8), 8 / coarse_mu),
                (
                    "set_phase_mode",
                    lambda: dds.set_phase_mode(PHASE_MODE_CONTINUOUS),
                    0,
                ),
                ("set_pow", lambda: dds.set_pow(0x4000), 52),
                ("set_ftw", lambda: dds.set_ftw(123456), 88),
                ("set_asf", lambda: dds.set_asf(8192), 88),
                ("set_cfr1", lambda: dds.set_cfr1(phase_clear=1), 88),
                ("set_cfr2", lambda: dds.set_cfr2(matched_latency_enable=1), 88),
                ("write_ram_51", lambda: dds.write_ram([0] * 51), 67 * 51 + 22),
                ("write_ram_201", lambda: dds.write_ram([0] * 201), 67 * 201 + 22),
                (
                    "set_mu_continuous",
                    lambda: dds.set_mu(123456, phase_mode=PHASE_MODE_CONTINUOUS),
                    156 + 8 / coarse_mu,
                ),
                (
                    "set_mu_absolute",
                    lambda: dds.set_mu(123456, phase_mode=PHASE_MODE_ABSOLUTE),
                    332 + 8 / coarse_mu,
                ),
                (
                    "set_mu_tracking",
                    lambda: dds.set_mu(123456, phase_mode=PHASE_MODE_TRACKING),
                    332 + 8 / coarse_mu,
                ),
                (
                    "set_profile",
                    lambda: dds.cpld.set_profile(0, 1),
                    52 if revision == 8 else 112,
                ),
                ("write64_profile", lambda: dds.write64(0x15, 0, 0), 156),
                (
                    "set_profile_ram",
                    lambda: dds.set_profile_ram(0, 200, step=1, profile=7),
                    156,
                ),
            ]
            if revision == 9:
                operations.extend(
                    [
                        (
                            "set_profile_four_channels_sequential",
                            sequential_profiles,
                            448,
                        ),
                        ("cfg_write_four_profiles_batch", batched_profiles, 112),
                    ]
                )
            for operation, call, expected_cycles in operations:
                clock.cursor = 0
                events.clear()
                call()
                if clock.cursor != expected_cycles * coarse_mu:
                    raise ValueError(f"Driver cursor changed for {operation}")
                report["operations"].append(
                    {
                        "proto_rev": revision,
                        "operation": operation,
                        "duration_mu": clock.cursor,
                        "duration_coarse_cycles": expected_cycles,
                        "last_rtio_submission_mu": max(
                            (event["timestamp_mu"] for event in events), default=None
                        ),
                        "io_update_edges": [
                            event
                            for event in events
                            if event["target"] == dds.io_update.target_o
                        ],
                        "rtio_events": list(events),
                    }
                )
        saved_core = core_driver.rtio_get_counter, core_driver.rtio_init
        resets = []
        try:
            core_driver.rtio_get_counter = lambda: 9000
            core_driver.rtio_init = lambda: resets.append(True)
            clock.origin = 0
            clock.certified_times = {134000}
            controls = []
            for operation, start in (
                ("break_realtime", 2000),
                ("break_realtime", 200000),
                ("reset", 200000),
            ):
                clock.cursor = start
                getattr(core_driver.Core, operation).artiq_embedded.function(object())
                controls.append(
                    dict(
                        operation=operation,
                        hardware_counter_mu=9000,
                        cursor_before_mu=start,
                        cursor_after_mu=clock.cursor,
                    )
                )
            if [r["cursor_after_mu"] for r in controls] != [
                134000,
                200000,
                134000,
            ] or resets != [True]:
                raise ValueError("Core cursor-control semantics changed")
            report["core_cursor_controls"] = controls
        finally:
            core_driver.rtio_get_counter, core_driver.rtio_init = saved_core
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        language.set_time_manager(saved[0])
        spi2.rtio_output, ttl.rtio_output, spi2.rtio_input_data = saved[1:]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/short_gate_timing/driver_phase_paths.json"),
    )
    parser.add_argument("--coarse-mu", type=int, default=8)
    args = parser.parse_args()
    report = audit_phase_paths(coarse_mu=args.coarse_mu)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{report['status']}: {args.output.resolve()}")
    raise SystemExit(report["status"] != "passed")


if __name__ == "__main__":
    main()
