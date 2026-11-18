"""Compile a trusted TaiyiDSL export with real ARTIQ, without running kernels.

Run this file using the separate ARTIQ toolchain's Python, not the DSL venv.
The generated device database is synthetic and must never be used on hardware.

Usage (from the project root in an ARTIQ environment)::

    python examples/artiq_offline_compile.py output/offline_imaging/artiq \
        --target rv32g --output output/artiq_compile/offline_imaging

Select the CPU target explicitly. rv32g is a reference target, not a detected
laboratory configuration. Compilation failures return a nonzero exit code and
are retained in compile_report.json. Passing does not validate hardware timing,
host result adapters, laboratory topology, or gateware compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import pprint
import re
import sys
import traceback
from pathlib import Path


def compile_device_db(manifest, target):
    """Build compile-only driver instances; channel numbers are placeholders."""
    if target not in {"rv32g", "rv32ima", "cortexa9"}:
        raise ValueError("Unsupported ARTIQ core target")
    period = manifest["tick_seconds"]
    if not math.isfinite(period) or period <= 0:
        raise ValueError("Invalid core reference period")
    devices = manifest["devices"]
    if "core" in devices or any(n.startswith("__offline_") for n in devices):
        raise ValueError("Export device name conflicts with compile-only device DB")

    def local(module, cls, **arguments):
        return {
            "type": "local",
            "module": f"artiq.coredevice.{module}",
            "class": cls,
            "arguments": arguments,
        }

    db = {"core": local("core", "Core", host=None, ref_period=period, target=target)}
    channel = 0
    buses = {}
    boards = {}
    for name, device in sorted(devices.items()):
        if device["kind"] == "ttl":
            db[name] = local("ttl", "TTLOut", channel=channel)
            channel += 1
            continue
        if device["kind"] != "dds":
            raise ValueError(f"Unsupported compile-only device kind: {device['kind']}")
        bus_key = device.get("bus") or name
        if bus_key not in buses:
            bus_name = f"__offline_spi_{len(buses)}"
            buses[bus_key] = bus_name
            db[bus_name] = local("spi2", "SPIMaster", channel=channel)
            channel += 1
        board_key = device.get("update_group") or name
        fallback_revision = (
            (8 if device.get("ram_profile_control") == "urukul_rev8" else 9)
            if device.get("ram_destination") == "powasf"
            else 9
        )
        revision = device.get("urukul_proto_rev")
        if revision is None:
            revision = fallback_revision
        if type(revision) is not int or revision not in {8, 9}:
            raise ValueError("Invalid Urukul protocol revision")
        if device.get("ram_destination") == "powasf" and revision != fallback_revision:
            raise ValueError("Urukul protocol differs from RAM selector contract")
        clock = device["sysclk_hz"]
        if not math.isfinite(clock) or not 0 < clock <= 1e9:
            raise ValueError("Invalid AD9910 clock")
        if board_key not in boards:
            board_name = f"__offline_cpld_{len(boards)}"
            update_name = f"{board_name}_io_update"
            db[update_name] = local("ttl", "TTLOut", channel=channel)
            channel += 1
            # 1 GHz uses the ordinary 125 MHz / 4 input and PLL. Other
            # declared clocks use compile-only external clock / PLL bypass.
            db[board_name] = local(
                "urukul",
                "CPLD",
                spi_device=buses[bus_key],
                io_update_device=update_name,
                refclk=125e6 if clock == 1e9 else clock,
                clk_div=0 if clock == 1e9 else 1,
                proto_rev=revision,
            )
            boards[board_key] = [board_name, clock, bus_key, 0, revision]
        board = boards[board_key]
        if board[1:3] != [clock, bus_key] or board[3] >= 4 or board[4] != revision:
            raise ValueError("DDS group cannot map to one compile-only Urukul")
        switch = f"__offline_switch_{len(db)}"
        db[switch] = local("ttl", "TTLOut", channel=channel)
        channel += 1
        db[name] = local(
            "ad9910",
            "AD9910",
            chip_select=4 + board[3],
            cpld_device=board[0],
            sw_device=switch,
            pll_en=int(clock == 1e9),
            sync_delay_seed=-1,
            io_update_delay=0,
        )
        board[3] += 1
    return db


def kernel_jobs(experiment, manifest):
    """Cover initialization and every exported point, including parameter rows."""
    generation = manifest["code_generation"]
    jobs = [("initialize_devices", None, [])]
    if generation["mode"] == "unrolled":
        jobs.extend(
            (f"point_{point}", point, []) for point in range(generation["kernel_count"])
        )
    elif generation["mode"] == "auto":
        for group in generation["groups"]:
            for point in group["points"]:
                if experiment._point_groups[point] != int(
                    group["kernel"].split("_")[1]
                ):
                    raise ValueError("Export point dispatch differs from manifest")
                jobs.append((group["kernel"], point, [experiment._point_values[point]]))
        covered = [point for _, point, _ in jobs[1:]]
        if sorted(covered) != list(range(len(experiment._point_values))):
            raise ValueError("Manifest does not cover every parameter row exactly once")
    else:
        raise ValueError("Unknown export mode")
    expected = {name for name, _, _ in jobs}
    actual = {
        name
        for name, method in vars(type(experiment)).items()
        if getattr(method, "artiq_embedded", None) is not None
        and method.artiq_embedded.core_name is not None
    }
    if expected != actual or len(expected) != generation["kernel_count"] + 1:
        raise ValueError("Manifest does not cover every generated kernel")
    return jobs


def validate_toolchain_version(version):
    """The generated source currently targets major version 9 only."""
    major = re.match(r"^(\d+)(?:\D|$)", version)
    if major is None or int(major[1]) != 9:
        raise ValueError(f"Export requires ARTIQ 9; installed version is {version!r}")


def select_kernel_jobs(jobs, points=None):
    """Select explicit point jobs while preserving every lifecycle-only job."""
    if points is None:
        return jobs
    requested = tuple(points)
    if not requested or any(type(point) is not int or point < 0 for point in requested):
        raise ValueError("Selected points must be nonnegative integers")
    selected = set(requested)
    available = {point for _, point, _ in jobs if point is not None}
    if not selected <= available:
        raise ValueError(
            f"Selected point is absent from the export: {sorted(selected - available)}"
        )
    return [job for job in jobs if job[1] is None or job[1] in selected]


def validate_driver_clocks(manager, manifest):
    """Ensure compile-only driver construction preserves the declared clocks."""
    core = manager.get("core")
    if not math.isclose(core.ref_period, manifest["tick_seconds"], rel_tol=1e-12):
        raise ValueError("Compile-only core clock differs from export manifest")
    for name, device in manifest["devices"].items():
        if device["kind"] == "dds" and not math.isclose(
            manager.get(name).sysclk, device["sysclk_hz"], rel_tol=1e-12
        ):
            raise ValueError(f"Compile-only DDS clock differs from manifest: {name}")


def compile_export(export, output, target, *, points=None):
    """Return an honest compilation report; compilation errors remain failures."""
    # Importing this script in the DSL test environment never imports ARTIQ.
    import artiq
    from artiq.master.databases import DeviceDB
    from artiq.master.worker_db import DeviceManager

    export, output = Path(export).resolve(), Path(output).resolve()
    source = export / "experiment.py"
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    # Export hashes use UTF-8 text with normalized LF newlines.
    digest = hashlib.sha256(source.read_text(encoding="utf-8").encode()).hexdigest()
    if digest != manifest["source_sha256"]:
        raise ValueError("Source hash differs from export manifest; re-export first")
    database = compile_device_db(manifest, target)
    output.mkdir(parents=True, exist_ok=True)
    db_path = output / "device_db_compile_only.py"
    db_path.write_text(
        "# SYNTHETIC COMPILE-ONLY DATABASE. NOT A LABORATORY CONFIGURATION.\n"
        "# RTIO channels, DDS chip selects and clock inputs are placeholders.\n"
        "device_db = " + pprint.pformat(database, sort_dicts=False) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "validation": "real_artiq_compile",
        "status": "failed",
        "artiq_version": artiq.__version__,
        "python": sys.version,
        "source_sha256": digest,
        "target": target,
        "device_db": str(db_path),
        "synthetic_device_db": True,
        "kernels_executed": False,
        "hardware_verified": False,
        "kernels": [],
        "selected_points": points,
    }
    manager = DeviceManager(DeviceDB(str(db_path)))
    try:
        validate_toolchain_version(artiq.__version__)
        spec = importlib.util.spec_from_file_location("taiyi_compile_export", source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        experiment = module.GeneratedExperiment((manager, None, None, {}))
        validate_driver_clocks(manager, manifest)
        jobs = select_kernel_jobs(kernel_jobs(experiment, manifest), points)
        for method, point, arguments in jobs:
            record = {"method": method, "point": point, "status": "failed"}
            report["kernels"].append(record)
            label = method if point is None else f"{method}_point_{point}"
            print(f"Compiling {label} for {target}...", flush=True)
            try:
                result = experiment.core.compile(
                    getattr(type(experiment), method), [experiment, *arguments], {}
                )
                elf = result[1]
                if not elf.startswith(b"\x7fELF"):
                    raise ValueError("ARTIQ compiler did not return an ELF binary")
                binary = output / f"{label}.elf"
                binary.write_bytes(elf)
                record.update(
                    status="passed",
                    elf=str(binary),
                    elf_sha256=hashlib.sha256(elf).hexdigest(),
                )
            except Exception:
                record["error"] = traceback.format_exc()
                print(record["error"], file=sys.stderr)
        if all(k["status"] == "passed" for k in report["kernels"]):
            report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
    finally:
        manager.close_devices()
        (output / "compile_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="Trusted TaiyiDSL export directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--point",
        type=int,
        action="append",
        help="Select a point (repeatable); lifecycle kernels remain included.",
    )
    parser.add_argument(
        "--target", choices=("rv32g", "rv32ima", "cortexa9"), required=True
    )
    args = parser.parse_args()
    report = compile_export(args.export, args.output, args.target, points=args.point)
    print(f"Real ARTIQ compilation: {report['status']}. No kernels were executed.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
