"""Explicit single-card sessions. Compiling and inspecting a shot open no device."""

from __future__ import annotations

import copy
import json
import tempfile
import threading
from pathlib import Path

from ...backends.artiq_spectrum.validation_awg import validate_program
from ...backends.spectrum.dds import validate_square_program
from .._cleanup import run_cleanup
from .awg import SpectrumRuntimeError
from .standalone_card import StandaloneSpcmCard


class StandaloneSpectrumSession:
    """Own one card and execute the compiled host point/shot order."""

    def __init__(self, lab, *, sdk=None):
        if lab.emulation or getattr(lab.backend, "name", lab.backend) != "spectrum":
            raise ValueError(
                "Standalone hardware sessions require a physical Spectrum lab"
            )
        self._config = copy.deepcopy(lab.setup.standalone_spectrum)
        self._hardware = sdk is None
        self._signal_map = copy.deepcopy(lab.signal_map.to_dict())
        address = getattr(lab.setup, "_spectrum_addresses", {}).get(
            self._config.card_name
        )
        self._adapter = StandaloneSpcmCard(
            self._config.card.serial_number, address=address, sdk=sdk
        )
        self._lock = threading.RLock()
        self._closed = False
        self._run_index = 0
        self.observed = None
        self.directory = None

    def open(self):
        with self._lock:
            if self._closed:
                raise SpectrumRuntimeError("The standalone Spectrum session is closed")
            if self.observed is not None:
                return self
            try:
                self.observed = self._adapter.inspect(self._config.contract)
                root = Path.cwd() / "output/hardware_sessions"
                root.mkdir(parents=True, exist_ok=True)
                self.directory = Path(tempfile.mkdtemp(prefix="sp_", dir=root))
            except BaseException as exc:
                run_cleanup([("Spectrum session", self.close)], primary_error=exc)
                raise
            return self

    def _programs(self, compiled, observed):
        contract = self._config.contract
        if compiled.provenance["backend"]["name"] != "spectrum":
            raise ValueError("A standalone session requires a spectrum artifact")
        source = compiled.setup.standalone_spectrum
        if (
            source is None
            or source.contract != contract
            or source.card_name != self._config.card_name
            or source.outputs != self._config.outputs
            or compiled.signal_map.to_dict() != self._signal_map
        ):
            raise ValueError(
                "Compiled Spectrum wiring or card differs from the connected lab"
            )
        programs = []
        for point in compiled.points:
            if (
                len(point.physical_events) != 1
                or point.physical_events[0].kind != "spectrum_shot"
            ):
                raise ValueError(
                    "A standalone point must contain exactly one complete card shot"
                )
            params = point.physical_events[0].params
            program = params["program"]
            if params["contract"] != contract or any(
                program["card_spec"].get(key) != value
                for key, value in contract.items()
            ):
                raise ValueError("Card program differs from the connected contract")
            if contract["mode"] == "awg":
                validate_program(program)
                memory = program["memory"]
                if (
                    memory["allocated_bytes"] > observed["memory_bytes"]
                    or memory["segment_slots"] > observed["max_segments"]
                    or len(program["steps"]) > observed["max_steps"]
                ):
                    raise SpectrumRuntimeError(
                        "AWG program exceeds physical card limits"
                    )
            else:
                validate_square_program(program)
                if program["command_count"] > observed["max_commands"]:
                    raise SpectrumRuntimeError(
                        "DDS program exceeds the physical command queue"
                    )
            programs.append(program)
        return programs

    def run(self, compiled):
        with self._lock:
            if self._closed or self.observed is None:
                raise SpectrumRuntimeError(
                    "The standalone Spectrum session is not connected"
                )
            digest = compiled.sha256
            try:
                observed = self._adapter.inspect(self._config.contract)
            except BaseException as exc:
                run_cleanup([("Spectrum session", self.close)], primary_error=exc)
                raise
            programs = self._programs(compiled, observed)
            self._run_index += 1
            receipt_path = (
                self.directory / f"run_{self._run_index}" / "execution_receipt.json"
            )
            receipt_path.parent.mkdir()
            receipt = {
                "backend": "spectrum",
                "card": observed,
                "mode": self._config.card.mode,
                "clock_source": "internal",
                "trigger_source": "software",
                "execution_mode": "hardware" if self._hardware else "injected_sdk",
                "compiled_sha256": digest,
                "physical_acceptance": "not_verified",
                "measurement_data": None,
                "completed_shots": [],
            }
            awg = self._config.card.mode == "awg"
            resident = None
            receipt["status"] = "running"
            receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            try:
                for point, shot in compiled.execution_order():
                    program = programs[point]
                    if not awg or program != resident:
                        self._adapter.prepare_shot(self._config.contract, program)
                        resident = program if awg else None
                    samples = (
                        program["physical_duration_samples"]
                        if awg
                        else program["duration_samples"]
                    )
                    timeout = 5.0 + samples / self._config.card.sample_rate_hz
                    self._adapter.play_shot(program, timeout)
                    receipt["completed_shots"].append({"point": point, "shot": shot})
            except BaseException as exc:
                # An interrupted shot may already have generated RF.
                receipt.update(
                    status="failed", error=str(exc), execution_state="unknown"
                )
                run_cleanup(
                    [
                        ("Spectrum session", self.close),
                        (
                            "partial shot receipt",
                            lambda: receipt_path.write_text(
                                json.dumps(receipt, indent=2), encoding="utf-8"
                            ),
                        ),
                    ],
                    primary_error=exc,
                )
                raise
            receipt.update(status="completed", hardware_executed=self._hardware)
            receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            return receipt

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._adapter.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        run_cleanup([("Spectrum session", self.close)], primary_error=exc)
