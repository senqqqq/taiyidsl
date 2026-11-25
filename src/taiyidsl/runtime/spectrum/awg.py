"""Explicit host-side Spectrum execution; importing this module opens no device.

The official SDK is imported only by :class:`SpcmAdapter`.  A point is fully
resident before the caller is allowed to run its ARTIQ kernel.  ``ready`` here
means armed and waiting for the external trigger, NOT M2STAT_CARD_READY (which
means that replay has finished).
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

import numpy as np

from ...config.spectrum import (
    EXTERNAL_REFERENCE,
    INTERNAL_PLL_BENCH,
    uncorrected_bench_profile,
)
from ...sample_assets import expand_sample_manifest
from ...spectrum_identity import _canonical
from ...spectrum_identity import calibration_fingerprint as calibration_fingerprint
from ...spectrum_identity import hardware_fingerprint as hardware_fingerprint
from .._cleanup import run_cleanup


class SpectrumRuntimeError(RuntimeError):
    """A point could not safely enter or complete its preloaded execution."""


class SpectrumAdapter(Protocol):
    is_hardware: bool
    clock_session_id: str

    def probe(self) -> dict: ...

    def configure(self, contract: dict) -> None: ...

    def upload(self, program: dict, segment_loader: Callable) -> None: ...

    def select_entry(self, step: int) -> None: ...

    def arm(self) -> None: ...

    def ready(self) -> bool: ...

    def wait_complete(self, timeout_s: float) -> None: ...

    def stop(self) -> None: ...


class SegmentCache:
    """A bounded, content-addressed disk cache; at most one tile is loaded at once."""

    def __init__(self, directory=None, max_bytes=256 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a nonnegative integer")
        self.directory = Path(directory).resolve() if directory is not None else None
        self.max_bytes = max_bytes

    def load(self, program, segment_id):
        from taiyidsl.backends.artiq_spectrum.awg import materialize_segment

        entries = {entry["id"]: entry for entry in program["segments"]}
        entry = entries[segment_id]
        digest = entry["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SpectrumRuntimeError("Invalid segment SHA-256")
        channels = len(program["card_spec"]["active_channels"])
        shape = (entry["sample_count"], channels)
        size = math.prod(shape) * 2
        path = self.directory / f"{digest}.bin" if self.directory else None
        if path is not None and path.is_symlink():
            raise SpectrumRuntimeError("A cached segment must not be a symbolic link")
        if path is not None and path.is_file():
            if path.stat().st_size != size:
                raise SpectrumRuntimeError(f"Corrupt cached segment: {path}")
            data = path.read_bytes()
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise SpectrumRuntimeError(f"Corrupt cached segment: {path}")
            path.touch()
            return np.frombuffer(data, dtype="<i2").reshape(shape)
        array = np.asarray(materialize_segment(program, segment_id))
        if array.shape != shape or array.dtype != np.dtype("<i2"):
            raise SpectrumRuntimeError("Segment materializer returned wrong shape/type")
        data = array.tobytes(order="C")
        if hashlib.sha256(data).hexdigest() != digest:
            raise SpectrumRuntimeError(
                "Materialized segment differs from compiled hash"
            )
        if path is not None and size <= self.max_bytes:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._evict(size)
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(data)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return array

    def _evict(self, incoming):
        files = sorted(
            (
                path
                for path in self.directory.glob("*.bin")
                if re.fullmatch(r"[0-9a-f]{64}\.bin", path.name)
                and path.is_file()
                and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
        )
        total = sum(path.stat().st_size for path in files)
        for path in files:
            if total + incoming <= self.max_bytes:
                break
            total -= path.stat().st_size
            path.unlink()


def _check_clock(card_id, contract, observed):
    """Compare the declared source with actual registers. Never switch clocks."""
    policy = contract.get("clock_policy", EXTERNAL_REFERENCE)
    expected = {"sample_rate_hz": contract["sample_rate_hz"]}
    if policy == INTERNAL_PLL_BENCH:
        if (
            contract.get("mode", "awg") != "awg"
            or contract.get("reference_clock_hz") is not None
        ):
            raise SpectrumRuntimeError(
                f"{card_id}: invalid internal PLL bench contract"
            )
        expected["clock_mode"] = "internal_pll"
    elif policy == EXTERNAL_REFERENCE:
        expected.update(
            clock_mode=EXTERNAL_REFERENCE,
            reference_clock_hz=contract.get("reference_clock_hz", 125_000_000),
        )
    else:
        raise SpectrumRuntimeError(f"{card_id}: unknown clock policy {policy!r}")
    actual = {key: observed.get(key) for key in expected}
    if actual != expected:
        raise SpectrumRuntimeError(
            f"{card_id}: Clock setup changed or not configured: expected {expected}; "
            f"read back {actual}. Set the clock on the stopped, exclusive card; "
            "connect/run does not switch clocks."
        )


def _card_receipt(contract, observed):
    if contract.get("clock_policy") != INTERNAL_PLL_BENCH:
        return dict(observed)
    return {
        **observed,
        "clock_policy": INTERNAL_PLL_BENCH,
        "output_basis": "setpoints_only",
        "shared_reference": False,
        "analog_delay_gain_absolute_phase": "not_verified",
    }


def _preflight_contract(card_id, contract, program, observed, *, validate=True):
    from taiyidsl.backends.artiq_spectrum.banks import validate_bank
    from taiyidsl.backends.artiq_spectrum.validation_awg import validate_program

    if validate:
        if program.get("kind") in {"spectrum_bank", "dds_bank"}:
            validate_bank(program)
        elif program.get("kind") == "dds":
            from taiyidsl.backends.artiq_spectrum.dds import validate_dds_program

            validate_dds_program(program)
        else:
            validate_program(program)
    dds = contract.get("mode", "awg") == "dds"
    if dds != (program.get("kind") in {"dds", "dds_bank"}):
        raise SpectrumRuntimeError(f"{card_id}: program/card mode mismatch")
    for key in ("model", "serial_number"):
        if str(observed.get(key)) != str(contract.get(key)):
            raise SpectrumRuntimeError(
                f"{card_id}: {key} differs from compiled contract"
            )
    if observed.get("model") != "M4i.6631-x8":
        raise SpectrumRuntimeError(f"{card_id}: unsupported card model")
    if dds:
        if not observed.get("dds_available") or not observed.get("dds20_available"):
            raise SpectrumRuntimeError(f"{card_id}: DDS20 mode is unavailable")
    elif not observed.get("sequence_available", False):
        raise SpectrumRuntimeError(f"{card_id}: sequence replay is unavailable")
    if observed.get("is_demo", False):
        raise SpectrumRuntimeError(f"{card_id}: SDK demo card is not physical hardware")
    if not dds:
        memory = int(observed.get("memory_bytes", 0))
        if memory < int(contract["memory_bytes"]):
            raise SpectrumRuntimeError(
                f"{card_id}: installed memory below declared capacity"
            )
        if memory < program["memory"]["allocated_bytes"]:
            raise SpectrumRuntimeError(f"{card_id}: program exceeds installed memory")
        if program["memory"]["segment_slots"] > observed.get("max_segments", 0):
            raise SpectrumRuntimeError(f"{card_id}: segment count exceeds device limit")
        if len(program["steps"]) > observed.get("max_steps", 0):
            raise SpectrumRuntimeError(f"{card_id}: step count exceeds device limit")
    _check_clock(card_id, contract, observed)
    if program["card_spec"].get("clock_policy", EXTERNAL_REFERENCE) != contract.get(
        "clock_policy", EXTERNAL_REFERENCE
    ):
        raise SpectrumRuntimeError(f"{card_id}: rendered program clock policy mismatch")
    expected_fw = contract.get("firmware")
    if expected_fw not in (None, "", "unmeasured") and expected_fw != observed.get(
        "firmware"
    ):
        raise SpectrumRuntimeError(f"{card_id}: firmware changed")
    if program["card_spec"].get("sample_rate_hz") != contract["sample_rate_hz"]:
        raise SpectrumRuntimeError(f"{card_id}: waveform sample rate mismatch")
    if list(program["card_spec"]["active_channels"]) != list(
        contract["active_channels"]
    ):
        raise SpectrumRuntimeError(f"{card_id}: waveform channel layout mismatch")


def _validate_calibration(card_id, contract, observed, adapter, profile, program):
    from ...spectrum_calibration import resolve_channel_calibration

    bench = contract.get("clock_policy") == INTERNAL_PLL_BENCH
    if bench:
        if not uncorrected_bench_profile(profile):
            raise SpectrumRuntimeError(
                f"{card_id}: internal PLL bench requires uncorrected setpoints"
            )
    elif profile.get("status") != "measured" or not profile.get("revision"):
        raise SpectrumRuntimeError(f"{card_id}: measured calibration is required")
    digest = program.get(
        "calibration_sha256", program.get("card_spec", {}).get("calibration_sha256")
    )
    if digest != calibration_fingerprint(profile):
        raise SpectrumRuntimeError(
            f"{card_id}: rendered program calibration fingerprint mismatch"
        )
    if bench:
        return  # Uncorrected setpoints, never evidence of analog calibration.
    expected = hardware_fingerprint(contract, observed)
    if profile.get("card_fingerprints", {}).get(card_id) != expected:
        raise SpectrumRuntimeError(
            f"{card_id}: calibration hardware fingerprint mismatch"
        )
    if profile.get("clock_session_ids", {}).get(card_id) != adapter.clock_session_id:
        raise SpectrumRuntimeError(
            f"{card_id}: clock session changed; recalibration needed"
        )
    if contract.get("mode", "awg") == "dds":
        timing = profile.get("dds_timing", {}).get(card_id, {})
        if timing.get("trigger_latency_ns") != contract.get("dds_trigger_latency_ns"):
            raise SpectrumRuntimeError(
                f"{card_id}: measured DDS trigger timing differs from its contract"
            )
        return
    for channel in contract["active_channels"]:
        item = profile.get("channels", {}).get(f"{card_id}:{channel}", {})
        if item.get("sample_rate_hz") != contract["sample_rate_hz"]:
            raise SpectrumRuntimeError(
                f"{card_id}:{channel}: calibration sample rate mismatch"
            )
        programs = (
            [entry["program"] for entry in program["entries"]]
            if program.get("kind") == "spectrum_bank"
            else [program]
        )
        frequencies = {
            pulse["frequency_hz"]
            for source in programs
            for pulse in source["pulses"]
            if pulse["channel"] == channel
        }
        if not frequencies:
            points = item.get("frequency_points") or [item]
            frequencies = {points[0].get("carrier_frequency_hz")}
        for frequency in frequencies:
            try:
                resolve_channel_calibration(
                    profile, card_id, channel, frequency, require_measured=True
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise SpectrumRuntimeError(str(exc)) from exc


class SpectrumRunner:
    """Run one fully resident point, delegating its single ARTIQ kernel to a caller.

    ``allow_unverified`` is only for nonhardware adapters.  No setting can use it
    to bypass the configured clock/calibration policy on :class:`SpcmAdapter`.
    """

    def __init__(
        self,
        adapters: Mapping[str, SpectrumAdapter],
        *,
        cache_dir=None,
        max_cache_bytes=256 * 1024 * 1024,
        allow_unverified=False,
        ready_timeout_s=5.0,
        completion_timeout_s=60.0,
    ):
        self.adapters = dict(adapters)
        self.cache = SegmentCache(cache_dir, max_cache_bytes)
        self.allow_unverified = bool(allow_unverified)
        for value in (ready_timeout_s, completion_timeout_s):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be finite and positive")
        self.ready_timeout_s = ready_timeout_s
        self.completion_timeout_s = completion_timeout_s
        self._resident_key = None
        self._resident_cards = []
        self._persistent_cards = set()
        self.transport_state = "uninitialized"

    def run_point(self, manifest, point, artiq_runner):
        if self.transport_state == "unknown":
            raise SpectrumRuntimeError(
                "Transport position is unknown after failure; close the session "
                "and re-establish the initial layout before a new attempt"
            )
        if manifest.get("schema_version") in {2, 3}:
            return self.run_resident_point(manifest, point, artiq_runner)
        if manifest.get("schema_version") != 1:
            raise SpectrumRuntimeError("Unsupported Spectrum manifest schema")
        if self._resident_key is not None:
            self.invalidate(preserve_dds=True)
        if type(point) is not int or point < 0:
            raise IndexError("Invalid Spectrum point")
        if not callable(artiq_runner):
            raise TypeError("artiq_runner must be callable")
        matches = [item for item in manifest["points"] if item["index"] == point]
        if len(matches) != 1:
            raise IndexError("Point is missing or duplicated in Spectrum manifest")
        point_plan = copy.deepcopy(matches[0])
        cards = self._cards(point_plan.get("groups", []))
        missing = set(cards) - self.adapters.keys()
        if missing:
            raise SpectrumRuntimeError(f"Missing adapters: {sorted(missing)}")
        if self.allow_unverified and any(
            self.adapters[name].is_hardware for name in cards
        ):
            raise SpectrumRuntimeError(
                "Unverified execution is limited to mock adapters"
            )
        receipt = {
            "point": point,
            "manifest_sha256": hashlib.sha256(
                _canonical(point_plan).encode()
            ).hexdigest(),
            "cards": {},
            "status": "preflight",
            "physical_acceptance": "not_verified",
        }
        touched = []
        try:
            # Read-only preflight of EVERY card precedes any setup or DMA write.
            for card_id, (item, profile) in cards.items():
                adapter = self.adapters[card_id]
                observed = adapter.probe()
                _preflight_contract(
                    card_id, item["contract"], item["program"], observed
                )
                if not self.allow_unverified:
                    _validate_calibration(
                        card_id,
                        item["contract"],
                        observed,
                        adapter,
                        profile,
                        item["program"],
                    )
                receipt["cards"][card_id] = _card_receipt(item["contract"], observed)
            for card_id, (item, _) in cards.items():
                adapter = self.adapters[card_id]
                touched.append(adapter)
                adapter.configure(item["contract"])
                adapter.upload(item["program"], self.cache.load)
            for card_id in cards:
                self.adapters[card_id].arm()
            deadline = time.monotonic() + self.ready_timeout_s
            while cards and not all(self.adapters[name].ready() for name in cards):
                if time.monotonic() >= deadline:
                    raise SpectrumRuntimeError("Spectrum group did not become ready")
                time.sleep(min(0.001, max(0, deadline - time.monotonic())))
            receipt["status"] = "armed"
            # The only transition allowed to emit the experiment's external TTL.
            result = artiq_runner(point)
            for card_id in cards:
                self.adapters[card_id].wait_complete(self.completion_timeout_s)
            receipt["status"] = "complete"
            receipt["artiq_result"] = result
            return receipt
        except BaseException as error:
            run_cleanup(
                [("stop touched card", adapter.stop) for adapter in reversed(touched)]
                + [("abort Spectrum group", self.abort)],
                primary_error=error,
            )
            raise

    def invalidate(self, *, preserve_dds=False):
        """Release AWG residency; only normal bank changes preserve DDS holds."""
        names = list(self._resident_cards)
        if not preserve_dds:
            if self._persistent_cards:
                self.transport_state = "unknown"
            names.extend(sorted(self._persistent_cards - set(names)))
            self._persistent_cards.clear()
        self._resident_key = None
        self._resident_cards = []
        run_cleanup(
            [(f"stop {name}", self.adapters[name].stop) for name in reversed(names)]
        )

    def abort(self):
        """Stop a failed run and invalidate the planned atom state."""
        if self._persistent_cards:
            self.transport_state = "unknown"
        self.invalidate()

    def release_awg_banks(self):
        """Release finite banks on normal run return, retaining live DDS traps."""
        self.invalidate(preserve_dds=True)

    def _cards(self, groups):
        result = {}
        for group in groups:
            for name, item in group["cards"].items():
                if name in result:
                    raise SpectrumRuntimeError(
                        "A card belongs to multiple active groups"
                    )
                result[name] = (item, group.get("calibration", {}))
                if (
                    item["contract"].get("clock_policy") == INTERNAL_PLL_BENCH
                    and len(self.adapters) != 1
                ):
                    raise SpectrumRuntimeError(
                        "Internal PLL bench requires a single-card session"
                    )
        return result

    def load_bank(self, manifest, bank):
        """Preflight all cards before upload, leaving them stopped and resident."""
        try:
            return self._load_bank(manifest, bank)
        except BaseException as error:
            run_cleanup([("abort Spectrum group", self.abort)], primary_error=error)
            raise

    def _load_bank(self, manifest, bank):
        from taiyidsl.backends.artiq_spectrum.banks import entry_program

        expand_sample_manifest(manifest)
        if self.transport_state == "unknown":
            raise SpectrumRuntimeError(
                "Transport position is unknown; close the session"
            )
        if manifest.get("schema_version") not in {2, 3}:
            raise SpectrumRuntimeError("Resident banks require manifest schema 2 or 3")
        expected = hashlib.sha256(
            _canonical(
                {key: value for key, value in bank.items() if key != "sha256"}
            ).encode()
        ).hexdigest()
        if bank.get("sha256") != expected:
            raise SpectrumRuntimeError("Spectrum bank fingerprint mismatch")
        key = (manifest.get("compiled_sha256"), bank["index"], expected)
        cards = self._cards(bank["groups"])
        if set(cards) - self.adapters.keys():
            raise SpectrumRuntimeError("Spectrum bank is missing card adapters")
        if self.allow_unverified and any(
            self.adapters[name].is_hardware for name in cards
        ):
            raise SpectrumRuntimeError(
                "Unverified execution is limited to mock adapters"
            )
        points = {item["index"]: item for item in manifest["points"]}
        # Compare each finite entry with the separately retained point program.
        for point in bank["points"]:
            originals = self._cards(points[point]["groups"])
            for name, (original, profile) in originals.items():
                if (
                    name not in cards
                    or _canonical(entry_program(cards[name][0]["program"], point))
                    != _canonical(original["program"])
                    or (
                        _canonical(cards[name][0]["contract"])
                        != _canonical(original["contract"])
                        or _canonical(cards[name][1]) != _canonical(profile)
                    )
                ):
                    raise SpectrumRuntimeError(
                        "Spectrum bank entry differs from its point program"
                    )
        if self._resident_key == key:
            return False
        for name, (item, profile) in cards.items():
            adapter = self.adapters[name]
            observed = adapter.probe()
            _preflight_contract(name, item["contract"], item["program"], observed)
            if not self.allow_unverified:
                _validate_calibration(
                    name, item["contract"], observed, adapter, profile, item["program"]
                )
        self.invalidate(preserve_dds=True)
        for name, (item, _) in cards.items():
            if item["contract"].get("mode", "awg") == "dds":
                # DDS bank entries are submitted one shot at a time while
                # the controller keeps its previous nonzero hold output.
                continue
            self._resident_cards.append(name)
            self.adapters[name].configure(item["contract"])
            self.adapters[name].upload(item["program"], self.cache.load)
        self._resident_key = key
        return True

    def run_resident_point(self, manifest, point, artiq_runner):
        """Select one finite entry and re-arm it, without repeating resident DMA."""
        from taiyidsl.backends.artiq_spectrum.banks import bank_entry

        expand_sample_manifest(manifest)
        if self.transport_state == "unknown":
            raise SpectrumRuntimeError(
                "Transport position is unknown; close the session"
            )

        if manifest.get("schema_version") not in {2, 3}:
            raise SpectrumRuntimeError("Resident banks require manifest schema 2 or 3")
        if type(point) is not int or point < 0 or not callable(artiq_runner):
            raise SpectrumRuntimeError("Invalid Spectrum point or ARTIQ runner")
        matches = [item for item in manifest["points"] if item["index"] == point]
        banks = [item for item in manifest["banks"] if point in item["points"]]
        if len(matches) != 1 or len(banks) != 1:
            raise SpectrumRuntimeError("Spectrum point bank is missing or duplicated")
        selected, bank = matches[0], banks[0]
        active = self._cards(selected["groups"])
        receipt = {
            "point": point,
            "chunk": bank["index"],
            "bank_sha256": bank["sha256"],
            "manifest_sha256": hashlib.sha256(
                _canonical(selected).encode()
            ).hexdigest(),
            "cards": {},
            "uploaded": False,
            "status": "preflight",
            "physical_acceptance": "not_verified",
        }
        try:
            if active:
                receipt["uploaded"] = self.load_bank(manifest, bank)
                resident = self._cards(bank["groups"])
                # Read-only per-shot checks precede selection/arming on every card.
                for name, (item, profile) in active.items():
                    adapter = self.adapters[name]
                    observed = adapter.probe()
                    _preflight_contract(
                        name,
                        item["contract"],
                        item["program"],
                        observed,
                        validate=False,
                    )
                    if not self.allow_unverified:
                        _validate_calibration(
                            name,
                            item["contract"],
                            observed,
                            adapter,
                            profile,
                            item["program"],
                        )
                    receipt["cards"][name] = _card_receipt(item["contract"], observed)
                    if item["contract"].get("mode", "awg") != "dds":
                        receipt["cards"][name]["entry_step"] = bank_entry(
                            resident[name][0]["program"], point
                        )["entry_step"]
                for name in active:
                    item, _ = active[name]
                    adapter = self.adapters[name]
                    if item["contract"].get("mode", "awg") == "dds":
                        self._persistent_cards.add(name)
                        self.transport_state = "executing"
                        adapter.configure(item["contract"])
                        adapter.upload(item["program"], None)
                    else:
                        adapter.select_entry(receipt["cards"][name]["entry_step"])
                for name in active:
                    self.adapters[name].arm()
                deadline = time.monotonic() + self.ready_timeout_s
                while not all(self.adapters[name].ready() for name in active):
                    if time.monotonic() >= deadline:
                        raise SpectrumRuntimeError(
                            "Spectrum group did not become ready"
                        )
                    time.sleep(min(0.001, max(0, deadline - time.monotonic())))
            receipt["status"] = "armed"
            receipt["artiq_result"] = artiq_runner(point)
            for name in active:
                self.adapters[name].wait_complete(self.completion_timeout_s)
            if self._persistent_cards:
                self.transport_state = "planned_initial_hold"
                receipt["transport_state"] = self.transport_state
                receipt["dds_output"] = "continuous_until_session_close"
            receipt["status"] = "complete"
            return receipt
        except BaseException as error:
            run_cleanup([("abort Spectrum group", self.abort)], primary_error=error)
            raise


class SpcmAdapter:
    """Official SDK adapter for one explicitly identified M4i.6631-x8 card.

    No instrument is opened until ``probe`` or explicit configuration is called.
    A new adapter session invalidates prior phase calibration unless the caller
    supplies the identity of a continuously monitored, unchanged clock session.
    Clock relock/cable changes must invalidate that identity externally.
    """

    is_hardware = True

    def __init__(self, serial_number, *, address=None, sdk=None, clock_session_id=None):
        serial = str(serial_number)
        if not serial.isdecimal() or int(serial) <= 0:
            raise ValueError("A positive physical card serial number is required")
        self.serial_number = serial
        if address is not None and (not isinstance(address, str) or not address):
            raise ValueError("Spectrum address must be nonempty text")
        self.address = address
        self.clock_session_id = clock_session_id or uuid.uuid4().hex
        self._sdk = sdk
        self._card = None
        self._sequence = None
        self._contract = None
        self._armed = False
        self._uploaded = False

    def _open(self):
        if self._card is None:
            if self._sdk is None:
                try:
                    self._sdk = importlib.import_module("spcm")
                except ImportError as exc:
                    raise SpectrumRuntimeError(
                        "Install the optional Spectrum SDK and native driver "
                        "to run hardware"
                    ) from exc
            # Do not combine card_type and serial lookup: older SDK find() accepts
            # the first matching function type even when the serial differs.
            card = (
                self._sdk.Card(self.address)
                if self.address is not None
                else self._sdk.Card(serial_number=int(self.serial_number))
            )
            try:
                card.open()
                if str(card.sn()) != self.serial_number:
                    raise SpectrumRuntimeError(
                        "SDK opened a different card serial number"
                    )
            except BaseException as error:
                if card:  # SDK Card.__bool__ reports whether a native handle exists.
                    # Identity was not verified. Release only the handle; SDK close()
                    # sends a STOP command, potentially to somebody else's card.
                    run_cleanup(
                        [("release unverified Spectrum handle", card.close_handle)],
                        primary_error=error,
                    )
                else:
                    # spcm 1.13.4 open_handle() marks a NULL handle open. Restore
                    # the SDK flag so Device.__del__ does not issue STOP on NULL.
                    card._closed = True
                    target = f" at {self.address}" if self.address else ""
                    error.add_note(
                        f"Cannot open Spectrum S/N {self.serial_number}{target}. "
                        "Check for another process holding the card, device access "
                        "permissions, and the configured address/serial number."
                    )
                raise
            self._card = card
        return self._card

    def probe(self):
        card, sdk = self._open(), self._sdk
        clock = sdk.Clock(card)
        mode = clock.mode()
        return {
            "model": card.product_name(),
            "serial_number": str(card.sn()),
            "memory_bytes": card.get_i(sdk.SPC_PCIMEMSIZE),
            "firmware": (
                f"{card.get_i(sdk.SPC_PCIVERSION):08x}/"
                f"{card.get_i(sdk.SPC_PCIMODULEVERSION):08x}"
            ),
            "sequence_available": bool(card.features() & sdk.SPCM_FEAT_SEQUENCE),
            "max_segments": card.get_i(sdk.SPC_SEQMODE_AVAILMAXSEGMENT),
            "max_steps": card.get_i(sdk.SPC_SEQMODE_AVAILMAXSTEPS),
            "is_demo": bool(card.get_i(sdk.SPC_MIINST_ISDEMOCARD)),
            "sample_rate_hz": clock.sample_rate(),
            # An external reference register left over from a prior setup is
            # not an input frequency when the local PLL is selected.
            "reference_clock_hz": clock.reference_clock()
            if mode == sdk.SPC_CM_EXTREFCLOCK
            else None,
            "clock_mode": (
                "external_reference"
                if mode == sdk.SPC_CM_EXTREFCLOCK
                else "internal_pll"
                if mode == sdk.SPC_CM_INTPLL
                else "other"
            ),
        }

    def configure_for_calibration(self, contract):
        """Explicitly configure the clock and invalidate the previous calibration.

        This operates hardware; it is never called implicitly by the runner.
        Collect a new measured calibration before attempting a normal run.
        """
        if contract.get("clock_policy", EXTERNAL_REFERENCE) != EXTERNAL_REFERENCE:
            raise SpectrumRuntimeError(
                "External-reference calibration setup is not for internal PLL bench"
            )
        if self._contract is not None and self._contract.get("mode") == "dds":
            raise SpectrumRuntimeError(
                "Close the live DDS session before recalibration"
            )
        card, sdk = self._open(), self._sdk
        # Once a clock change is attempted the old calibration cannot be trusted,
        # even if a later write/readback fails. Never commit an inherited step table.
        self.clock_session_id = uuid.uuid4().hex
        self._uploaded = False
        self._sequence = None
        try:
            card.stop()
            self._armed = False
            card.timeout(10_000)
            self._stage_stopped_single()
            clock = sdk.Clock(card)
            clock.mode(sdk.SPC_CM_EXTREFCLOCK)
            clock.reference_clock(contract.get("reference_clock_hz", 125_000_000))
            clock.sample_rate(contract["sample_rate_hz"])
            self._commit_muted_outputs()
        except BaseException as error:
            # No program exists yet, even when this is a fresh DDS calibration.
            run_cleanup(
                [("release clock setup", lambda: self._stop_and_mute(awg=True))],
                primary_error=error,
            )
            raise
        self.configure(contract)
        return self.probe()

    def configure(self, contract):
        card, sdk = self._open(), self._sdk
        self._uploaded = False
        self._sequence = None
        observed = self.probe()
        _check_clock(self.serial_number, contract, observed)
        # Take ownership before the first mutation, including a partial setup.
        self._contract = copy.deepcopy(contract)
        card.stop()
        self._armed = False
        card.timeout(10_000)
        if card.card_mode(sdk.SPC_REP_STD_SEQUENCE) != sdk.SPC_REP_STD_SEQUENCE:
            raise SpectrumRuntimeError("Device did not enter static sequence mode")
        self._configure_outputs(contract)
        trigger = sdk.Trigger(card)
        trigger_settings = (
            ("or_mask", sdk.SPC_TMASK_EXT0),
            ("and_mask", 0),
            ("ext0_mode", sdk.SPC_TM_POS),
            ("ext0_coupling", sdk.COUPLING_DC),
            ("termination", int(contract.get("trigger_termination_ohm", 50) == 50)),
            ("ext0_level0", contract.get("trigger_level_mv", 1000)),
            # The compiler's calibrated trigger_latency_samples assumes zero.
            ("delay", 0),
        )
        for setting, expected in trigger_settings:
            if getattr(trigger, setting)(expected) != expected:
                raise SpectrumRuntimeError(f"Trigger {setting} readback mismatch")
        # Stage only. Even while stopped, WRITESETUP validates the entry step.
        # upload() commits after DMA, the entire step table and start_step(0).
        self._sequence = sdk.Sequence(card)

    def _configure_outputs(self, contract, *, awg=True):
        card, sdk = self._card, self._sdk
        # Explicitly turn off unused output amplifiers; channel selection alone
        # does not define the state left by a previous experiment.
        for channel in sdk.Channels(card, card_enable=3):
            channel.enable(False)
            if (
                awg
                and channel.stop_level(sdk.SPCM_STOPLVL_ZERO) != sdk.SPCM_STOPLVL_ZERO
            ):
                raise SpectrumRuntimeError("Device did not accept zero stop level")
        mask = sum(1 << channel for channel in contract["active_channels"])
        channels = sdk.Channels(card, card_enable=mask)
        ranges = contract["output_range_mv"]
        for channel in channels:
            index = channel.index
            level = (
                ranges.get(str(index), ranges.get(index))
                if isinstance(ranges, Mapping)
                else ranges
            )
            if channel.amp(level) != level:
                raise SpectrumRuntimeError("Output range was rounded by the driver")
            if channel.offset(0) != 0:
                raise SpectrumRuntimeError("Device did not accept zero output offset")
            if awg:
                channel.enable(True)
        return channels

    def upload(self, program, segment_loader):
        if self._sequence is None or self._armed:
            raise SpectrumRuntimeError("Configure a stopped card before uploading")
        # Repartitioning destroys the old bank. A failed replacement is not armable.
        self._uploaded = False
        sdk, card, sequence = self._sdk, self._card, self._sequence
        sequence.max_segments(program["memory"]["segment_slots"])
        segment_indices = {}
        for index, segment in enumerate(program["segments"]):
            data = segment_loader(program, segment["id"])
            sequence.write_segment(index)
            sequence.segment_size(segment["sample_count"])
            transfer = sdk.DataTransfer(card)
            transfer.allocate_buffer(segment["sample_count"])
            transfer.buffer[:] = data.T
            transfer.start_buffer_transfer(
                sdk.M2CMD_DATA_STARTDMA, sdk.M2CMD_DATA_WAITDMA
            )
            segment_indices[segment["id"]] = index
        for step in program["steps"]:
            sequence.step_memory(
                step["index"],
                next_step_index=step["next_step"],
                segment_index=segment_indices[step["segment_id"]],
                loops=step["loops"],
                flags=sdk.SPCSEQ_END if step["end"] else sdk.SPCSEQ_ENDLOOPALWAYS,
            )
        sequence.start_step(0)
        card.write_setup()
        self._entries = {
            entry["entry_step"] for entry in program.get("entries", [])
        } or {0}
        self._uploaded = True

    def select_entry(self, step):
        if not self._uploaded or self._armed:
            raise SpectrumRuntimeError("Select a resident entry on a stopped card")
        if type(step) is not int or step not in self._entries:
            raise SpectrumRuntimeError("Unknown resident Spectrum entry")
        self._uploaded = False
        self._sequence.start_step(step)
        self._card.write_setup()
        self._uploaded = True

    def arm(self):
        if not self._uploaded or self._armed:
            raise SpectrumRuntimeError(
                "Upload a complete stopped program before arming"
            )
        self._card.start(self._sdk.M2CMD_CARD_ENABLETRIGGER)
        self._armed = True

    def ready(self):
        if not self._armed:
            return False
        status = self._card.status()
        if status & self._sdk.M2STAT_CARD_TRIGGER:
            raise SpectrumRuntimeError("Unexpected trigger before the ARTIQ launch")
        return not bool(status & self._sdk.M2STAT_CARD_READY)

    def wait_complete(self, timeout_s):
        if not self._armed:
            raise SpectrumRuntimeError("Card was not armed")
        self._card.timeout(max(1, math.ceil(timeout_s * 1000)))
        self._card.cmd(self._sdk.M2CMD_CARD_WAITREADY)
        status = self._card.status()
        expected = self._sdk.M2STAT_CARD_READY | self._sdk.M2STAT_CARD_TRIGGER
        if status & expected != expected:
            raise SpectrumRuntimeError(
                "Replay ended without a confirmed external trigger"
            )
        self._armed = False

    def _stage_stopped_single(self):
        # A clock/mute-only commit must not validate an old or partial sequence.
        # The single-replay length is legal on this model. Never start this mode.
        sdk, card = self._sdk, self._card
        if card.card_mode(sdk.SPC_REP_STD_SINGLE) != sdk.SPC_REP_STD_SINGLE:
            raise SpectrumRuntimeError(
                "Device did not enter stopped single-replay mode"
            )
        card.set_i(sdk.SPC_MEMSIZE, 4096)

    def _commit_muted_outputs(self):
        for channel in self._sdk.Channels(self._card, card_enable=3):
            channel.enable(False)
        self._card.write_setup()

    def stop(self):
        self._sequence = None
        self._armed = self._uploaded = False
        if self._card is None or self._contract is None:
            return  # Probe-only sessions own no playback setup.
        self._stop_and_mute(awg=self._contract.get("mode", "awg") == "awg")
        self._contract = None

    def _stop_and_mute(self, *, awg):
        actions = [
            (
                "stop Spectrum replay/DMA",
                lambda: self._card.stop(self._sdk.M2CMD_DATA_STOPDMA),
            )
        ]
        if awg:
            actions.append(("leave AWG sequence mode", self._stage_stopped_single))
        actions.append(("disable Spectrum outputs", self._commit_muted_outputs))
        run_cleanup(actions)

    def close(self):
        try:
            if self._card is not None:
                run_cleanup(
                    [
                        ("release Spectrum setup", self.stop),
                        ("close Spectrum card", self._card.close),
                    ]
                )
        finally:
            self._card = self._sequence = self._contract = None
            self._armed = self._uploaded = False
