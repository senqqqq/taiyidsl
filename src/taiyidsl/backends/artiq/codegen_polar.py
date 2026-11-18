"""POWASF and mixed-mode source generation; no verification drivers."""

from __future__ import annotations

import ast

from taiyidsl.backends.artiq.codegen_ram import ram_preload_lines
from taiyidsl.backends.artiq.codegen_square import square_preload_lines
from taiyidsl.backends.artiq.polar_contracts import _KINDS, _parameters, _signed
from taiyidsl.backends.artiq.submission import (
    validate_submission_plan,
)
from taiyidsl.compiler.chunking import host_loop_lines
from taiyidsl.execution import device_execution_plan
from taiyidsl.host_plan import execution_plan_for


def _call(kind, attr, profile="0", *, ftw="0", pow_="0", asf="0"):
    dds = f"self.{attr}"
    if kind == "dds_polar_select":
        return f"{dds}.cpld.set_profile({dds}.chip_select - 4, {profile})"
    if kind == "dds_prepare":
        return f"{dds}.write64(0x15, ({asf} << 16) | {pow_}, {ftw})"
    if kind == "dds_ram_phase":
        return f"{dds}.set_pow({pow_})"
    if kind in {"dds_polar_retrigger", "dds_nco_epoch", "dds_commit", "dds_ram_start"}:
        return f"{dds}.io_update.on()"
    if kind in {
        "dds_polar_commit_off",
        "dds_nco_epoch_off",
        "dds_commit_off",
        "dds_ram_commit_off",
    }:
        return f"{dds}.io_update.off()"
    switch = dds + (".sw" if kind.startswith("dds_") else "")
    return f"{switch}.{'on' if kind.endswith('_on') else 'off'}()"


def _instruction(event, attr, *, table=False, indent="        "):
    """Emit one real driver call at its certified absolute timestamp."""
    p = event.params
    if table:
        start, duration = "events[event_index, 0]", "events[event_index, 4]"
        profile, ftw, pow_, asf = (
            f"int32(events[event_index, {i}])" for i in (3, 5, 6, 7)
        )
    else:
        start, duration = event.start_mu, event.duration_mu
        profile = p.get("polar_profile", 0)
        ftw = _signed(p.get("ftw", 0))
        pow_ = p.get("square_pow", p.get("ram_pow", 0))
        asf = p.get("asf", 0)
    lines = [
        f"{indent}at_mu(t0 + {start})",
        f"{indent}"
        + _call(
            event.kind, attr, str(profile), ftw=str(ftw), pow_=str(pow_), asf=str(asf)
        ),
    ]
    if event.kind in {"dds_prepare", "dds_ram_phase", "dds_polar_select"}:
        lines += [
            f"{indent}if now_mu() > t0 + {start} + {duration}:",
            f"{indent}    raise ValueError('DDS driver exceeded "
            "declared preparation budget')",
        ]
    return lines


