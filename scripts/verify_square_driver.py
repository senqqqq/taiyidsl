"""Audit an export with installed ARTIQ driver bodies and a virtual RTIO clock.

Run with ``scripts/artiq-python.ps1 scripts/verify_square_driver.py EXPORT``.
No Core device, communication driver, kernel execution, or instrument is opened.
This checks SPI cursor costs, register words and digital edge timestamps. It
does not establish CPU throughput, analog pipeline latency, or phase coherence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
from collections import Counter
from pathlib import Path


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compare_trace(point, writes, edges, targets, *, origin=None):
    """Compare actual driver transactions/edges with an exported timed plan."""
    expected = [e for e in point["physical_events"] if e["kind"] == "dds_prepare"]
    profile_writes = [
        w
        for w in writes
        if w["address"] == 0x15 and (origin is None or w["start_mu"] >= origin)
    ]
    if not expected:
        raise ValueError("Driver trace requires a timed Square preparation")
    if len(profile_writes) != len(expected):
        raise ValueError("Profile write count differs from timed preparations")
    if origin is None:
        origin = profile_writes[0]["start_mu"] - expected[0]["start_mu"]
    for event, write in zip(expected, profile_writes):
        params = event["params"]
        desired = (
            event["signal"],
            event["start_mu"] + origin,
            int(params["ftw"]) & 0xFFFFFFFF,
            int(params["square_pow"]) & 0xFFFF,
            int(params["asf"]),
        )
        observed = (
            write["device"],
            write["start_mu"],
            write["lo"] & 0xFFFFFFFF,
            write["hi"] & 0xFFFF,
            (write["hi"] >> 16) & 0x3FFF,
        )
        if observed != desired:
            raise ValueError(f"Profile transaction differs: {observed} != {desired}")
        if write["end_mu"] > origin + event["start_mu"] + event["duration_mu"]:
            raise ValueError("Actual profile write exceeds declared preparation budget")
    expected_edges = []
    for event in point["physical_events"]:
        kind = event["kind"]
        if kind in {
            "dds_commit",
            "dds_commit_off",
            "dds_nco_epoch",
            "dds_nco_epoch_off",
        }:
            target = targets[event["signal"]]["update"]
            level = int(kind in {"dds_commit", "dds_nco_epoch"})
        elif kind in {"dds_on", "dds_off"}:
            target = targets[event["signal"]]["switch"]
            level = int(kind == "dds_on")
        elif kind in {"ttl_on", "ttl_off"}:
            target = targets[event["signal"]]["output"]
            level = int(kind == "ttl_on")
        elif kind in {"dds_prepare", "dds_effect"}:
            continue
        else:
            raise ValueError(f"Square trace does not support {kind}")
        expected_edges.append((origin + event["start_mu"], target, level))
    digital_targets = {t for item in targets.values() for t in item.values()}
    actual_edges = [
        (e["timestamp_mu"], e["target"], e["value"])
        for e in edges
        if e["timestamp_mu"] >= origin
        and e["target"] in digital_targets
        and e.get("timed", True)
    ]
    if actual_edges != expected_edges:
        raise ValueError("Real TTL/IO_UPDATE edge trace differs from compiled plan")
    return {
        "point": point["index"],
        "origin_mu": origin,
        "profile_writes": len(profile_writes),
        "digital_edges": len(actual_edges),
        "maximum_profile_duration_mu": max(
            w["end_mu"] - w["start_mu"] for w in profile_writes
        ),
    }


def compare_legacy_trace(point, calls, edges, targets):
    """Check actual set_mu call spans without treating SPI cost as a budget."""
    expected = [e for e in point["physical_events"] if e["kind"] == "dds_config"]
    if not expected or len(expected) != len(calls):
        raise ValueError("Legacy DDS configuration count differs")
    origin = calls[0]["start_mu"] - expected[0]["start_mu"]
    for event, call in zip(expected, calls):
        params = event["params"]
        wanted = (
            event["signal"],
            origin + event["start_mu"],
            params["ftw"] & 0xFFFFFFFF,
            params["pow"],
            params["asf"],
        )
        observed = (
            call["device"],
            call["start_mu"],
            call["ftw"] & 0xFFFFFFFF,
            call["pow"],
            call["asf"],
        )
        if observed != wanted:
            raise ValueError("Legacy driver parameters differ from compiled plan")
        if call["end_mu"] - call["start_mu"] > event["duration_mu"]:
            raise ValueError("Actual set_mu exceeds declared configuration budget")
    output_targets = {
        target
        for item in targets.values()
        for kind, target in item.items()
        if kind != "update"
    }
    expected_edges = []
    for event in point["physical_events"]:
        kind = event["kind"]
        if kind == "dds_config":
            continue
        if kind not in {"dds_on", "dds_off", "ttl_on", "ttl_off"}:
            raise ValueError(f"Legacy Square trace does not support {kind}")
        target_kind = "switch" if kind.startswith("dds") else "output"
        expected_edges.append(
            (
                origin + event["start_mu"],
                targets[event["signal"]][target_kind],
                int(kind.endswith("_on")),
            )
        )
    actual_edges = [
        (e["timestamp_mu"], e["target"], e["value"])
        for e in edges
        if e["timestamp_mu"] >= origin
        and e["target"] in output_targets
        and e.get("timed", True)
    ]
    if actual_edges != expected_edges:
        raise ValueError("Legacy real output edges differ from compiled plan")
    return {
        "point": point["index"],
        "origin_mu": origin,
        "configuration_calls": len(calls),
        "digital_edges": len(actual_edges),
        "maximum_configuration_duration_mu": max(
            call["end_mu"] - call["start_mu"] for call in calls
        ),
    }


def validate_rtio_trace(edges, coarse_mu, lane_count):
    """Check actual submission order, not just same-cycle event counts."""
    counts = Counter(e["timestamp_mu"] // coarse_mu for e in edges)
    maximum = max(counts.values(), default=0)
    if maximum > lane_count:
        raise ValueError(
            f"Real driver trace needs {maximum} same-cycle events, "
            f"target declares {lane_count} RTIO lanes"
        )
    lanes, lane, channels = [-1] * lane_count, 0, {}
    for event in edges:
        timestamp = event["timestamp_mu"] // coarse_mu
        channel = event["target"] >> 8
        if timestamp <= channels.get(channel, -1):
            raise ValueError("Real RTIO channel events collide or go backwards")
        channels[channel] = timestamp
        if timestamp <= lanes[lane]:
            lane = (lane + 1) % lane_count
        if timestamp <= lanes[lane]:
            raise ValueError("Real driver submission order exceeds SED lane capacity")
        lanes[lane] = timestamp
    return maximum


class SPITimingAudit:
    """Observe actual SPI config/data RTIO words and enforce per-bus busy time."""

    def __init__(self, channels, coarse_mu):
        self.channels = set(channels)
        self.coarse_mu = coarse_mu
        self.busy, self.config = {}, {}

    def observe(self, target, value, timestamp):
        channel, address = int(target) >> 8, int(target) & 255
        if channel not in self.channels:
            return
        if timestamp < self.busy.get(channel, -1):
            raise ValueError("Real SPI command arrives while its bus is busy")
        if address == 1:
            self.config[channel] = (
                ((int(value) >> 8) & 31) + 1,
                ((int(value) >> 16) & 255) + 2,
            )
        elif address == 0:
            length, div = self.config[channel]
            self.busy[channel] = timestamp + ((length + 1) * div + 1) * self.coarse_mu


def validate_nco_epochs(point, records, origin):
    """Check real committed clear-hold/release and the initial hardware POW."""
    expected = {}
    for event in point["physical_events"]:
        p = event["params"]
        if p.get("nco_mode") == "continuous" and "ftw" in p:
            expected[event["signal"]] = p
    for name, p in expected.items():
        release = origin + p["nco_epoch_release_mu"]
        states = sorted(
            (r for r in records if r["device"] == name), key=lambda r: r["timestamp_mu"]
        )
        before = [r for r in states if r["timestamp_mu"] < release]
        current = [r for r in states if r["timestamp_mu"] == release]
        if not before or len(current) != 1:
            raise ValueError("Continuous NCO lacks one planned clear release")
        held, state = before[-1]["state"], current[0]["state"]
        if not held.get("0", 0) & (1 << 11) or held.get("0", 0) & (1 << 13):
            raise ValueError("NCO was not held clear before its epoch")
        if any(
            r["state"].get("0", 0) & ((1 << 11) | (1 << 13))
            for r in states
            if r["timestamp_mu"] >= release
        ):
            raise ValueError("NCO was cleared after its release")
        ram = bool(state.get("0", 0) & (1 << 31))
        if bool(held.get("0", 0) & (1 << 31)) != ram:
            raise ValueError("NCO release switches the DDS frequency source")
        if ram:
            ftw, pow_ = state.get("7", 0), state.get("8", 0)
        else:
            profile = state.get("21", {})
            ftw, pow_ = profile.get("lo", 0), profile.get("hi", 0) & 0xFFFF
        if (ftw & 0xFFFFFFFF, pow_) != (p["ftw"] & 0xFFFFFFFF, p["nco_initial_pow"]):
            raise ValueError("NCO initial FTW/POW differs from its declared reference")
    return len(expected)


class VirtualClock:
    def __init__(self, ref_period):
        self.cursor = 0
        self.ref_period = ref_period
        self.certified_times = set()
        self.origin = None
        self.awaiting_origin = False

    def certify(self, point):
        self.certified_times = {e["start_mu"] for e in point["physical_events"]}
        self.certified_times.add(
            max(
                [point["origin_mu"] + point["duration_mu"]]
                + [e["start_mu"] + e["duration_mu"] for e in point["physical_events"]]
            )
        )
        self.origin = None
        self.awaiting_origin = True

    def get_time_mu(self):
        return self.cursor

    def set_time_mu(self, timestamp):
        if self.awaiting_origin:
            self.origin = int(timestamp)
            self.awaiting_origin = False
        elif int(timestamp) < self.cursor - 7 and (
            self.origin is None
            or int(timestamp) - self.origin not in self.certified_times
        ):
            raise ValueError("Generated code rewinds outside its certified plan")
        self.cursor = int(timestamp)

    def take_time_mu(self, duration):
        self.cursor += int(duration)

    def take_time(self, duration):
        self.take_time_mu(round(duration / self.ref_period))


class VirtualCore:
    def __init__(self, clock, coarse_mu):
        self.clock = clock
        self.ref_period = clock.ref_period
        self.ref_multiplier = coarse_mu
        self.coarse_ref_period = coarse_mu * self.ref_period

    def seconds_to_mu(self, seconds):
        return round(seconds / self.ref_period)

    def mu_to_seconds(self, mu):
        return int(mu) * self.ref_period

    def run(self, function, args, kwargs):
        return function.artiq_embedded.function(*args, **kwargs)

    def reset(self):
        self.clock.cursor = 125_000

    def break_realtime(self):
        # Frozen virtual RTIO counter at zero; this does not measure CPU slack.
        self.clock.cursor = max(self.clock.cursor, 125_000)

    def wait_until_mu(self, timestamp):
        if timestamp < self.clock.cursor:
            raise ValueError("Wait precedes the driver cursor")


class LocalDriverManager:
    """Construct only local DDS/SPI/TTL drivers; never construct real Core."""

    def __init__(self, database, core):
        self.database = database
        self.instances = {"core": core}

    def get(self, name):
        if name not in self.instances:
            entry = self.database[name]
            if entry["module"] not in {
                "artiq.coredevice.ad9910",
                "artiq.coredevice.spi2",
                "artiq.coredevice.ttl",
                "artiq.coredevice.urukul",
            }:
                raise ValueError("Driver outside the offline audit allowlist")
            cls = getattr(importlib.import_module(entry["module"]), entry["class"])
            self.instances[name] = cls(self, **entry["arguments"])
            if entry["class"] == "CPLD":
                # Nested kernel calls are inlined on a core; host interpretation
                # needs the same virtual dispatcher on the protocol helper.
                self.instances[name].version.core = self.instances["core"]
        return self.instances[name]


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
    compiler = load_module(
        Path(__file__).resolve().parents[1] / "examples" / "artiq_offline_compile.py",
        "square_compile_database",
    )
    database = compiler.compile_device_db(manifest, "rv32g")
    for entry in database.values():
        if entry["class"] == "CPLD":
            entry["arguments"]["proto_rev"] = proto_rev
    clock = VirtualClock(manifest["tick_seconds"])
    core = VirtualCore(clock, coarse_mu)
    manager = LocalDriverManager(database, core)
    edges, writes, targets, staged, commit_states = [], [], {}, {}, []
    legacy_calls = []
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
            members = [
                name for name, item in targets.items() if item.get("update") == target
            ]
            if members:
                commit_states.append(
                    {
                        "timestamp_mu": clock.cursor,
                        "members": {name: dict(staged[name]) for name in members},
                    }
                )

    def forbid_input(*args, **kwargs):
        raise RuntimeError("Hardware input is forbidden in this offline audit")

    def capture_write(device, name):
        original = device.write64
        original32 = device.write32
        original_set = device.set_mu

        def set_mu(ftw=0, **kwargs):
            start = clock.cursor
            result = original_set(ftw, **kwargs)
            legacy_calls.append(
                {
                    "device": name,
                    "start_mu": start,
                    "end_mu": clock.cursor,
                    "ftw": int(ftw),
                    "pow": int(kwargs.get("pow_", 0)),
                    "asf": int(kwargs.get("asf", 0x3FFF)),
                }
            )
            return result

        def write32(address, value):
            result = original32(address, value)
            staged[name][str(int(address))] = int(value)
            return result

        def write64(address, hi, lo):
            start = clock.cursor
            result = original(address, hi, lo)
            writes.append(
                {
                    "device": name,
                    "address": int(address),
                    "hi": int(hi),
                    "lo": int(lo),
                    "start_mu": start,
                    "end_mu": clock.cursor,
                }
            )
            staged[name][str(int(address))] = {"hi": int(hi), "lo": int(lo)}
            return result

        device.write64 = write64
        device.write32 = write32
        device.set_mu = set_mu

    for name, desc in manifest["devices"].items():
        device = manager.get(name)
        if desc["kind"] == "dds":
            staged[name] = {}
            capture_write(device, name)
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
        "validation": "real_driver_python_trace",
        "status": "failed",
        "artiq_version": artiq.__version__,
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
        "source_sha256": manifest["source_sha256"],
        "coarse_mu": coarse_mu,
        "proto_rev": proto_rev,
        "hardware_executed": False,
        "kernel_executed": False,
        "limitations": [
            "No CPU throughput, slack, actual gateware or analog timing validation",
            "Device initialization requiring hardware input is not executed",
            "Per-shot generated preparation and real SPI/TTL bodies are executed",
        ],
        "points": [],
    }
    try:
        language.set_time_manager(clock)
        spi2.rtio_output = ttl.rtio_output = emit
        spi2.rtio_input_data = forbid_input
        module = load_module(export / "experiment.py", "square_driver_export")
        instance = object.__new__(module.GeneratedExperiment)
        instance.get_device = manager.get
        instance.build()
        instance.prepared_shared_resources = tuple(
            manifest["execution_resources"]["prepared_shared_resources_required"]
        )
        instance._validate_target()
        for point in plan["points"]:
            spi_audit = SPITimingAudit(
                (
                    manager.get(name).bus.channel
                    for name, desc in manifest["devices"].items()
                    if desc["kind"] == "dds"
                ),
                coarse_mu,
            )
            edges.clear()
            writes.clear()
            commit_states.clear()
            legacy_calls.clear()
            clock.certify(point)
            instance._execute_point(point["index"])
            timed = any(e["kind"] == "dds_prepare" for e in point["physical_events"])
            result = (
                compare_trace(point, writes, edges, targets, origin=clock.origin)
                if timed
                else compare_legacy_trace(point, legacy_calls, edges, targets)
            )
            for commit in commit_states if timed else ():
                if commit["timestamp_mu"] < result["origin_mu"]:
                    continue
                for name, state in commit["members"].items():
                    cfr1, cfr2 = state.get("0", 0), state.get("1", 0)
                    continuous = (
                        plan["setup"]["devices"][name].get("nco_mode") == "continuous"
                    )
                    if (
                        cfr1 & (1 << 31)
                        or bool(cfr1 & (1 << 13)) == continuous
                        or (continuous and cfr1 & (1 << 11))
                    ):
                        raise ValueError(
                            "Commit lacks single-tone phase-autoclear mode"
                        )
                    if not cfr2 & (1 << 7) or not cfr2 & (1 << 24):
                        raise ValueError(
                            "Commit lacks matched single-tone amplitude mode"
                        )
            result["register_transactions"] = list(writes)
            result["continuous_epochs"] = validate_nco_epochs(
                point,
                [
                    dict(timestamp_mu=c["timestamp_mu"], device=name, state=state)
                    for c in commit_states
                    for name, state in c["members"].items()
                ],
                result["origin_mu"],
            )
            result["legacy_configuration_calls"] = list(legacy_calls)
            result["rtio_events"] = list(edges)
            result["commit_register_states"] = list(commit_states)
            result["maximum_events_per_coarse_cycle"] = validate_rtio_trace(
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
    output = args.output or args.export / "real_driver_trace.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
