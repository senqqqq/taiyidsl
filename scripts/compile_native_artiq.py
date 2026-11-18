"""Compile a declared native/hybrid composition with real ARTIQ, never run it.

Use scripts/artiq-python.ps1 to invoke this file. Extra devices are explicit
synthetic JSON records, not a production device_db. The core has host=None.
Only initialization and top-level point entries are compiled: their called
native routines and generated bodies are included transitively in each binary.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pprint
import sys
import traceback
from pathlib import Path

ALLOWED_MODULES = {
    "artiq.coredevice.ttl",
    "artiq.coredevice.spi2",
    "artiq.coredevice.urukul",
    "artiq.coredevice.ad9910",
    "artiq.coredevice.ad53xx",
    "artiq.coredevice.zotino",
}


def merge_compile_devices(base, extra):
    """Allow only disconnected local-driver fixtures and resolvable aliases."""
    if not isinstance(extra, dict):
        raise ValueError("Extra device database must be a JSON object")
    overlap = set(base) & set(extra)
    if overlap:
        raise ValueError(f"Extra devices replace generated devices: {sorted(overlap)}")
    merged = {**base, **extra}
    for name, entry in extra.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Compile-only device name must be nonempty")
        if isinstance(entry, str):
            target, seen = entry, {name}
            while isinstance(merged.get(target), str):
                if target in seen:
                    raise ValueError("Compile-only device aliases form a cycle")
                seen.add(target)
                target = merged[target]
            if target not in merged:
                raise ValueError(f"Compile-only alias has no target: {name}")
            continue
        if (
            not isinstance(entry, dict)
            or entry.get("type") != "local"
            or entry.get("module") not in ALLOWED_MODULES
            or not isinstance(entry.get("class"), str)
            or not isinstance(entry.get("arguments", {}), dict)
        ):
            raise ValueError(f"Unsupported compile-only native device: {name}")
    return merged


def _helpers():
    path = Path(__file__).resolve().parents[1] / "examples/artiq_offline_compile.py"
    spec = importlib.util.spec_from_file_location("taiyi_compile_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compile_native(
    export,
    output,
    *,
    target,
    extra_devices,
    module_roots=(),
    arguments=None,
    points=None,
):
    """Compile trusted package build code using a synthetic disconnected device DB."""
    import artiq
    from artiq.coredevice.comm_kernel import CommKernelDummy
    from artiq.language.environment import ProcessArgumentManager
    from artiq.master.databases import DeviceDB
    from artiq.master.worker_db import DeviceManager, DummyDevice

    helpers = _helpers()
    helpers.validate_toolchain_version(artiq.__version__)
    export, output = Path(export).resolve(), Path(output).resolve()
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    composition = manifest["native_composition"]
    source = export / "experiment.py"
    digest = hashlib.sha256(source.read_text(encoding="utf-8").encode()).hexdigest()
    if digest != manifest["source_sha256"]:
        raise ValueError("Composed source hash changed; re-export first")
    base = helpers.compile_device_db(manifest, target)
    database = merge_compile_devices(base, extra_devices)
    if database["core"]["arguments"]["host"] is not None:
        raise ValueError("Compile-only core must be disconnected")
    for root in reversed(module_roots):
        directory = Path(root).resolve()
        if not directory.is_dir():
            raise ValueError(f"Native import root does not exist: {directory}")
        sys.path.insert(0, str(directory))
    output.mkdir(parents=True, exist_ok=True)
    db_path = output / "device_db_compile_only.py"
    db_path.write_text(
        "# SYNTHETIC, DISCONNECTED COMPILE-ONLY DEVICES. NEVER USE ON HARDWARE.\n"
        "device_db = " + pprint.pformat(database, sort_dicts=False) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "validation": "real_artiq_native_composition_compile",
        "execution": composition["execution"],
        "status": "failed",
        "artiq_version": artiq.__version__,
        "python": sys.version,
        "target": target,
        "source_sha256": digest,
        "native_source_pins": composition["source_pins"],
        "synthetic_device_db": True,
        "device_db": str(db_path),
        "kernels_executed": False,
        "hardware_verified": False,
        "kernels": [],
        "selected_points": points,
    }
    manager = DeviceManager(
        DeviceDB(str(db_path)), virtual_devices={"scheduler": DummyDevice()}
    )
    try:
        spec = importlib.util.spec_from_file_location(
            "taiyi_native_compile_export", source
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        argument_manager = ProcessArgumentManager(arguments or {})
        experiment = module.GeneratedExperiment((manager, None, argument_manager, {}))
        argument_manager.check_unprocessed_arguments()
        helpers.validate_driver_clocks(manager, manifest)
        experiment._validate_target()
        report["host_target_and_resource_identity_check"] = "passed"
        if "tifffile" in sys.modules:
            tifffile = sys.modules["tifffile"]
            report["host_import_dependencies"] = {
                "tifffile": {"version": tifffile.__version__, "path": tifffile.__file__}
            }
        entries = [("initialize_devices", None)]
        if composition.get("initialize_method"):
            entries.append((composition["initialize_method"], None))
        entries += [
            (f"point_{point['index']}", point["index"])
            for point in composition["points"]
        ]
        if composition.get("session_finalize_method"):
            entries.append((composition["session_finalize_method"], None))
        jobs = helpers.select_kernel_jobs(
            [(name, point, []) for name, point in entries], points
        )
        for name, point, _ in jobs:
            record = {"method": name, "point": point, "status": "failed"}
            report["kernels"].append(record)
            print(f"Compiling composed {name} for {target}...", flush=True)
            try:
                result = experiment.core.compile(
                    getattr(type(experiment), name), [experiment], {}
                )
                binary = result[1]
                if not binary.startswith(b"\x7fELF"):
                    raise ValueError("ARTIQ compiler did not return an ELF binary")
                destination = output / f"{name}.elf"
                destination.write_bytes(binary)
                record.update(
                    status="passed",
                    elf=str(destination),
                    elf_sha256=hashlib.sha256(binary).hexdigest(),
                )
            except Exception:
                record["error"] = traceback.format_exc()
                print(record["error"], file=sys.stderr)
        if all(record["status"] == "passed" for record in report["kernels"]):
            report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
    finally:
        # ARTIQ 9's disconnected communicator has no close method or socket.
        # Exclude only those dummy cores; retain normal cleanup for other drivers.
        manager.active_devices[:] = [
            pair
            for pair in manager.active_devices
            if not isinstance(getattr(pair[1], "comm", None), CommKernelDummy)
        ]
        manager.close_devices()
        (output / "compile_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="The composed export/artiq directory")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--point",
        type=int,
        action="append",
        help="Select a point (repeatable); lifecycle kernels remain included.",
    )
    parser.add_argument(
        "--target", required=True, choices=("rv32g", "rv32ima", "cortexa9")
    )
    parser.add_argument(
        "--extra-device-db", required=True, type=Path, help="Synthetic JSON"
    )
    parser.add_argument("--module-root", action="append", type=Path, default=[])
    parser.add_argument(
        "--arguments", type=Path, help="Optional host build arguments JSON"
    )
    args = parser.parse_args()
    report = compile_native(
        args.export,
        args.output,
        target=args.target,
        extra_devices=json.loads(args.extra_device_db.read_text(encoding="utf-8")),
        module_roots=args.module_root,
        points=args.point,
        arguments=(
            json.loads(args.arguments.read_text(encoding="utf-8"))
            if args.arguments
            else None
        ),
    )
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