def generate_polar(compiled, mode):
    from taiyidsl.backends.artiq._contracts import (
        ExportError,
        _devices,
        _origin_grid,
        _point_end,
    )

    if mode not in {"auto", "unrolled"}:
        raise ValueError("Export mode must be 'auto' or 'unrolled'")
    compiled.assert_integrity()
    descriptor = compiled.provenance.get("backend")
    if descriptor is not None and descriptor["name"] != "artiq":
        raise ExportError("ARTIQ exporter requires an ARTIQ-compiled plan")
    if [p.index for p in compiled.points] != list(range(len(compiled.points))):
        raise ExportError("Compiled point indices must be contiguous starting at zero")
    devices = _devices(compiled)
    known_protocols = {
        (8 if device.ram_profile_control == "urukul_rev8" else 9)
        if device.ram_destination == "powasf"
        else device.urukul_proto_rev
        for device in devices.values()
        if device.kind == "dds"
    } - {None}
    if len(known_protocols) > 1:
        raise ExportError(
            "Current ARTIQ 9 drivers cannot compile mixed CPLD protocol revisions "
            "in one generated experiment; hardware was not retargeted"
        )
    names = sorted(devices)
    attrs = {name: f"device_{i}" for i, name in enumerate(names)}
    execution = device_execution_plan(compiled)
    grid = _origin_grid(devices, compiled.setup.rtio_coarse_mu)
    for point in compiled.points:
        if any(
            e.kind not in (*_KINDS, "dds_polar_effect", "dds_effect", "dds_ram_effect")
            for e in point.physical_events
        ):
            raise ExportError(
                "Mixed POWASF export requires timed Square or continuous resident "
                "ASF RAM; opaque legacy DDS calls are unsupported"
            )
        if any(
            devices[e.signal].kind == "dds"
            and devices[e.signal].nco_mode != "continuous"
            for e in point.physical_events
        ):
            raise ExportError(
                "Mixed POWASF export requires continuous NCO on every active DDS"
            )
        validate_submission_plan(point.physical_events, compiled.setup)
    lines = [
        '"""POWASF resident banks. Offline contracts only; hardware NOT VERIFIED."""',
        "from artiq.experiment import EnvExperiment, kernel, at_mu, now_mu",
        "from artiq.coredevice.ad9910 import "
        "RAM_DEST_POWASF, RAM_DEST_ASF, RAM_MODE_RAMPUP",
        "from artiq.coredevice.ttl import TTLOut",
        "from numpy import array, int32, int64",
        "from inspect import signature",
        "",
        "class GeneratedExperiment(EnvExperiment):",
        "    def build(self):",
        "        self.core = self.get_device('core')",
    ]
    for name in names:
        lines.append(f"        self.{attrs[name]} = self.get_device({name!r})")
    lines += [
        "        self.result_adapter = None",
        "        self.prepared_shared_resources = ()",
    ]
    for point in compiled.points:
        for name, params in _parameters(point).items():
            for bank in params["polar_bank"]:
                key = f"bank_{point.index}_{attrs[name]}_{bank['profile']}"
                words = [_signed(w) for w in bank["words"]]
                lines.append(f"        self.{key} = array({words!r}, dtype=int32)")
        if mode == "auto":
            rows = [
                [
                    int(e.start_mu),
                    _KINDS.index(e.kind),
                    names.index(e.signal),
                    int(e.params.get("polar_profile", 0)),
                    int(e.duration_mu),
                    _signed(e.params.get("ftw", 0)),
                    int(e.params.get("square_pow", e.params.get("ram_pow", 0))),
                    int(e.params.get("asf", 0)),
                ]
                for e in point.physical_events
                if e.kind in _KINDS
            ]
            lines.append(
                "        # Timed rows: start_mu, opcode, device, profile, "
                "budget, FTW, POW, ASF."
            )
            lines.append(
                f"        self.events_{point.index} = array({rows!r}, dtype=int64)"
            )
    lines += [
        "",
        "    @kernel",
        "    def initialize_devices(self):",
        "        self.core.reset()",
    ]
    for name in names:
        if name in execution["initialize_dds"]:
            lines.append(f"        self.{attrs[name]}.init()")
        lines += [
            "        self.core.break_realtime()",
            "        "
            + _call(
                "dds_off"
                if devices[name].kind == "dds"
                else "ttl_on"
                if getattr(devices[name], "ttl_idle_level", 0)
                else "ttl_off",
                attrs[name],
            ),
        ]
    lines += ["        self.core.wait_until_mu(now_mu())", ""]
    for point in compiled.points:
        lines += [
            "    @kernel",
            f"    def point_{point.index}(self):",
            "        self.core.reset()",
        ]
        params_by_name = _parameters(point)
        for name, params in sorted(params_by_name.items()):
            dds = f"self.{attrs[name]}"
            lines += [
                "        # RF-off preload: hold NCO clear through every bank update.",
                f"        {dds}.sw.off()",
                f"        {dds}.set_cfr1(ram_enable=0, "
                "phase_clear=1, phase_autoclear=0)",
                f"        {dds}.set_cfr2(asf_profile_enable=0, "
                "matched_latency_enable=1)",
                f"        {dds}.io_update.pulse_mu(8)",
            ]
            for bank in params["polar_bank"]:
                key = f"bank_{point.index}_{attrs[name]}_{bank['profile']}"
                lines += [
                    f"        {dds}.set_profile_ram({bank['start']}, {bank['end']}, "
                    f"step={bank['step']}, profile={bank['profile']}, "
                    f"mode={bank['mode']})",
                    f"        {dds}.io_update.pulse_mu(8)",
                    f"        {dds}.cpld.set_profile({dds}.chip_select - 4, "
                    f"{bank['profile']})",
                    f"        {dds}.write_ram([int32(x) for x in self.{key}])",
                    "        self.core.break_realtime()",
                ]
            lines += [
                f"        {dds}.set_ftw({_signed(params['ftw'])})",
                f"        {dds}.cpld.set_profile({dds}.chip_select - 4, "
                f"{params['polar_initial_profile']})",
                f"        {dds}.set_cfr1(ram_enable=1, "
                "ram_destination=RAM_DEST_POWASF, phase_clear=1, phase_autoclear=0)",
                f"        {dds}.io_update.pulse_mu(8)",
            ]
        from taiyidsl.backends.artiq._contracts import _words

        lines += ram_preload_lines(point, attrs, devices, _words)
        lines += square_preload_lines(
            {e.signal for e in point.physical_events if e.kind == "dds_prepare"},
            attrs,
            point,
            devices,
        )
        # All DDS modes are established before staging any NCO release.
        active_ram = {
            e.signal for e in point.physical_events if e.kind == "dds_ram_start"
        }
        epoch_names = {
            name
            for e in point.physical_events
            if e.kind == "dds_nco_epoch"
            for name in e.params["commit_members"]
        }
        for name in sorted(epoch_names):
            mode_setting = (
                "ram_enable=1, ram_destination=RAM_DEST_POWASF"
                if name in params_by_name
                else "ram_enable=1, ram_destination=RAM_DEST_ASF"
                if name in active_ram
                else "ram_enable=0"
            )
            lines += [
                f"        self.{attrs[name]}.set_cfr1({mode_setting}, "
                "phase_clear=0, phase_autoclear=0)"
            ]
        lines += [
            "        self.core.break_realtime()",
            f"        t0 = ((now_mu() + {grid - 1}) // {grid}) * {grid}",
            "        at_mu(t0)",
        ]
        if mode == "auto" and any(e.kind in _KINDS for e in point.physical_events):
            lines += [
                f"        events = self.events_{point.index}",
                "        for event_index in range(len(events)):",
            ]
            used = {
                (e.kind, e.signal) for e in point.physical_events if e.kind in _KINDS
            }
            for i, (kind, name) in enumerate(sorted(used)):
                prefix = "if" if i == 0 else "elif"
                lines += [
                    f"            {prefix} events[event_index, 1] "
                    f"== {_KINDS.index(kind)} "
                    f"and events[event_index, 2] == {names.index(name)}:",
                ]
                event = next(
                    e
                    for e in point.physical_events
                    if e.kind == kind and e.signal == name
                )
                lines += _instruction(
                    event, attrs[name], table=True, indent="                "
                )
            lines += [
                "            else:",
                "                raise ValueError('Unknown POWASF instruction')",
            ]
        else:
            for event in point.physical_events:
                if event.kind not in _KINDS:
                    continue
                lines += _instruction(event, attrs[event.signal])
        lines += [
            f"        at_mu(t0 + {_point_end(point)})",
            "        self.core.wait_until_mu(now_mu())",
            "",
        ]
    lines += _target_lines(compiled, devices, attrs, execution)
    lines += [
        "",
        "    def _execute_point(self, point):",
        "        getattr(self, 'point_' + str(point))()",
        "",
        "    def _run_shot(self, point, shot):",
        "        self.result_adapter.before_shot(point, shot)",
        "        self._execute_point(point)",
        "        self.result_adapter.after_shot(point, shot)",
        "",
        "    def run(self):",
        "        if self.result_adapter is None:",
        "            raise RuntimeError('A configured result_adapter is required')",
        "        self._validate_target()",
        "        self.initialize_devices()",
        "        self._validate_target()",
    ]
    lines += host_loop_lines(execution_plan_for(compiled))
    lines += ["        self.result_adapter.finish()", ""]
    source = "\n".join(lines)
    ast.parse(source)
    return source, {
        "mode": "unrolled",
        "requested_mode": mode,
        "ram_execution": "polar_event_table" if mode == "auto" else "polar_unrolled",
        "kernel_count": len(compiled.points),
        "source_lines": len(lines),
        "host_loops": "preserved",
        "groups": [],
        "ram_preparation": "RF-off resident POWASF bank; one NCO epoch per shot",
    }


