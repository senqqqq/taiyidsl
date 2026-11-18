"""Shared resident segments with independent finite entry paths for scan points."""

from __future__ import annotations

import copy
import hashlib
import json

import numpy as np

from taiyidsl.backends.artiq_spectrum.awg import _memory, materialize_segment
from taiyidsl.backends.artiq_spectrum.validation_awg import validate_program
from taiyidsl.ir import CompileError


def _identity(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class BankBudget:
    """Small exact union of DAC hashes and sequence-step counts, without samples."""

    def __init__(self):
        self.cards = {}

    def add_point(self, point):
        for event in point.physical_events:
            if event.kind != "spectrum_program":
                continue
            for name, item in event.params["cards"].items():
                program = item["program"]
                if program.get("kind") == "dds":
                    from taiyidsl.backends.artiq_spectrum.dds import (
                        validate_dds_program,
                    )

                    validate_dds_program(program)
                    # DDS SINGLE capacity is per shot, not an accumulated AWG bank.
                    continue
                identity = _identity(
                    (item["contract"], event.params.get("calibration", {}))
                )
                card = self.cards.setdefault(
                    name,
                    {
                        "identity": identity,
                        "spec": program["card_spec"],
                        "segments": {},
                        "steps": 0,
                    },
                )
                if card["identity"] != identity:
                    raise CompileError("A chunk cannot mix card/calibration contracts")
                card["steps"] += len(program["steps"])
                for segment in program["segments"]:
                    card["segments"][(segment["sample_count"], segment["sha256"])] = {
                        "sample_count": segment["sample_count"]
                    }

    def merged(self, other):
        result = copy.deepcopy(self)
        for name, incoming in other.cards.items():
            if name not in result.cards:
                result.cards[name] = copy.deepcopy(incoming)
                continue
            card = result.cards[name]
            if card["identity"] != incoming["identity"]:
                raise CompileError("A chunk cannot mix card/calibration contracts")
            card["segments"].update(incoming["segments"])
            card["steps"] += incoming["steps"]
        return result

    def validate(self):
        for name, card in self.cards.items():
            try:
                _memory(card["spec"], list(card["segments"].values()))
                if card["steps"] > card["spec"]["max_steps"]:
                    raise CompileError("Spectrum bank exceeds the sequence step limit")
            except CompileError as exc:
                raise CompileError(f"Card {name}: {exc}") from exc


def build_bank(programs):
    """Share exact DAC bytes; point paths end independently and never auto-chain."""
    if not programs:
        raise CompileError("A Spectrum bank requires at least one point")
    first = next(iter(programs.values()))
    if first.get("kind") == "dds":
        return _build_dds_bank(programs)
    bank = {
        "schema_version": 1,
        "kind": "spectrum_bank",
        "card_spec": copy.deepcopy(first["card_spec"]),
        "max_tile_samples": first["max_tile_samples"],
        "calibration_sha256": first.get("calibration_sha256"),
        "segments": [],
        "steps": [],
        "entries": [],
    }
    by_hash = {}
    for point, program in programs.items():
        if type(point) is not int or point < 0:
            raise CompileError(
                "Spectrum bank point indices must be nonnegative integers"
            )
        if any(
            _identity(program.get(key)) != _identity(bank.get(key))
            for key in ("card_spec", "max_tile_samples", "calibration_sha256")
        ):
            raise CompileError(
                "A Spectrum bank cannot mix card or calibration contracts"
            )
        validate_program(program)
        segment_ids = []
        for segment in program["segments"]:
            key = (segment["sample_count"], segment["sha256"])
            slot = by_hash.get(key)
            if slot is None:
                slot = len(bank["segments"])
                by_hash[key] = slot
                bank["segments"].append({**copy.deepcopy(segment), "id": slot})
                _memory(bank["card_spec"], bank["segments"])
            elif not np.array_equal(
                materialize_segment(bank, slot),
                materialize_segment(program, segment["id"]),
            ):
                raise CompileError("Spectrum bank segment hash collision")
            segment_ids.append(slot)
        entry = len(bank["steps"])
        for step in program["steps"]:
            bank["steps"].append(
                {
                    **step,
                    "index": entry + step["index"],
                    "next_step": entry + step["next_step"],
                    "segment_id": segment_ids[step["segment_id"]],
                }
            )
        if len(bank["steps"]) > bank["card_spec"]["max_steps"]:
            raise CompileError("Spectrum bank exceeds the sequence step limit")
        bank["entries"].append(
            {
                "point": point,
                "entry_step": entry,
                "step_count": len(program["steps"]),
                "segment_ids": segment_ids,
                # Equal DAC bytes may come from different pulse/source recipes.
                # Keep per-point provenance without duplicating resident samples.
                "segment_recipes": [
                    copy.deepcopy(segment["recipe"]) for segment in program["segments"]
                ],
                "program": {
                    key: copy.deepcopy(value)
                    for key, value in program.items()
                    if key not in {"segments", "steps", "memory"}
                },
            }
        )
    bank["memory"] = _memory(bank["card_spec"], bank["segments"])
    validate_bank(bank)
    return bank


def bank_entry(bank, point):
    matches = [entry for entry in bank["entries"] if entry["point"] == point]
    if len(matches) != 1:
        raise CompileError("Spectrum bank point entry is missing or duplicated")
    return matches[0]


def entry_program(bank, point):
    """Decode one bank entry back into the ordinary single-point program format."""
    entry = bank_entry(bank, point)
    if bank.get("kind") == "dds_bank":
        return copy.deepcopy(entry["program"])
    start, count = entry["entry_step"], entry["step_count"]
    if type(start) is not int or type(count) is not int or start < 0 or count < 1:
        raise CompileError("Invalid Spectrum bank entry range")
    ids = entry["segment_ids"]
    recipes = entry["segment_recipes"]
    if (
        len(set(ids)) != len(ids)
        or any(
            type(slot) is not int or not 0 <= slot < len(bank["segments"])
            for slot in ids
        )
        or len(recipes) != len(ids)
    ):
        raise CompileError("Invalid Spectrum bank segment references")
    reverse = {slot: index for index, slot in enumerate(ids)}
    result = copy.deepcopy(entry["program"])
    for key in ("card_spec", "max_tile_samples", "calibration_sha256"):
        if _identity(result.get(key)) != _identity(bank.get(key)):
            raise CompileError("Spectrum bank entry contract differs from its bank")
    result["segments"] = [
        {
            **copy.deepcopy(bank["segments"][slot]),
            "id": index,
            "recipe": copy.deepcopy(recipes[index]),
        }
        for index, slot in enumerate(ids)
    ]
    steps = bank["steps"][start : start + count]
    if len(steps) != count:
        raise CompileError("Spectrum bank entry exceeds its step table")
    result["steps"] = []
    for index, step in enumerate(steps):
        if (
            step["index"] != start + index
            or step["segment_id"] not in reverse
            or not start <= step["next_step"] < start + count
        ):
            raise CompileError("Spectrum entry escapes its finite point path")
        result["steps"].append(
            {
                **step,
                "index": index,
                "next_step": step["next_step"] - start,
                "segment_id": reverse[step["segment_id"]],
            }
        )
    result["memory"] = _memory(result["card_spec"], result["segments"])
    return result


def validate_bank(bank):
    """Validate all declared entries, exact samples, shared slots and total steps."""
    if bank.get("kind") == "dds_bank":
        return _validate_dds_bank(bank)
    if bank.get("schema_version") != 1 or bank.get("kind") != "spectrum_bank":
        raise CompileError("Unsupported Spectrum bank schema")
    if [s["id"] for s in bank["segments"]] != list(range(len(bank["segments"]))):
        raise CompileError("Spectrum bank segment slots must be consecutive")
    if not 1 <= len(bank["steps"]) <= bank["card_spec"]["max_steps"]:
        raise CompileError("Spectrum bank exceeds the sequence step limit")
    if _memory(bank["card_spec"], bank["segments"]) != bank["memory"]:
        raise CompileError("Spectrum bank memory accounting differs from its segments")
    cursor, points, used = 0, set(), set()
    for entry in bank["entries"]:
        point = entry["point"]
        if type(point) is not int or point < 0 or point in points:
            raise CompileError("Spectrum bank point entries must be unique")
        points.add(point)
        if entry["entry_step"] != cursor:
            raise CompileError(
                "Spectrum bank contains unreachable or overlapping steps"
            )
        validate_program(entry_program(bank, point))
        used.update(entry["segment_ids"])
        cursor += entry["step_count"]
    if cursor != len(bank["steps"]) or used != set(range(len(bank["segments"]))):
        raise CompileError("Spectrum bank has unreachable steps or unused segments")
    # Entry validation above checks every slot's materialization bounds first.
    # Upload uses the shared slot recipes, independently of point provenance.
    for segment in bank["segments"]:
        packed = materialize_segment(bank, segment["id"])
        if hashlib.sha256(packed.tobytes()).hexdigest() != segment["sha256"]:
            raise CompileError("Spectrum bank packed segment hash mismatch")
    return {
        "status": "passed",
        "point_count": len(points),
        "segment_count": len(bank["segments"]),
        "step_count": len(bank["steps"]),
    }


def build_manifest_banks(points, plan):
    """Build fixed-contract card banks for each declared host chunk."""
    by_point = {point["index"]: point for point in points}
    result = []
    for chunk in plan["chunks"]:
        groups = {}
        for point in chunk["points"]:
            for group in by_point[point]["groups"]:
                target = groups.setdefault(
                    group["name"],
                    {
                        "name": group["name"],
                        "calibration": group["calibration"],
                        "cards": {},
                    },
                )
                if _identity(target["calibration"]) != _identity(group["calibration"]):
                    raise CompileError("A chunk cannot mix group calibration contracts")
                for name, card in group["cards"].items():
                    item = target["cards"].setdefault(
                        name, {"contract": card["contract"], "points": {}}
                    )
                    if _identity(item["contract"]) != _identity(card["contract"]):
                        raise CompileError("A chunk cannot mix card configurations")
                    item["points"][point] = card["program"]
        for group in groups.values():
            for name, item in group["cards"].items():
                try:
                    item["program"] = build_bank(item.pop("points"))
                except CompileError as exc:
                    raise CompileError(
                        f"Chunk {chunk['index']}, card {name}: {exc}; "
                        "use smaller sweep chunks; a single point cannot be split"
                    ) from exc
        bank = {**copy.deepcopy(chunk), "groups": list(groups.values())}
        bank["sha256"] = hashlib.sha256(_identity(bank).encode()).hexdigest()
        result.append(bank)
    return result


def validate_chunk_capacity(points, plan):
    """Check declared chunk banks using existing segment hashes, without rendering."""
    by_point = {point.index: point for point in points}
    for chunk in plan["chunks"]:
        cards = {}
        for point in chunk["points"]:
            for event in by_point[point].physical_events:
                if event.kind != "spectrum_program":
                    continue
                for name, item in event.params["cards"].items():
                    program = item["program"]
                    if program.get("kind") == "dds":
                        from taiyidsl.backends.artiq_spectrum.dds import (
                            validate_dds_program,
                        )

                        validate_dds_program(program)
                        continue
                    summary = cards.setdefault(
                        name, {"spec": program["card_spec"], "segments": {}, "steps": 0}
                    )
                    summary["steps"] += len(program["steps"])
                    for segment in program["segments"]:
                        key = (segment["sample_count"], segment["sha256"])
                        summary["segments"][key] = segment
        for name, summary in cards.items():
            try:
                _memory(summary["spec"], list(summary["segments"].values()))
                if summary["steps"] > summary["spec"]["max_steps"]:
                    raise CompileError("Spectrum bank exceeds the sequence step limit")
            except CompileError as exc:
                raise CompileError(
                    f"Chunk {chunk['index']}, card {name}: {exc}; "
                    "use smaller sweep chunks; a single point cannot be split"
                ) from exc


def _build_dds_bank(programs):
    first = next(iter(programs.values()))
    bank = {
        "schema_version": 1,
        "kind": "dds_bank",
        "card_spec": copy.deepcopy(first["card_spec"]),
        "entries": [
            {"point": point, "program": copy.deepcopy(program)}
            for point, program in programs.items()
        ],
    }
    _validate_dds_bank(bank)
    return bank


def _validate_dds_bank(bank):
    from taiyidsl.backends.artiq_spectrum.dds import validate_dds_program

    if bank.get("schema_version") != 1 or not bank.get("entries"):
        raise CompileError("Invalid DDS bank schema or empty entries")
    points, initial = set(), None
    for entry in bank["entries"]:
        point, program = entry["point"], entry["program"]
        if type(point) is not int or point < 0 or point in points:
            raise CompileError("DDS bank point entries must be unique")
        points.add(point)
        validate_dds_program(program)
        if _identity(program["card_spec"]) != _identity(bank["card_spec"]):
            raise CompileError("DDS bank card contracts differ")
        state = _identity(program["initial_tones"])
        if initial is not None and initial != state:
            raise CompileError("DDS bank points require one initial hold state")
        initial = state
        if _identity(program["final_tones"]) != state:
            raise CompileError("DDS bank shot must return to initial hold state")
    return {"status": "passed", "point_count": len(points), "kind": "dds_bank"}
