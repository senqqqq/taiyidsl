"""Audit POWASF exports with real ARTIQ driver bodies and virtual RTIO only.

Run with ``scripts/artiq-python.ps1 scripts/verify_polar_driver.py EXPORT``.
No real Core, communication driver, kernel execution or input is permitted.
Profile-call end is the declared timing anchor, not a measured pin transition.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_polar_driver_common", Path(__file__).with_name("verify_square_driver.py")
)
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)


def compare_trace(point, writes, edges, records, selections, banks, targets, origin):
    """Check actual uploads, selector calls, commits and NCO register state."""
    events = point["physical_events"]
    expected = [e for e in events if e["kind"] == "dds_polar_select"]
    timed = [s for s in selections if s["start_mu"] >= origin]
    if len(expected) != len(timed):
        raise ValueError("POWASF real selector-call count differs from plan")
    for event, selection in zip(expected, timed):
        wanted = (
            event["signal"],
            origin + event["start_mu"],
            origin + event["start_mu"] + event["duration_mu"],
            event["params"]["polar_profile"],
        )
        actual = tuple(
            selection[k] for k in ("device", "start_mu", "end_mu", "profile")
        )
        if wanted != actual:
            raise ValueError(
                f"POWASF real profile selector differs: {actual} != {wanted}"
            )
    parameters = {}
    for event in events:
        if "polar_bank" in event["params"]:
            parameters.setdefault(event["signal"], event["params"])
    for name, params in parameters.items():
        for entry in params["polar_bank"]:
            bank = banks[(name, entry["profile"])]
            if bank["words"] != entry["words"]:
                raise ValueError(
                    "Actual POWASF SPI RAM payload differs from packed bank"
                )
            hi, lo = bank["profile_words"]
            profile = (
                (lo >> 14) & 1023,
                ((hi & 255) << 2) | ((lo >> 30) & 3),
                (hi >> 8) & 65535,
                lo & 7,
            )
            if profile != tuple(entry[k] for k in ("start", "end", "step", "mode")):
                raise ValueError("Real POWASF RAM descriptor differs from bank")
            if bank["end_mu"] > origin:
                raise ValueError("POWASF RAM was uploaded during the timed shot")
        release = origin + params["nco_epoch_release_mu"]
        states = [r for r in records if r["device"] == name]
        before = [r for r in states if r["timestamp_mu"] < release]
        epoch = [
            r
            for r in states
            if r["timestamp_mu"] == release and r["kind"] == "io_update"
        ]
        if not before or len(epoch) != 1:
            raise ValueError("POWASF needs one real continuous NCO release")
        held = before[-1]["state"].get("0", 0)
        if not held & (1 << 11) or held & (1 << 13):
            raise ValueError("POWASF NCO was not held clear before release")
        for record in states:
            if record["timestamp_mu"] < release:
                continue
            state = record["state"]
            cfr1, cfr2 = state.get("0", 0), state.get("1", 0)
            if (
                not cfr1 & (1 << 31)
                or (cfr1 >> 29) & 3 != 3
                or cfr1 & ((1 << 11) | (1 << 13) | (7 << 17))
                or cfr2 & (1 << 24)
                or not cfr2 & (1 << 7)
            ):
                raise ValueError(
                    "POWASF real commit changed RAM/NCO mode or cleared phase"
                )
            if state.get("7", 0) & 0xFFFFFFFF != params["ftw"] & 0xFFFFFFFF:
                raise ValueError("POWASF real FTW is not continuous and fixed")
        initial_profile = epoch[0]["profile"]
        initial_words = banks[(name, initial_profile)]["words"]
        if (
            initial_profile != params["polar_initial_profile"]
            or initial_words[0] >> 16 != params["nco_initial_pow"]
            or (initial_words[0] >> 2) & 0x3FFF
        ):
            raise ValueError("POWASF initial RAM phase hold differs from NCO reference")
    if any(w["start_mu"] >= origin for w in writes):
        raise ValueError(
            "POWASF performs an unplanned DDS register write during the shot"
        )

    expected_edges = []
    for event in events:
        kind, name = event["kind"], event["signal"]
        when = origin + event["start_mu"]
        if kind in {"dds_polar_retrigger", "dds_nco_epoch"}:
            expected_edges.append((when, targets[name]["update"], 1))
        elif kind in {"dds_polar_commit_off", "dds_nco_epoch_off"}:
            expected_edges.append((when, targets[name]["update"], 0))
        elif kind in {"dds_on", "dds_off", "ttl_on", "ttl_off"}:
            key = "switch" if kind.startswith("dds") else "output"
            expected_edges.append((when, targets[name][key], int(kind.endswith("_on"))))
        elif kind not in {"dds_polar_select", "dds_polar_effect"}:
            raise ValueError(f"Unexpected event in POWASF driver audit: {kind}")
    digital = {target for values in targets.values() for target in values.values()}
    actual_edges = [
        (e["timestamp_mu"], e["target"], e["value"])
        for e in edges
        if e["timed"] and e["timestamp_mu"] >= origin and e["target"] in digital
    ]
    if actual_edges != expected_edges:
        raise ValueError("Real POWASF RF/TTL/IO_UPDATE edges differ from compiled plan")
    return {
        "point": point["index"],
        "origin_mu": origin,
        "selector_calls": len(timed),
        "selector_durations_mu": sorted({s["end_mu"] - s["start_mu"] for s in timed}),
        "resident_profiles": len(banks),
        "resident_words": sum(len(bank["words"]) for bank in banks.values()),
        "continuous_epochs": len(parameters),
        "digital_edges": len(actual_edges),
    }


def verify_driver(export, *, coarse_mu=8):
    import artiq
    from artiq.coredevice import spi2, ttl, urukul
    from artiq.language import core as language

    export = Path(export).resolve()
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    plan = json.loads((export / "compiled.json").read_text(encoding="utf-8"))
    source = (export / "experiment.py").read_text(encoding="utf-8")
    if hashlib.sha256(source.encode()).hexdigest() != manifest["source_sha256"]:
        raise ValueError("Export source hash mismatch")
    compiler = common.load_module(
        Path(__file__).resolve().parents[1] / "examples/artiq_offline_compile.py",
        "polar_compile_database",
    )
    database = compiler.compile_device_db(manifest, "rv32g")
    clock = common.VirtualClock(manifest["tick_seconds"])
    manager = common.LocalDriverManager(database, common.VirtualCore(clock, coarse_mu))
    edges, writes, records, selections = [], [], [], []
    targets, staged, banks, selected, profile_words = {}, {}, {}, {}, {}
    patched_cplds = set()
    spi_audit = None

    def record(name, kind):
        records.append(
            {
                "timestamp_mu": clock.cursor,
                "device": name,
                "kind": kind,
                "state": dict(staged[name]),
                "profile": selected.get(name, 7),
            }
        )

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
                    record(name, "io_update")

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
                        "values": [int(v) for v in values],
                        "start_mu": start,
                        "end_mu": clock.cursor,
                    }
                )
                if len(values) == 1:
                    staged[name][str(int(address))] = int(values[0])
                else:
                    profile_words[(name, int(address) - 0x0E)] = tuple(map(int, values))
                return result

            setattr(device, method, capture)
        original_ram = device.write_ram

        def write_ram(words):
            start, edge_start = clock.cursor, len(edges)
            result = original_ram(words)
            payload = [
                edge["value"] & 0xFFFFFFFF
                for edge in edges[edge_start:]
                if edge["target"] == device.bus.channel << 8
            ]
            if payload != [0x16 << 24] + [int(word) & 0xFFFFFFFF for word in words]:
                raise ValueError(
                    "Actual RAM SPI data order differs from requested words"
                )
            if clock.cursor - start != (67 * len(words) + 22) * coarse_mu:
                raise ValueError("Real write_ram cursor cost differs from audited rule")
            banks[(name, selected[name])] = {
                "words": payload[1:],
                "profile_words": profile_words[(name, selected[name])],
                "start_mu": start,
                "end_mu": clock.cursor,
            }
            return result

        device.write_ram = write_ram
        cpld = device.cpld
        if id(cpld) not in patched_cplds:
            original_profile = cpld.set_profile

            def set_profile(channel, profile):
                start = clock.cursor
                result = original_profile(channel, profile)
                for member, description in manifest["devices"].items():
                    if description["kind"] != "dds":
                        continue
                    dds = manager.get(member)
                    if dds.cpld is not cpld:
                        continue
                    if cpld.proto_rev == 9 and dds.chip_select - 4 != int(channel):
                        continue
                    shift = urukul.CFG_PROFILE
                    if cpld.proto_rev == 9:
                        shift += int(channel) * 3
                    value = (int(cpld.cfg_reg) >> shift) & 7
                    if value != int(profile):
                        raise ValueError(
                            "Real CPLD register selects a different profile"
                        )
                    previous = selected.get(member, 7)
                    selected[member] = value
                    selections.append(
                        {
                            "device": member,
                            "start_mu": start,
                            "end_mu": clock.cursor,
                            "profile": value,
                        }
                    )
                    if previous != value:
                        record(member, "profile_select")
                return result

            cpld.set_profile = set_profile
            patched_cplds.add(id(cpld))

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
        "validation": "real_powasf_driver_python_trace",
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
        "limitations": [
            "Profile-call end is the declared anchor, not a measured profile-pin edge",
            "Phase/envelope delays are declared; no analog output is observed",
            "No CPU throughput, slack, physical gateware or clock synchronization test",
            "Initialization kernel is compiled separately; audit executes RF-off "
            "preload and point driver bodies only, with all hardware input forbidden",
        ],
        "points": [],
    }
    try:
        language.set_time_manager(clock)
        spi2.rtio_output = ttl.rtio_output = emit
        spi2.rtio_input_data = forbid_input
        module = common.load_module(export / "experiment.py", "polar_driver_export")
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
            for values in (edges, writes, records, selections, banks):
                values.clear()
            clock.certify(point)
            instance._execute_point(point["index"])
            result = compare_trace(
                point, writes, edges, records, selections, banks, targets, clock.origin
            )
            result["maximum_events_per_coarse_cycle"] = common.validate_rtio_trace(
                edges, coarse_mu, plan["setup"].get("rtio_lane_count", 8)
            )
            result["register_transactions"] = list(writes)
            result["selector_transactions"] = list(selections)
            result["commit_register_states"] = list(records)
            result["rtio_events"] = list(edges)
            result["uploaded_banks"] = [
                {"device": name, "profile": profile, **bank}
                for (name, profile), bank in sorted(banks.items())
            ]
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
    args = parser.parse_args()
    report = verify_driver(args.export, coarse_mu=args.coarse_mu)
    path = args.output or args.export / "real_polar_driver_trace.json"
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
