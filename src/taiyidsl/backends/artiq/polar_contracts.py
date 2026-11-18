"""Static POWASF plan inspection and integer representation contracts."""

from __future__ import annotations

_KINDS = (
    "dds_polar_select",
    "dds_polar_retrigger",
    "dds_polar_commit_off",
    "dds_nco_epoch",
    "dds_nco_epoch_off",
    "dds_on",
    "dds_off",
    "ttl_on",
    "ttl_off",
    "dds_prepare",
    "dds_commit",
    "dds_commit_off",
    "dds_ram_phase",
    "dds_ram_start",
    "dds_ram_commit_off",
)


def uses_polar(compiled):
    return any(
        e.kind.startswith("dds_polar_")
        for p in compiled.points
        for e in p.physical_events
    )


def _parameters(point):
    result = {}
    for event in point.physical_events:
        if "polar_bank" in event.params:
            result.setdefault(event.signal, event.params)
    return result


def polar_bank_summary(compiled):
    result = []
    for point in compiled.points:
        for name, params in sorted(_parameters(point).items()):
            calls = [
                e
                for e in point.physical_events
                if e.signal == name and e.kind == "dds_polar_effect"
            ]
            bank = params["polar_bank"]
            result.append(
                {
                    "point": point.index,
                    "device": name,
                    "destination": "powasf",
                    "plays": sum(not e.params["polar_frame_only"] for e in calls),
                    "frame_only_updates": sum(
                        e.params["polar_frame_only"] for e in calls
                    ),
                    "resident_words": sum(len(p["words"]) for p in bank),
                    "profiles": [
                        {k: p[k] for k in ("profile", "start", "end", "step", "mode")}
                        for p in bank
                    ],
                    "phase_policy": "continuous",
                    "hardware": "not_verified",
                }
            )
    return result


def _signed(word):
    value = int(word) & 0xFFFFFFFF
    return value if value < 2**31 else value - 2**32
