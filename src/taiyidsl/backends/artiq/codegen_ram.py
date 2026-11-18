"""Source-generation helpers for resident AD9910 RAM waveform banks.

Waveforms occupy DDS RAM once per point preparation. The event table stores
instructions in core memory, not samples in DDS RAM; its length can therefore
grow independently of the waveform bank's 1024-word capacity.
"""

_KINDS = (
    "dds_config",
    "dds_ram_rearm",
    "dds_ram_start",
    "dds_on",
    "dds_off",
    "ttl_on",
    "ttl_off",
    "dds_ram_phase",
    "dds_ram_commit_off",
    "dds_nco_epoch",
    "dds_nco_epoch_off",
)


def ram_bank_summary(compiled):
    """Expose memory occupancy and replay contracts separately from gate count."""
    result = []
    for point in compiled.points:
        banks = {}
        for event in point.physical_events:
            if event.kind == "dds_ram_start":
                banks.setdefault(event.signal, []).append(event.params)
        for name, calls in sorted(banks.items()):
            profiles = {p["ram_profile"]: p for p in calls}
            result.append(
                {
                    "point": point.index,
                    "device": name,
                    "plays": sum(not p.get("ram_frame_only") for p in calls),
                    "frame_only_updates": sum(
                        bool(p.get("ram_frame_only")) for p in calls
                    ),
                    "launches": len(calls),
                    "resident_words": sum(len(p["ram_asf"]) for p in profiles.values()),
                    "profiles": [
                        {
                            "profile": i,
                            "start": p["ram_start"],
                            "end": p["ram_end"],
                            "step": p["ram_step"],
                        }
                        for i, p in sorted(profiles.items())
                    ],
                    "rearm_budget_mu": compiled.setup.devices[
                        name
                    ].ram_rearm_duration_mu,
                    "ram_latency_mu": calls[0]["ram_latency_mu"],
                    "min_gap_mu": (
                        max(
                            (p.get("ram_min_gap_mu", 0) for p in calls[1:] or calls),
                            default=0,
                        )
                        if calls[0].get("ram_boundary")
                        else max(
                            (p.get("ram_min_gap_mu", 0) for p in calls[1:] or calls),
                            default=0,
                        )
                        or None
                    ),
                    "matched_latency_enable": calls[0]["ram_matched_latency"],
                    "phase_update_budget_mu": compiled.setup.devices[
                        name
                    ].ram_phase_update_duration_mu,
                    "phase_policy": compiled.setup.devices[name].nco_mode,
                    "phase_latency_mu": (
                        compiled.setup.devices[name].ram_pow_latency_mu
                        if compiled.setup.devices[name].nco_mode == "continuous"
                        else compiled.setup.devices[name].ram_phase_latency_mu
                    ),
                    "boundary_replay": bool(calls[0].get("ram_boundary")),
                    "replay_actions": {
                        action: sum(p.get("ram_replay_action") == action for p in calls)
                        for action in ("first", "rearm", "phase", "retrigger")
                    },
                    "min_gap_by_action_mu": {
                        p["ram_replay_action"]: p["ram_min_gap_mu"]
                        for p in calls
                        if p.get("ram_replay_action") in {"rearm", "phase", "retrigger"}
                    },
                }
            )
    return result


def ram_rearm_lines(attr, start, *, ftw, pow_, profile, duration, indent="        "):
    """Select a resident waveform while RF is off; leave launch to its event."""
    return [
        f"{indent}at_mu(t0 + {start})",
        f"{indent}self.{attr}.set_cfr1(ram_enable=0)",
        f"{indent}self.{attr}.io_update.pulse_mu(8)",
        f"{indent}self.{attr}.cpld.set_profile(self.{attr}.chip_select - 4, {profile})",
        f"{indent}self.{attr}.set_ftw({ftw})",
        f"{indent}self.{attr}.set_pow({pow_})",
        # Profile changes can update registers/start RAM. Commit phase/frequency
        # explicitly while RAM remains disabled before staging its next enable.
        f"{indent}self.{attr}.io_update.pulse_mu(8)",
        f"{indent}self.{attr}.set_cfr1(ram_enable=1, "
        "ram_destination=RAM_DEST_ASF, phase_autoclear=1)",
        f"{indent}if now_mu() > t0 + {start} + {duration}:",
        f"{indent}    raise ValueError('RAM rearm driver exceeded "
        "declared configuration budget')",
    ]


