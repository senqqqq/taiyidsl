"""Export actual resident bank slots and finite point entries for offline review."""

import hashlib
import json

from taiyidsl.backends.artiq_spectrum.awg import materialize_segment

from .spectrum_report import card_execution


def export_bank_reports(manifest, output):
    reports = []
    for bank in manifest.get("banks", []):
        cards = []
        for group_index, group in enumerate(bank["groups"]):
            for card_index, (name, item) in enumerate(group["cards"].items()):
                directory = (
                    output / f"bank_{bank['index']}" / f"{group_index}_{card_index}"
                )
                directory.mkdir(parents=True, exist_ok=True)
                program = item["program"]
                execution = card_execution(item)
                if program.get("kind") == "dds_bank":
                    report = {
                        "group": group["name"],
                        "card": name,
                        "mode": "dds",
                        "trigger_device": item["contract"]["trigger_device"],
                        "directory": directory.relative_to(output).as_posix(),
                        "entries": [
                            {
                                "point": e["point"],
                                "command_count": e["program"]["command_count"],
                            }
                            for e in program["entries"]
                        ],
                        "execution": execution,
                    }
                    (directory / "bank.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    (directory / "sdk_calls.txt").write_text(
                        execution["upload_listing"], encoding="utf-8"
                    )
                    cards.append(report)
                    continue
                for segment in program["segments"]:
                    packed = materialize_segment(program, segment["id"]).tobytes()
                    if hashlib.sha256(packed).hexdigest() != segment["sha256"]:
                        raise ValueError("Bank review sample hash differs from export")
                    (directory / f"segment_{segment['id']}.bin").write_bytes(packed)
                (directory / "sdk_calls.txt").write_text(
                    execution["upload_listing"], encoding="utf-8"
                )
                entries = [
                    {
                        key: entry[key]
                        for key in ("point", "entry_step", "step_count", "segment_ids")
                    }
                    for entry in program["entries"]
                ]
                report = {
                    "group": group["name"],
                    "card": name,
                    "trigger_device": item["contract"]["trigger_device"],
                    "directory": directory.relative_to(output).as_posix(),
                    "memory": program["memory"],
                    "entries": entries,
                    "execution": execution,
                }
                (directory / "bank.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                cards.append(report)
        reports.append(
            {
                "index": bank["index"],
                "points": bank["points"],
                "sha256": bank["sha256"],
                "cards": cards,
            }
        )
    return reports
