"""Audit resident RAM and mixed timed Square with real driver bodies, offline.

Run with ``scripts/artiq-python.ps1 scripts/verify_ram_driver.py EXPORT``.
The virtual clock checks SPI cursor costs, buffered register contents, resident
samples and digital edges. Physical phase/envelope latencies remain calibrated
device contracts; this script does not measure analog output or CPU throughput.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_ram_driver_common", Path(__file__).with_name("verify_square_driver.py")
)
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)


def compare_trace(
    point, writes, edges, commits, targets, banks, *, coarse_mu=8, origin=None
):
    """Compare real transactions with a resident same-profile replay plan."""
    events = point["physical_events"]
    launches = [e for e in events if e["kind"] == "dds_ram_start"]
    if not launches:
        raise ValueError("RAM audit requires a RAM launch")
    if any(e["kind"] in {"dds_config", "dds_ram_rearm"} for e in events):
        raise ValueError("RAM audit currently supports resident same-profile replay")
    enabled = [c for c in commits if c["state"].get("0", 0) & (1 << 31)]
    if not enabled:
        raise ValueError("RAM driver never committed an enabled RAM state")
    if origin is None:
        origin = enabled[0]["timestamp_mu"] - launches[0]["start_mu"]
    epoch_times = {
        (origin + e["start_mu"], member)
        for e in events
        if e["kind"] == "dds_nco_epoch"
        for member in e["params"]["commit_members"]
    }
    enabled = [
        c
        for c in enabled
        if c["timestamp_mu"] >= origin
        and (c["timestamp_mu"], c["device"]) not in epoch_times
    ]
    if len(enabled) != len(launches):
        raise ValueError("RAM commit count differs from the compiled plan")
    for event, commit in zip(launches, enabled):
        params, state = event["params"], commit["state"]
        expected = (
            event["signal"],
            origin + event["start_mu"],
            params["ftw"] & 0xFFFFFFFF,
            params["ram_pow"],
            params["ram_profile"],
        )
        actual = (
            commit["device"],
            commit["timestamp_mu"],
            state.get("7", 0) & 0xFFFFFFFF,
            state.get("8", 0),
            commit["profile"],
        )
        if actual != expected:
            raise ValueError("RAM committed register state differs from plan")
        cfr1, cfr2 = state.get("0", 0), state.get("1", 0)
        continuous = params.get("nco_mode") == "continuous"
        if (
            (cfr1 >> 29) & 3 != 2
            or bool(cfr1 & (1 << 13)) == continuous
            or (continuous and cfr1 & (1 << 11))
        ):
            raise ValueError("RAM commit lacks ASF destination and phase autoclear")
        if params.get("ram_matched_latency") and not cfr2 & (1 << 7):
            raise ValueError("RAM commit lacks the declared matched-latency setting")
        if cfr2 & (1 << 24):
            raise ValueError("RAM commit unexpectedly enables profile amplitude")
        bank = banks[(event["signal"], params["ram_profile"])]
        if bank["words"] != [asf << 18 for asf in params["ram_asf"]]:
            raise ValueError("Actual uploaded RAM samples differ from plan")
        hi, lo = bank["profile_words"]
        actual_profile = (
            (lo >> 14) & 1023,
            ((hi & 255) << 2) | ((lo >> 30) & 3),
            (hi >> 8) & 65535,
            lo & 7,
        )
        desired_profile = (
            params["ram_start"],
            params["ram_end"],
            params["ram_step"],
            1,
        )
        if actual_profile != desired_profile:
            raise ValueError("Actual RAM profile differs from compiled bank")
    expected_writes = [e for e in events if e["kind"] == "dds_ram_phase"]
    actual_writes = [w for w in writes if w["address"] == 8 and w["start_mu"] >= origin]
    if len(actual_writes) != len(expected_writes):
        raise ValueError("RAM phase preparation count differs from plan")
    for event, write in zip(expected_writes, actual_writes):
        if (
            event["signal"],
            origin + event["start_mu"],
            event["params"]["ram_pow"],
        ) != (write["device"], write["start_mu"], write["value"]):
            raise ValueError("RAM phase transaction differs from plan")
        if write["end_mu"] > origin + event["start_mu"] + event["duration_mu"]:
            raise ValueError("Actual RAM phase write exceeds declared budget")
    expected_edges = []
    for event in events:
        kind = event["kind"]
        timestamp = origin + event["start_mu"]
        if kind in {"dds_ram_start", "dds_nco_epoch"}:
            target = targets[event["signal"]]["update"]
            expected_edges.append((timestamp, target, 1))
            if event["duration_mu"]:
                expected_edges.append((timestamp + 8, target, 0))
        elif kind in {"dds_ram_commit_off", "dds_nco_epoch_off"}:
            expected_edges.append((timestamp, targets[event["signal"]]["update"], 0))
        elif kind in {"dds_on", "dds_off", "ttl_on", "ttl_off"}:
            key = "switch" if kind.startswith("dds") else "output"
            expected_edges.append(
                (timestamp, targets[event["signal"]][key], int(kind.endswith("_on")))
            )
        elif kind not in {"dds_ram_phase", "dds_ram_effect"}:
            raise ValueError(f"RAM audit does not support {kind}")
    digital_targets = {t for item in targets.values() for t in item.values()}
    actual_edges = [
        (e["timestamp_mu"], e["target"], e["value"])
        for e in edges
        if e["timestamp_mu"] >= origin
        and e["target"] in digital_targets
        and e.get("timed", True)
    ]
    if actual_edges != expected_edges:
        raise ValueError("Real RAM TTL/IO_UPDATE edges differ from compiled plan")
    previous_falls = {}
    for timestamp, target, value in actual_edges:
        if target not in {
            item["update"] for item in targets.values() if "update" in item
        }:
            continue
        if value:
            if (
                target in previous_falls
                and timestamp < previous_falls[target] + coarse_mu
            ):
                raise ValueError("RAM IO_UPDATE rise lacks a full coarse-cycle low")
        else:
            previous_falls[target] = timestamp
    return {
        "point": point["index"],
        "origin_mu": origin,
        "launches": len(launches),
        "phase_preparations": len(actual_writes),
        "maximum_phase_duration_mu": max(
            (w["end_mu"] - w["start_mu"] for w in actual_writes), default=0
        ),
        "resident_profiles": len(banks),
        "digital_edges": len(actual_edges),
    }


def verify_driver(export, *, coarse_mu=8, proto_rev=9):
    import artiq
    from artiq.coredevice import spi2, ttl
    from artiq.language import core as language

    export = Path(export).resolve()
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    plan = json.loads((export / "compiled.json").read_text(encoding="utf-8"))
    source = (export / "experiment.py").read_text(encoding="utf-8")
    if hashlib.sha256(source.encode()).hexdigest() != manifest["source_sha256"]:
        raise ValueError("Export source hash mismatch")
    compiler = common.load_module(
        Path(__file__).resolve().parents[1] / "examples" / "artiq_offline_compile.py",
        "ram_compile_database",
    )
    database = compiler.compile_device_db(manifest, "rv32g")
    for entry in database.values():
        if entry["class"] == "CPLD":
            entry["arguments"]["proto_rev"] = proto_rev
    clock = common.VirtualClock(manifest["tick_seconds"])
    manager = common.LocalDriverManager(database, common.VirtualCore(clock, coarse_mu))
    edges, writes, commits, targets, staged, banks = [], [], [], {}, {}, {}
    selected, patched_cplds, profile_words = {}, set(), {}
    spi_audit = None

    def emit(target, value):
        if spi_audit is not None:
            spi_audit.observe(target, value, clock.cursor)
        edges.append(
            {
                "timestamp_mu": clock.cursor,
                "target": int(target),
                "value": int(value),
                "timed": clock.origin is not None and not clock.awaiting_origin,
            }
        )
        if value == 1:
            for name, channels in targets.items():
                if channels.get("update") == target:
                    device = manager.get(name)
                    commits.append(
                        {
                            "timestamp_mu": clock.cursor,
                            "device": name,
                            "state": dict(staged[name]),
                            "profile": selected.get(
                                (id(device.cpld), device.chip_select - 4), 7
                            ),
                        }
                    )

    def forbid_input(*args, **kwargs):
        raise RuntimeError("Hardware input is forbidden in this offline audit")

    def capture_driver(device, name):
        for method in ("write16", "write32", "write64"):
            original = getattr(device, method)

            def capture(address, *values, original=original):
                start = clock.cursor
                result = original(address, *values)
                writes.append(
                    {
                        "device": name,
                        "address": int(address),
                        "value": int(values[0]),
                        "values": [int(v) for v in values],
                        "start_mu": start,
                        "end_mu": clock.cursor,
                    }
                )
                if len(values) == 1:
                    staged[name][str(int(address))] = int(values[0])
                else:
                    profile_words[(name, int(address) - 0x0E)] = tuple(map(int, values))
                    staged[name][str(int(address))] = {
                        "hi": int(values[0]),
                        "lo": int(values[1]),
                    }
                    writes[-1].update(hi=int(values[0]), lo=int(values[1]))
                return result

            setattr(device, method, capture)
        original_ram = device.write_ram

        def write_ram(words):
            result = original_ram(words)
            profile = selected[(id(device.cpld), device.chip_select - 4)]
            banks[(name, profile)] = {
                "words": [int(w) & 0xFFFFFFFF for w in words],
                "profile_words": profile_words[(name, profile)],
            }
            return result

        device.write_ram = write_ram
        cpld_id = id(device.cpld)
        if cpld_id not in patched_cplds:
            original_profile = device.cpld.set_profile

            def set_profile(channel, profile):
                result = original_profile(channel, profile)
                selected[(cpld_id, int(channel))] = int(profile)
                return result

            device.cpld.set_profile = set_profile
            patched_cplds.add(cpld_id)

    for name, desc in manifest["devices"].items():
        device = manager.get(name)
        if desc["kind"] == "dds":
            staged[name] = {}
            capture_driver(device, name)
            targets[name] = {
                "update": device.io_update.target_o,
                "switch": device.sw.target_o,
            }
        else:
            targets[name] = {"output": device.target_o}
    saved = (
        language._time_manager,
        spi2.rtio_output,
        ttl.rtio_output,
        spi2.rtio_input_data,
    )
    report = {
        "validation": "real_ram_driver_python_trace",
        "status": "failed",
        "artiq_version": artiq.__version__,
        "source_sha256": manifest["source_sha256"],
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
        "hardware_executed": False,
        "kernel_executed": False,
        "coarse_mu": coarse_mu,
        "proto_rev": proto_rev,
        "limitations": [
            "Analog phase/envelope latencies are declared contracts, not measured here",
            "No CPU throughput, slack, actual gateware or clock coherence validation",
            "RAM replay is restricted to resident same-profile calls; "
            "mixed timed Square is also audited",
        ],
        "points": [],
    }
    try:
        language.set_time_manager(clock)
        spi2.rtio_output = ttl.rtio_output = emit
        spi2.rtio_input_data = forbid_input
        module = common.load_module(export / "experiment.py", "ram_driver_export")
        instance = object.__new__(module.GeneratedExperiment)
        instance.get_device = manager.get
        instance.build()
        instance.prepared_shared_resources = tuple(
            manifest["execution_resources"]["prepared_shared_resources_required"]
        )
        instance._validate_target()
        for point in plan["points"]:
            spi_audit = common.SPITimingAudit(
                (
                    manager.get(name).bus.channel
                    for name, desc in manifest["devices"].items()
                    if desc["kind"] == "dds"
                ),
                coarse_mu,
            )
            edges.clear()
            writes.clear()
            commits.clear()
            banks.clear()
            clock.certify(point)
            instance._execute_point(point["index"])
            square_names = {
                event["signal"]
                for event in point["physical_events"]
                if event["kind"] == "dds_prepare"
            }
            ram_point = {
                **point,
                "physical_events": [
                    event
                    for event in point["physical_events"]
                    if event["signal"] not in square_names
                ],
            }
            result = compare_trace(
                ram_point,
                writes,
                edges,
                commits,
                {
                    name: value
                    for name, value in targets.items()
                    if name not in square_names
                },
                banks,
                coarse_mu=coarse_mu,
                origin=clock.origin,
            )
            if square_names:
                square_point = {
                    **point,
                    "physical_events": [
                        event
                        for event in point["physical_events"]
                        if event["signal"] in square_names
                    ],
                }
                result["square"] = common.compare_trace(
                    square_point,
                    [write for write in writes if write["device"] in square_names],
                    edges,
                    {
                        name: value
                        for name, value in targets.items()
                        if name in square_names
                    },
                    origin=clock.origin,
                )
                for commit in commits:
                    if (
                        commit["device"] not in square_names
                        or commit["timestamp_mu"] < clock.origin
                    ):
                        continue
                    cfr1, cfr2 = (
                        commit["state"].get("0", 0),
                        commit["state"].get("1", 0),
                    )
                    continuous = (
                        plan["setup"]["devices"][commit["device"]].get("nco_mode")
                        == "continuous"
                    )
                    if cfr1 & (1 << 31) or bool(cfr1 & (1 << 13)) == continuous:
                        raise ValueError("Mixed Square commit has an invalid DDS mode")
                    if not cfr2 & (1 << 7) or not cfr2 & (1 << 24):
                        raise ValueError(
                            "Mixed Square commit lacks matched profile amplitude"
                        )
            result["register_transactions"] = list(writes)
            result["continuous_epochs"] = common.validate_nco_epochs(
                point, commits, result["origin_mu"]
            )
            result["commit_register_states"] = list(commits)
            result["rtio_events"] = list(edges)
            result["maximum_events_per_coarse_cycle"] = common.validate_rtio_trace(
                edges, coarse_mu, plan["setup"].get("rtio_lane_count", 8)
            )
            report["points"].append(result)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        language.set_time_manager(saved[0])
        spi2.rtio_output, ttl.rtio_output, spi2.rtio_input_data = saved[1:]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--coarse-mu", type=int, default=8)
    parser.add_argument("--proto-rev", type=int, choices=(8, 9), default=9)
    args = parser.parse_args()
    report = verify_driver(
        args.export, coarse_mu=args.coarse_mu, proto_rev=args.proto_rev
    )
    path = args.output or args.export / "real_ram_driver_trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(path),
                "error": report.get("error"),
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