def ram_preload_lines(point, attributes, devices, words):
    """Load each allocated profile once and stage each DDS's first launch."""
    launches = {}
    for event in sorted(point.physical_events, key=lambda event: event.start_mu):
        if event.kind in {"dds_effect", "dds_ram_effect"}:
            continue
        if event.kind == "dds_ram_start":
            launches.setdefault(event.signal, []).append(event)
    lines = []
    for name, events in sorted(launches.items()):
        attr = attributes[name]
        continuous = devices[name].nco_mode == "continuous"
        hold = ", phase_clear=1, phase_autoclear=0" if continuous else ""
        lines += [
            "        # Preload resident RAM bank before the timed shot, with RF off.",
            "        self.core.break_realtime()",
            f"        self.{attr}.sw.off()",
            f"        self.{attr}.set_cfr1(ram_enable=0{hold})",
            f"        self.{attr}.set_asf(0)",
            f"        self.{attr}.set_cfr2(asf_profile_enable=0, "
            f"matched_latency_enable={events[0].params.get('ram_matched_latency', 0)})",
            f"        self.{attr}.io_update.pulse_mu(8)",
        ]
        profiles = {}
        for event in events:
            profiles.setdefault(event.params["ram_profile"], event.params)
        for profile, params in sorted(profiles.items()):
            ram_words = [int(asf) << 18 for asf in params["ram_asf"]]
            ram_words = [word if word < 2**31 else word - 2**32 for word in ram_words]
            lines += [
                f"        self.{attr}.cpld.set_profile("
                f"self.{attr}.chip_select - 4, {profile})",
                f"        self.{attr}.set_profile_ram("
                f"{params['ram_start']}, {params['ram_end']}, "
                f"step={params['ram_step']}, profile={profile}, mode=RAM_MODE_RAMPUP)",
                f"        self.{attr}.io_update.pulse_mu(8)",
                "        self.core.break_realtime()",
                f"        self.{attr}.write_ram([int32(x) for x in {ram_words!r}])",
                "        self.core.break_realtime()",
            ]
        first = events[0]
        ftw, _, _ = words(first, devices[name])
        initial_pow = (
            first.params["nco_initial_pow"] if continuous else first.params["ram_pow"]
        )
        lines += [
            f"        self.{attr}.cpld.set_profile("
            f"self.{attr}.chip_select - 4, {first.params['ram_profile']})",
            f"        self.{attr}.set_ftw({ftw})",
            f"        self.{attr}.set_pow({initial_pow})",
            f"        self.{attr}.io_update.pulse_mu(8)",
            f"        self.{attr}.set_cfr1(ram_enable=1, "
            + (
                "ram_destination=RAM_DEST_ASF, phase_clear=1, phase_autoclear=0)"
                if continuous
                else "ram_destination=RAM_DEST_ASF, phase_autoclear=1)"
            ),
            "        self.core.break_realtime()",
        ]
        if continuous:
            # Commit final RAM mode while the accumulator remains held clear.
            lines += [f"        self.{attr}.io_update.pulse_mu(8)"]
    return lines


def ram_table_build_lines(point, attributes, devices, words):
    """Build a typed instruction matrix; no waveform samples appear in rows."""
    device_ids = {name: index for index, name in enumerate(sorted(attributes))}
    lines = [
        "        # device, opcode, start, duration, FTW, POW, ASF, reference, profile",
        f"        self._ram_events_{point.index} = array([",
    ]
    for event in sorted(point.physical_events, key=lambda event: event.start_mu):
        if event.kind in {"dds_effect", "dds_ram_effect"}:
            continue
        ftw = pow_ = asf = reference = profile = 0
        if event.kind == "dds_config":
            ftw, pow_, asf = words(event, devices[event.signal])
            reference = event.params.get("ref_time_mu", point.origin_mu)
        elif event.kind in {"dds_ram_rearm", "dds_ram_phase"}:
            ftw, _, _ = words(event, devices[event.signal])
            pow_ = event.params["ram_pow"]
            profile = event.params["ram_profile"]
        row = (
            device_ids[event.signal],
            _KINDS.index(event.kind),
            int(event.start_mu),
            int(event.duration_mu),
            ftw,
            pow_,
            asf,
            int(reference),
            profile,
        )
        lines.append(f"            {row!r},")
    lines.append("        ], dtype=int64)")
    return lines


def ram_table_body_lines(point, attributes, devices, event_lines):
    """Generate a bounded kernel loop over the point's scheduled instructions."""
    device_ids = {name: index for index, name in enumerate(sorted(attributes))}
    signatures = sorted(
        {
            (e.signal, e.kind)
            for e in point.physical_events
            if e.kind not in {"dds_effect", "dds_ram_effect"}
        }
    )
    if not signatures:
        return []
    lines = [
        f"        events = self._ram_events_{point.index}",
        "        for event_index in range(len(events)):",
        "            device_id = events[event_index, 0]",
        "            opcode = events[event_index, 1]",
    ]
    for index, (name, kind) in enumerate(signatures):
        branch = "if" if index == 0 else "elif"
        lines.append(
            f"            {branch} device_id == {device_ids[name]} "
            f"and opcode == {_KINDS.index(kind)}:"
        )
        kwargs = {}
        if kind == "dds_config":
            kwargs = {
                "words": tuple(f"int32(events[event_index, {i}])" for i in (4, 5, 6)),
                "reference": "events[event_index, 7]",
                "duration": int(devices[name].config_duration_mu),
            }
        elif kind in {"dds_ram_rearm", "dds_ram_phase"}:
            kwargs = {
                "words": ("int32(events[event_index, 4])", 0, 0),
                "ram_pow": "int32(events[event_index, 5])",
                "ram_profile": "int32(events[event_index, 8])",
                "duration": int(
                    devices[name].ram_phase_update_duration_mu
                    if kind == "dds_ram_phase"
                    else devices[name].ram_rearm_duration_mu
                ),
            }
        elif kind == "dds_ram_start":
            kwargs = {
                "ram_boundary": devices[name].nco_mode == "continuous"
                or getattr(devices[name], "ram_phase_latency_mu", None) is not None
            }
        lines += event_lines(
            kind,
            attributes[name],
            "events[event_index, 2]",
            indent="                ",
            **kwargs,
        )
    lines += [
        "            else:",
        "                raise ValueError('Unknown RAM sequence instruction')",
    ]
    return lines