def _target_lines(compiled, devices, attrs, execution):
    lines = [
        "    def _validate_target(self):",
        "        if abs(self.core.ref_period - "
        f"{float(compiled.setup.tick_seconds)!r}) > 1e-18 "
        f"or self.core.ref_multiplier != {compiled.setup.rtio_coarse_mu}:",
        "            raise ValueError('Core clock differs from compiled target')",
    ]
    for resource in execution["prepared_shared_resources_required"]:
        lines += [
            f"        if {resource!r} not in self.prepared_shared_resources:",
            "            raise ValueError('Shared DDS resources require "
            "external preparation and exclusive ownership')",
        ]
    dds_names = [n for n, d in devices.items() if d.kind == "dds"]
    ram_names = {
        e.signal
        for p in compiled.points
        for e in p.physical_events
        if e.kind in {"dds_ram_start", "dds_polar_select", "dds_polar_retrigger"}
    }
    for name in dds_names:
        device, attr = devices[name], f"self.{attrs[name]}"
        revision = 8 if device.ram_profile_control == "urukul_rev8" else 9
        lines += [
            f"        if abs({attr}.sysclk - {device.sysclk_hz!r}) > 1e-3 "
            f"or {attr}.bus.ref_period_mu != {device.spi_ref_period_mu}:",
            "            raise ValueError('DDS/SPI clock differs "
            "from compiled target')",
            f"        if not isinstance({attr}.io_update, TTLOut) "
            f"or {attr}.sync_data.io_update_delay != 0:",
            "            raise ValueError('POWASF requires direct TTL IO_UPDATE "
            "with zero driver delay')",
            f"        if tuple(signature({attr}.cpld.set_profile).parameters) "
            f"!= ('channel', 'profile') or not 4 <= {attr}.chip_select <= 7:",
            "            raise ValueError('POWASF requires "
            "ARTIQ 9 channel/profile API')",
        ]
        if device.ram_destination == "powasf":
            lines += [
                f"        if {attr}.cpld.proto_rev != {revision}:",
                "            raise ValueError('CPLD revision differs "
                "from certified selector duration')",
            ]
        elif device.urukul_proto_rev is not None:
            lines += [
                f"        if {attr}.cpld.proto_rev != {device.urukul_proto_rev}:",
                "            raise ValueError('CPLD protocol differs from "
                "the declared hardware')",
            ]
        kinds = {
            e.kind
            for p in compiled.points
            for e in p.physical_events
            if e.signal == name
        }
        for kind, budget, cycles in (
            ("dds_prepare", device.square_prepare_duration_mu, 156),
            ("dds_ram_phase", device.ram_phase_update_duration_mu, 52),
        ):
            if kind in kinds:
                lines += [
                    f"        if {budget} < {cycles} * {attr}.bus.ref_period_mu:",
                    "            raise ValueError('DDS preparation budget is below "
                    "the real SPI cursor minimum')",
                ]
    for i, name in enumerate(dds_names):
        for other in dds_names[i + 1 :]:
            a, b = f"self.{attrs[name]}", f"self.{attrs[other]}"
            first, second = devices[name], devices[other]
            lines += [
                f"        if {a}.cpld.proto_rev != {b}.cpld.proto_rev:",
                "            raise ValueError('Current ARTIQ 9 drivers cannot "
                "compile mixed CPLD protocol revisions in one experiment')",
            ]
            if first.bus is not None and second.bus is not None:
                relation = "!=" if first.bus == second.bus else "=="
                lines += [
                    f"        if {a}.bus.channel {relation} {b}.bus.channel:",
                    "            raise ValueError('DDS SPI wiring differs "
                    "from compiled bus groups')",
                ]
            relation = (
                "!="
                if first.update_group and first.update_group == second.update_group
                else "=="
            )
            lines += [
                f"        if {a}.io_update.target_o {relation} {b}.io_update.target_o:",
                "            raise ValueError('DDS IO_UPDATE wiring differs "
                "from compiled groups')",
                f"        if {a}.bus.channel == {b}.bus.channel "
                f"and {a}.chip_select == {b}.chip_select:",
                "            raise ValueError('DDS chip selects alias')",
            ]
            if name in ram_names or other in ram_names:
                lines += [
                    f"        if {a}.bus.channel == {b}.bus.channel "
                    f"and {a}.cpld is not {b}.cpld:",
                    "            raise ValueError('RAM SPI aliases distinct "
                    "CPLD configuration shadows')",
                    f"        if {a}.bus.channel == {b}.bus.channel "
                    f"and ({a}.cpld.proto_rev == 8 or {b}.cpld.proto_rev == 8):",
                    "            raise ValueError('Rev8 shared profile pins "
                    "interfere with another active DDS')",
                ]
    outputs = [
        f"self.{attrs[name]}" + (".sw" if device.kind == "dds" else "")
        for name, device in devices.items()
    ]
    for index, output in enumerate(outputs):
        for other in outputs[index + 1 :]:
            lines += [
                f"        if {output}.target_o == {other}.target_o:",
                "            raise ValueError('RF/TTL output targets alias')",
            ]
        for name in dds_names:
            lines += [
                f"        if {output}.target_o == "
                f"self.{attrs[name]}.io_update.target_o:",
                "            raise ValueError('IO_UPDATE aliases an RF/TTL output')",
            ]
    return lines
