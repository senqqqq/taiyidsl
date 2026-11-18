"""Square/NCO preload source fragments."""

from __future__ import annotations


def square_preload_lines(names, attributes, point=None, devices=None):
    """Establish single-tone mode before the shot origin, with RF gated off.

    Persistent phase-autoclear requires every participating member of a shared
    IO_UPDATE group to be staged on every commit. The compiler owns that rule.
    """
    lines = []
    for name in sorted(names):
        attr = attributes[name]
        continuous = devices is not None and devices[name].nco_mode == "continuous"
        cfr = "phase_clear=1, phase_autoclear=0" if continuous else "phase_autoclear=1"
        lines += [
            "        # Establish buffered single-tone mode before the timed origin.",
            "        self.core.break_realtime()",
            f"        self.{attr}.sw.off()",
            f"        self.{attr}.set_cfr1(ram_enable=0, {cfr})",
            f"        self.{attr}.set_cfr2(asf_profile_enable=1, "
            "matched_latency_enable=1)",
            f"        self.{attr}.io_update.pulse_mu(8)",
            f"        self.{attr}.cpld.set_profile(self.{attr}.chip_select - 4, 7)",
            f"        self.{attr}.io_update.pulse_mu(8)",
            "        self.core.break_realtime()",
        ]
        if continuous:
            first = next(
                e
                for e in point.physical_events
                if e.signal == name and e.kind == "dds_prepare"
            )
            p = first.params
            lines += [
                f"        self.{attr}.write64(0x15, ({p['asf']} << 16) | "
                f"{p['nco_initial_pow']}, {p['ftw']})",
                f"        self.{attr}.io_update.pulse_mu(8)",
            ]
    return lines


def nco_release_stage_lines(point, attributes, devices):
    """Stage release only after every member's preload commits are complete."""
    active_ram = {e.signal for e in point.physical_events if e.kind == "dds_ram_start"}
    names = {
        name
        for e in point.physical_events
        if e.kind == "dds_nco_epoch"
        for name in e.params["commit_members"]
    }
    lines = []
    for name in sorted(names):
        attr = attributes[name]
        mode = (
            "ram_enable=1, ram_destination=RAM_DEST_ASF"
            if name in active_ram
            else "ram_enable=0"
        )
        lines += [
            f"        self.{attr}.set_cfr1({mode}, phase_clear=0, phase_autoclear=0)",
            "        self.core.break_realtime()",
        ]
    return lines
