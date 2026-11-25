"""Private line-based host worker; imports drivers only after explicit connect."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pprint
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..config.spectrum import EXTERNAL_REFERENCE, hybrid_output_basis
from ..sample_assets import expand_sample_manifest
from ._cleanup import run_cleanup


class ReceiptOnlyAdapter:
    """Host loop bookkeeping for experiments without DSL acquisitions."""

    def __init__(self):
        self.completed = []

    def before_shot(self, point, shot):
        pass

    def after_shot(self, point, shot):
        self.completed.append({"point": point, "shot": shot})

    def finish(self):
        pass


def native_source_roots(plan):
    """Derive import roots only from source pins already declared by the user."""
    roots = []

    def visit(value):
        if isinstance(value, dict):
            if {"module", "path", "sha256"} <= value.keys():
                source = Path(value["path"]).resolve(strict=True)
                if hashlib.sha256(source.read_bytes()).hexdigest() != value["sha256"]:
                    raise ValueError(f"Native source changed: {value['module']}")
                depth = len(value["module"].split("."))
                root = source.parents[
                    depth - 1 if source.name != "__init__.py" else depth
                ]
                if str(root) not in roots:
                    roots.append(str(root))
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan.get("metadata", {}).get("native_artiq", {}).get("package", {}))
    return roots


@contextmanager
def _loaded_program(source, plan):
    """Own only this generated module and import paths added by this load.

    Native modules remain registered: their classes and session objects can
    outlive a generated host program, and the loader verifies their identities.
    """
    roots = native_source_roots(plan)
    name = "_taiyidsl_session_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    paths = []
    sys.modules[name] = module
    try:
        for source_root in reversed(roots):
            if source_root not in sys.path:
                sys.path.insert(0, source_root)
                paths.append(source_root)
        spec.loader.exec_module(module)
        yield module
    finally:
        if sys.modules.get(name) is module:
            del sys.modules[name]
        for owned in paths:
            for index, value in enumerate(sys.path):
                if value is owned:
                    del sys.path[index]
                    break


def _execution_policy(contract, group_basis):
    clock = contract.get("clock_policy", EXTERNAL_REFERENCE)
    return clock, hybrid_output_basis(clock, group_basis)


class Worker:
    def __init__(self, config, directory):
        self.config, self.directory = config, directory
        self.manager = None
        self.adapters = {}
        self.runner = None

    def connect(self):
        if self.manager is not None or self.adapters or self.runner is not None:
            raise RuntimeError("Worker is already connected")
        try:
            return self._connect()
        except BaseException as error:
            run_cleanup(
                [("partial worker connection", self.close)], primary_error=error
            )
            raise

    def _connect(self):
        from artiq.master.databases import DeviceDB
        from artiq.master.worker_db import DeviceManager

        from ..config.descriptor import resolve_device
        from .spectrum.awg import SpcmAdapter, SpectrumRunner, _check_clock
        from .spectrum.dds import SpcmDDSAdapter

        device_file = self.directory / "device_db_snapshot.py"
        device_file.write_text(
            "device_db = " + pprint.pformat(self.config["device_db"]) + "\n",
            encoding="utf-8",
        )
        self.manager = DeviceManager(DeviceDB(str(device_file)))
        core = self.manager.get("core")
        core_record = resolve_device(self.config["device_db"], "core")[1]
        if core_record["arguments"].get("host") is None:
            raise ValueError("Hardware connection requires a real ARTIQ core host")
        core.comm.check_system_info()
        observed = {}
        for group in (self.config.get("spectrum") or {}).get("groups", {}).values():
            for uid, contract in group["cards"].items():
                adapter_type = (
                    SpcmDDSAdapter
                    if contract.get("mode", "awg") == "dds"
                    else SpcmAdapter
                )
                adapter = adapter_type(
                    contract["serial_number"],
                    address=self.config["addresses"].get(uid),
                )
                self.adapters[uid] = adapter
                actual = adapter.probe()
                for key in ("model", "serial_number"):
                    if str(actual[key]) != str(contract[key]):
                        raise ValueError(f"{uid}: connected {key} differs from spec_db")
                if actual["is_demo"]:
                    raise ValueError(f"{uid}: a physical Spectrum card is required")
                _check_clock(uid, contract, actual)
                if contract.get("mode", "awg") == "dds" and (
                    not actual.get("dds_available")
                    or not actual.get("dds20_available")
                    or actual.get("num_cores") != 21
                    or actual.get("max_commands", 0) < 4096
                ):
                    raise ValueError(f"{uid}: native DDS20 topology/capacity required")
                observed[uid] = actual
        self.runner = SpectrumRunner(self.adapters)
        return {
            "connected": True,
            "hardware_executed": False,
            "cards": observed,
        }

    def run(self, directory):
        try:
            return self._run(directory)
        except BaseException as error:
            if self.runner is not None:
                run_cleanup(
                    [("abort Spectrum execution", self.runner.abort)],
                    primary_error=error,
                )
            raise

    def _run(self, directory):
        from artiq.language.environment import ProcessArgumentManager

        from .spectrum.awg import _preflight_contract, _validate_calibration

        root = Path(directory).resolve(strict=True)
        source = root / "experiment.py"
        manifest = expand_sample_manifest(
            json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        )
        # Refuse acquisition-bearing exports even if invoked outside public API.
        plan = json.loads((root / "compiled.json").read_text(encoding="utf-8"))
        if any(
            event["kind"] == "acquire"
            for point in plan["points"]
            for event in point["logical_events"]
        ):
            raise ValueError("No detector result adapter is configured")
        # Check all cards and their declared calibration policy before any generated
        # initialization kernel, upload or native build can run.
        policies = {
            uid: _execution_policy(contract, group.get("output_basis"))
            for group in (self.config.get("spectrum") or {}).get("groups", {}).values()
            for uid, contract in group["cards"].items()
        }
        for point in manifest.get("points", []):
            for group in point.get("groups", []):
                for uid, card in group["cards"].items():
                    contract = card["contract"]
                    if _execution_policy(
                        contract, contract.get("output_basis")
                    ) != policies.get(uid):
                        raise ValueError(
                            f"{uid}: compiled clock/output policy differs "
                            "from connected setup"
                        )
                    adapter = self.adapters[uid]
                    observed = adapter.probe()
                    _preflight_contract(
                        uid, card["contract"], card["program"], observed
                    )
                    _validate_calibration(
                        uid,
                        card["contract"],
                        observed,
                        group["calibration"],
                        card["program"],
                    )
        # Preserve import-origin validation in the generated loader. Supplying
        # the declared roots makes existing packages importable without asking
        # the experiment author to manipulate sys.path in every notebook.
        with _loaded_program(source, plan) as module:
            arguments = ProcessArgumentManager({})
            experiment = module.GeneratedExperiment((self.manager, None, arguments, {}))
            arguments.check_unprocessed_arguments()
            result_adapter = ReceiptOnlyAdapter()
            experiment.result_adapter = result_adapter
            if (
                manifest.get("backend") == "artiq_spectrum"
                or "points" in manifest
                and any(p.get("groups") for p in manifest["points"])
            ):
                experiment.spectrum_runner = self.runner
            experiment.run()
            return {
                "status": "completed",
                "hardware_executed": True,
                "physical_acceptance": "not_verified",
                "measurement_data": None,
                "completed_shots": result_adapter.completed,
                "spectrum_receipts": getattr(experiment, "spectrum_receipts", []),
            }

    def close(self):
        runner, self.runner = self.runner, None
        adapters, self.adapters = self.adapters, {}
        manager, self.manager = self.manager, None
        actions = []
        if runner is not None:
            actions.append(("invalidate Spectrum runner", runner.invalidate))
        actions.extend(
            (f"close Spectrum adapter {name}", adapter.close)
            for name, adapter in adapters.items()
        )
        if manager is not None:
            actions.append(("close ARTIQ devices", manager.close_devices))
        run_cleanup(actions)
