"""ARTIQ source generation from an already compiled plan; no mock execution."""

from __future__ import annotations

import ast

from taiyidsl.backends.artiq._contracts import (
    ExportError,
    _devices,
    _ordered_events,
    _origin_grid,
    _point_end,
    _words,
)
from taiyidsl.backends.artiq.codegen_ram import (
    ram_preload_lines,
    ram_rearm_lines,
    ram_table_body_lines,
    ram_table_build_lines,
)
from taiyidsl.backends.artiq.codegen_square import (
    nco_release_stage_lines,
    square_preload_lines,
)
from taiyidsl.backends.artiq.submission import (
    validate_submission_plan,
)
from taiyidsl.compiler.chunking import host_loop_lines
from taiyidsl.execution import device_execution_plan
from taiyidsl.host_plan import execution_plan_for
from taiyidsl.ir import CompileError


def _validate_plan(compiled):
    if hasattr(compiled, "assert_integrity"):
        compiled.assert_integrity()
    descriptor = getattr(compiled, "provenance", {}).get("backend")
    if descriptor is not None and descriptor["name"] != "artiq":
        raise ExportError("ARTIQ exporter requires an ARTIQ-compiled plan")
    devices = _devices(compiled)
    if [p.index for p in compiled.points] != list(range(len(compiled.points))):
        raise ExportError("Compiled point indices must be contiguous starting at zero")
    allowed = {
        "dds_nco_epoch",
        "dds_nco_epoch_off",
        "dds_config",
        "dds_prepare",
        "dds_commit",
        "dds_commit_off",
        "dds_effect",
        "dds_ram_rearm",
        "dds_ram_phase",
        "dds_ram_start",
        "dds_ram_commit_off",
        "dds_ram_effect",
        "dds_on",
        "dds_off",
        "ttl_on",
        "ttl_off",
    }
    for point in compiled.points:
        try:
            validate_submission_plan(_ordered_events(point), compiled.setup)
        except CompileError as exc:
            raise ExportError(str(exc)) from exc
        cursor = 0
        high_updates = {}
        ram_falls = {}
        for event in _ordered_events(point):
            if event.kind not in allowed:
                raise ExportError(f"Unsupported physical event: {event.kind}")
            if event.signal not in devices:
                raise ExportError(f"Unknown device: {event.signal}")
            if event.kind in {"dds_effect", "dds_ram_effect"}:
                # An effect is a modeled output change, not a CPU submission.
                # It may occur while the CPU is preparing a later state.
                continue
            if int(event.start_mu) != event.start_mu or event.start_mu < 0:
                raise ExportError(
                    f"Point {point.index}: event {event.kind} at {event.start_mu} mu "
                    "has an invalid scheduled timestamp"
                )
            cursor = int(event.start_mu)
            device = devices[event.signal]
            if event.kind in {
                "dds_config",
                "dds_prepare",
                "dds_ram_rearm",
                "dds_ram_phase",
            }:
                expected_duration = int(
                    device.square_prepare_duration_mu
                    if event.kind == "dds_prepare"
                    else device.ram_phase_update_duration_mu
                    if event.kind == "dds_ram_phase"
                    else device.ram_rearm_duration_mu
                    if event.kind == "dds_ram_rearm"
                    else device.config_duration_mu
                )
                if event.duration_mu != expected_duration:
                    raise ExportError(
                        "DDS event duration differs from the device driver contract"
                    )
                _words(event, device)
                cursor += expected_duration
            elif event.kind in {"dds_commit", "dds_nco_epoch"}:
                if event.duration_mu or event.signal in high_updates:
                    raise ExportError("Square commit requires one nonblocking rise")
                high_updates[event.signal] = event.start_mu
            elif event.kind in {"dds_commit_off", "dds_nco_epoch_off"}:
                if (
                    event.duration_mu
                    or high_updates.pop(event.signal, None) != event.start_mu - 8
                ):
                    raise ExportError("Square commit requires a paired 8-mu fall")
            elif event.kind == "dds_ram_start":
                if event.params.get("ram_boundary"):
                    if event.duration_mu or event.signal in high_updates:
                        raise ExportError("RAM commit requires one nonblocking rise")
                    previous_fall = ram_falls.get(event.signal)
                    if previous_fall is not None and event.start_mu < (
                        previous_fall + getattr(compiled.setup, "rtio_coarse_mu", 8)
                    ):
                        raise ExportError("RAM commit requires a full coarse-cycle low")
                    high_updates[event.signal] = event.start_mu
                elif event.duration_mu != 8:
                    raise ExportError("RAM launch requires an 8-mu IO_UPDATE pulse")
                else:
                    cursor += 8
            elif event.kind == "dds_ram_commit_off":
                if (
                    event.duration_mu
                    or high_updates.pop(event.signal, None) != event.start_mu - 8
                ):
                    raise ExportError("RAM commit requires a paired 8-mu fall")
                ram_falls[event.signal] = event.start_mu
        if high_updates:
            raise ExportError("Commit is missing its IO_UPDATE falling edge")
        end = max(int(point.duration_mu) + int(point.origin_mu), cursor)
        if end < 0:
            raise ExportError("Negative shot end")


def _alignment(signature, template):
    """Map an ordered signature onto a supersequence without changing event order."""
    positions, cursor = [], 0
    for item in signature:
        while cursor < len(template) and template[cursor] != item:
            cursor += 1
        if cursor == len(template):
            return None
        positions.append(cursor)
        cursor += 1
    return positions


def _program_groups(compiled):
    """Group only by compiled structure, never by experiment/template name.

    Descending-size representatives make zero-pulse/zero-delay points optional
    subsequences of the normal program. Different event order/device sequences
    stay in separate groups. No RTIO action is inferred, merged or rescheduled.
    """
    groups = []
    for point in sorted(
        compiled.points, key=lambda p: (-len(p.physical_events), p.index)
    ):
        events = _ordered_events(point)
        signature = tuple((e.kind, e.signal) for e in events)
        for group in groups:
            positions = _alignment(signature, group["signature"])
            if positions is not None:
                break
        else:
            group = {
                "signature": signature,
                "points": [],
                "slots": {},
                "columns": [],
                "rows": {},
            }
            groups.append(group)
            positions = list(range(len(signature)))
        group["points"].append(point)
        group["slots"][point.index] = dict(zip(positions, events))
    groups.sort(key=lambda g: min(p.index for p in g["points"]))
    for index, group in enumerate(groups):
        group["index"] = index
        group["points"].sort(key=lambda p: p.index)
    return groups


def _kernel_header(method, origin_grid, parameterized=False):
    signature = (
        f"{method}(self, values: TArray(TInt64, 1))"
        if parameterized
        else f"{method}(self)"
    )
    return [
        "    @kernel",
        f"    def {signature}:",
        "        self.core.reset()",
        f"        t0 = ((now_mu() + {origin_grid - 1}) // {origin_grid}) "
        f"* {origin_grid}",
        "        at_mu(t0)",
    ]


def _event_lines(
    kind,
    attr,
    start,
    *,
    words=None,
    reference=None,
    duration=None,
    ram_pow=None,
    ram_profile=None,
    ram_boundary=False,
    square_pow=None,
    indent="        ",
):
    if kind in {"dds_effect", "dds_ram_effect"}:
        return []
    if kind == "dds_ram_rearm":
        return ram_rearm_lines(
            attr,
            start,
            ftw=words[0],
            pow_=ram_pow,
            profile=ram_profile,
            duration=duration,
            indent=indent,
        )
    lines = [f"{indent}at_mu(t0 + {start})"]
    if kind == "dds_prepare":
        ftw, _, asf = words
        lines += [
            f"{indent}self.{attr}.write64(0x15, ({asf} << 16) | {square_pow}, {ftw})",
            f"{indent}if now_mu() > t0 + {start} + {duration}:",
            f"{indent}    raise ValueError('Square prepare driver exceeded "
            "declared budget')",
        ]
    elif kind in {"dds_commit", "dds_nco_epoch"}:
        lines.append(f"{indent}self.{attr}.io_update.on()")
    elif kind in {"dds_commit_off", "dds_nco_epoch_off"}:
        lines.append(f"{indent}self.{attr}.io_update.off()")
    elif kind == "dds_config":
        ftw, pow_, asf = words
        lines += [
            f"{indent}self.{attr}.set_mu({ftw}, pow_={pow_}, asf={asf}, "
            f"phase_mode=PHASE_MODE_TRACKING, ref_time_mu=t0 + {reference})",
            f"{indent}if now_mu() > t0 + {start} + {duration}:",
            f"{indent}    raise ValueError('DDS driver exceeded "
            "declared configuration budget')",
        ]
    elif kind == "dds_ram_start":
        call = "on()" if ram_boundary else "pulse_mu(8)"
        lines.append(f"{indent}self.{attr}.io_update.{call}")
    elif kind == "dds_ram_commit_off":
        lines.append(f"{indent}self.{attr}.io_update.off()")
    elif kind == "dds_ram_phase":
        lines += [
            f"{indent}self.{attr}.set_pow({ram_pow})",
            f"{indent}if now_mu() > t0 + {start} + {duration}:",
            f"{indent}    raise ValueError('RAM phase driver exceeded "
            "declared configuration budget')",
        ]
    elif kind.startswith("dds_"):
        lines.append(f"{indent}self.{attr}.sw.{kind[4:]}()")
    else:
        lines.append(f"{indent}self.{attr}.{kind[4:]}()")
    return lines


def _parameterized_kernel(group, devices, attributes, origin_grid):
    vectors = {}
    declarations = []

    def expression(label, values, present=None, register=False):
        values = tuple(int(v) for v in values)
        selected = (
            [v for v, active in zip(values, present) if active]
            if present is not None
            else list(values)
        )
        if selected and len(set(selected)) == 1:
            return str(selected[0])
        if values not in vectors:
            column = len(vectors)
            vectors[values] = label
            group["columns"].append(label)
            declarations.append(f"        {label} = values[{column}]")
        name = vectors[values]
        return f"int32({name})" if register else name

    body = []
    points = group["points"]
    for slot, (kind, device_name) in enumerate(group["signature"]):
        events = [group["slots"][p.index].get(slot) for p in points]
        present = [e is not None for e in events]
        enabled = None
        if not all(present):
            enabled = expression(f"emit_{kind}_{slot}", present)
            body.append(f"        if {enabled} != 0:")
        indent = "            " if enabled is not None else "        "
        start = expression(
            f"{kind}_{slot}_mu", [e.start_mu if e else 0 for e in events], present
        )
        kwargs = {}
        if kind == "dds_config":
            device = devices[device_name]
            words = [_words(e, device) if e else (0, 0, 0) for e in events]
            kwargs["words"] = tuple(
                expression(
                    f"{name}_{slot}", [w[i] for w in words], present, register=True
                )
                for i, name in enumerate(("ftw", "pow", "asf"))
            )
            kwargs["reference"] = expression(
                f"phase_reference_{slot}_mu",
                [
                    e.params.get("ref_time_mu", p.origin_mu) if e else 0
                    for p, e in zip(points, events)
                ],
                present,
            )
            kwargs["duration"] = str(int(device.config_duration_mu))
        body.append(f"{indent}# {kind}: {device_name}")
        body.extend(
            _event_lines(kind, attributes[device_name], start, indent=indent, **kwargs)
        )
    end = expression("shot_end_mu", [_point_end(p) for p in points])
    vectors_in_order = list(vectors)
    for row, point in enumerate(points):
        group["rows"][point.index] = [vector[row] for vector in vectors_in_order]
    lines = _kernel_header(f"kernel_{group['index']}", origin_grid, parameterized=True)
    lines += declarations + body
    lines += [
        f"        at_mu(t0 + {end})",
        "        self.core.wait_until_mu(now_mu())",
        "",
    ]
    return lines


def _generate(compiled, mode):
    from taiyidsl.backends.artiq.codegen_polar import generate_polar
    from taiyidsl.backends.artiq.polar_contracts import uses_polar

    if uses_polar(compiled):
        return generate_polar(compiled, mode)
    if mode not in {"auto", "unrolled"}:
        raise ValueError("Export mode must be 'auto' or 'unrolled'")
    _validate_plan(compiled)
    requested_mode = mode
    table_points = {
        p.index
        for p in compiled.points
        if mode == "auto"
        and sum(e.kind == "dds_ram_start" for e in p.physical_events) > 1
        and not any(e.kind == "dds_prepare" for e in p.physical_events)
    }
    ram_enabled = any(
        e.kind == "dds_ram_start" for p in compiled.points for e in p.physical_events
    )
    ram_devices = {
        e.signal
        for p in compiled.points
        for e in p.physical_events
        if e.kind == "dds_ram_start"
    }
    square_devices = {
        e.signal
        for p in compiled.points
        for e in p.physical_events
        if e.kind == "dds_prepare"
    }
    if ram_enabled or square_devices:
        # Variable-size RAM buffers use per-point kernels; the square fast path
        # retains its existing compact parameterization.
        mode = "unrolled"
    devices = _devices(compiled)
    execution = device_execution_plan(compiled)
    origin_grid = _origin_grid(devices, getattr(compiled.setup, "rtio_coarse_mu", 8))
    names = sorted(devices)
    attributes = {name: f"device_{index}" for index, name in enumerate(names)}
    groups = _program_groups(compiled) if mode == "auto" else []
    kernel_lines = []
    for group in groups:
        kernel_lines.extend(
            _parameterized_kernel(group, devices, attributes, origin_grid)
        )
    lines = [
        '"""Generated for ARTIQ 9; real compilation and hardware are NOT VERIFIED.',
        "The timing contract, optical calibration and device_db must be validated",
        "before use. Set result_adapter before run(); "
        'no detector driver is assumed."""',
        "from artiq.experiment import EnvExperiment, kernel, at_mu, now_mu",
        "from artiq.coredevice.ad9910 import PHASE_MODE_TRACKING",
        "",
        "class GeneratedExperiment(EnvExperiment):",
        "    def build(self):",
        "        self.core = self.get_device('core')",
    ]
    if ram_enabled or square_devices:
        lines[6:6] = [
            "from artiq.coredevice.ad9910 import RAM_DEST_ASF, RAM_MODE_RAMPUP",
            "from artiq.coredevice.ttl import TTLOut",
            "from inspect import signature",
            "from numpy import array, int32, int64",
        ]
    if mode == "auto":
        lines[6:6] = [
            "from artiq.experiment import TArray, TInt64",
            "from numpy import array, int32, int64",
        ]
    for name in names:
        lines.append(f"        self.{attributes[name]} = self.get_device({name!r})")
    lines += [
        "        self.result_adapter = None",
        "        self.prepared_shared_resources = ()",
    ]
    for point in compiled.points:
        if point.index in table_points:
            lines += ram_table_build_lines(point, attributes, devices, _words)
    dispatch = {}
    if mode == "auto":
        rows = {}
        for group in groups:
            for point in group["points"]:
                dispatch[point.index] = group["index"]
            rows.update(group["rows"])
        lines.append(
            "        self._point_groups = "
            f"{tuple(dispatch[p.index] for p in compiled.points)!r}"
        )
        for group in groups:
            lines.append(
                f"        # kernel_{group['index']} values columns: "
                f"{', '.join(group['columns']) or '(all constants)'}"
            )
        lines.append("        self._point_values = [")
        for point in compiled.points:
            lines.append(
                f"            array({rows[point.index]!r}, dtype=int64),  "
                f"# point {point.index}"
            )
        lines.append("        ]")
    lines += [
        "",
        "    @kernel",
        "    def initialize_devices(self):",
        "        self.core.reset()",
    ]
    for name in names:
        attribute = attributes[name]
        if devices[name].kind == "dds":
            if name in execution["initialize_dds"]:
                lines += [f"        self.{attribute}.init()"]
            lines += [
                "        self.core.break_realtime()",
                f"        self.{attribute}.sw.off()",
            ]
        else:
            idle = "on" if getattr(devices[name], "ttl_idle_level", 0) else "off"
            lines.append(f"        self.{attribute}.{idle}()")
    lines += ["        self.core.wait_until_mu(now_mu())", ""]
    lines.extend(kernel_lines)
    for point in compiled.points if mode == "unrolled" else []:
        header = _kernel_header(f"point_{point.index}", origin_grid)
        preparations = []
        active_ram = {
            e.signal for e in point.physical_events if e.kind == "dds_ram_start"
        }
        for name in sorted(ram_devices - active_ram):
            attr = attributes[name]
            preparations += [
                "        # Restore single-tone profile after any earlier RAM shot.",
                "        self.core.break_realtime()",
                f"        self.{attr}.sw.off()",
                f"        self.{attr}.set_cfr1(ram_enable=0)",
                f"        self.{attr}.set_cfr2(asf_profile_enable=1, "
                "matched_latency_enable=0)",
                f"        self.{attr}.io_update.pulse_mu(8)",
                f"        self.{attr}.cpld.set_profile(self.{attr}.chip_select - 4, 7)",
                f"        self.{attr}.io_update.pulse_mu(8)",
                "        self.core.break_realtime()",
            ]
        preparations += ram_preload_lines(point, attributes, devices, _words)
        preparations += square_preload_lines(
            {e.signal for e in point.physical_events if e.kind == "dds_prepare"},
            attributes,
            point,
            devices,
        )
        preparations += nco_release_stage_lines(point, attributes, devices)
        # Insert after core.reset and before the shot origin is established.
        lines += header[:3] + preparations + header[3:]
        if point.index in table_points:
            lines += ram_table_body_lines(point, attributes, devices, _event_lines)
        else:
            for event in _ordered_events(point):
                kwargs = {}
                if event.kind == "dds_config":
                    kwargs = {
                        "words": _words(event, devices[event.signal]),
                        "reference": int(
                            event.params.get("ref_time_mu", point.origin_mu)
                        ),
                        "duration": int(event.duration_mu),
                    }
                elif event.kind == "dds_prepare":
                    kwargs = {
                        "words": _words(event, devices[event.signal]),
                        "square_pow": int(event.params["square_pow"]),
                        "duration": int(event.duration_mu),
                    }
                elif event.kind in {"dds_ram_rearm", "dds_ram_phase"}:
                    kwargs = {
                        "words": _words(event, devices[event.signal]),
                        "ram_pow": event.params["ram_pow"],
                        "ram_profile": event.params["ram_profile"],
                        "duration": int(event.duration_mu),
                    }
                elif event.kind == "dds_ram_start":
                    kwargs = {"ram_boundary": bool(event.params.get("ram_boundary"))}
                lines += _event_lines(
                    event.kind, attributes[event.signal], int(event.start_mu), **kwargs
                )
        end = _point_end(point)
        lines += [
            f"        at_mu(t0 + {end})",
            "        self.core.wait_until_mu(now_mu())",
            "",
        ]
    lines += [
        "    def _validate_target(self):",
        "        if abs(self.core.ref_period - "
        f"{float(compiled.setup.tick_seconds)!r}) > 1e-18:",
        "            raise ValueError('Core reference period "
        "differs from compiled target')",
        "        if self.core.ref_multiplier != "
        f"{getattr(compiled.setup, 'rtio_coarse_mu', 8)}:",
        "            raise ValueError('Core coarse period differs "
        "from compiled target')",
    ]
    for name in names:
        if devices[name].kind == "dds":
            expected_sysclk = float(getattr(devices[name], "sysclk_hz", 1e9))
            tolerance = max(1e-6, abs(expected_sysclk) * 1e-12)
            lines += [
                f"        if abs(self.{attributes[name]}.sysclk - "
                f"{expected_sysclk!r}) > {tolerance!r}:",
                "            raise ValueError('DDS clock differs "
                f"from compiled target: {name}')",
                f"        if self.{attributes[name]}.bus.ref_period_mu "
                f"!= {devices[name].spi_ref_period_mu}:",
                "            raise ValueError('DDS SPI clock differs "
                "from compiled target')",
            ]
            if any(
                e.kind == "dds_config" and e.signal == name
                for p in compiled.points
                for e in p.physical_events
            ):
                attr = attributes[name]
                lines += [
                    f"        if {devices[name].config_duration_mu} < "
                    f"332 * self.{attr}.bus.ref_period_mu + 8 + "
                    f"(self.{attr}.sync_data.io_update_delay // 8) * 8:",
                    "            raise ValueError('DDS configuration budget is below "
                    "the real tracking driver SPI cursor minimum')",
                ]
    for resource in execution["prepared_shared_resources_required"]:
        message = (
            "Shared DDS resource must be externally prepared "
            f"and exclusively owned: {resource}"
        )
        lines += [
            f"        if {resource!r} not in self.prepared_shared_resources:",
            f"            raise RuntimeError({message!r})",
        ]
    for name in sorted(square_devices):
        attr, device = attributes[name], devices[name]
        lines += [
            f"        if not isinstance(self.{attr}.io_update, TTLOut):",
            "            raise ValueError('Square commit requires "
            "direct TTL IO_UPDATE')",
            f"        if tuple(signature(self.{attr}.cpld.set_profile).parameters) "
            "!= ('channel', 'profile'):",
            "            raise ValueError('Square export requires ARTIQ 9 "
            "channel/profile API')",
            f"        if self.{attr}.bus.ref_period_mu != {device.spi_ref_period_mu}:",
            "            raise ValueError('Square SPI clock differs "
            "from compiled target')",
            f"        if {device.square_prepare_duration_mu} < "
            f"156 * self.{attr}.bus.ref_period_mu:",
            "            raise ValueError('Square preparation budget is below "
            "driver SPI cursor minimum')",
            f"        if self.{attr}.sync_data.io_update_delay != 0:",
            "            raise ValueError('Square commit requires "
            "zero io_update_delay; "
            "include calibration in the declared output timing contract')",
        ]
    dds_names = [name for name in names if devices[name].kind == "dds"]
    for name in dds_names:
        revision = getattr(devices[name], "urukul_proto_rev", None)
        if revision is not None:
            lines += [
                f"        if self.{attributes[name]}.cpld.proto_rev != {revision}:",
                "            raise ValueError('CPLD protocol differs from declared "
                "urukul_proto_rev')",
            ]
    for index, name in enumerate(dds_names):
        for other in dds_names[index + 1 :]:
            if (
                name not in square_devices | ram_devices
                and other not in square_devices | ram_devices
            ):
                continue
            group = devices[name].update_group
            shared = group is not None and group == devices[other].update_group
            first, second = attributes[name], attributes[other]
            first_bus, second_bus = devices[name].bus, devices[other].bus
            if first_bus is not None and second_bus is not None:
                relation = "!=" if first_bus == second_bus else "=="
                lines += [
                    f"        if self.{first}.bus.channel {relation} "
                    f"self.{second}.bus.channel:",
                    "            raise ValueError('DDS SPI wiring differs "
                    "from compiled bus groups')",
                ]
            lines += [
                f"        if self.{first}.bus.channel == self.{second}.bus.channel "
                f"and self.{first}.chip_select == self.{second}.chip_select:",
                "            raise ValueError('DDS channels alias one "
                "SPI chip select')",
            ]
            if shared:
                condition = (
                    f"not isinstance(self.{first}.io_update, TTLOut) or "
                    f"not isinstance(self.{second}.io_update, TTLOut) or "
                    f"self.{first}.io_update.target_o != "
                    f"self.{second}.io_update.target_o"
                )
            else:
                condition = (
                    f"isinstance(self.{first}.io_update, TTLOut) and "
                    f"isinstance(self.{second}.io_update, TTLOut) and "
                    f"self.{first}.io_update.target_o == "
                    f"self.{second}.io_update.target_o"
                )
            lines += [
                f"        if {condition}:",
                "            raise ValueError('DDS IO_UPDATE wiring "
                "differs from compiled groups')",
            ]
    for name in sorted(ram_devices):
        attr = attributes[name]
        lines += [
            f"        if not isinstance(self.{attr}.io_update, TTLOut):",
            "            raise ValueError('RAM launch requires direct TTL IO_UPDATE; "
            "SPI RegIOUpdate is unsupported')",
            f"        if tuple(signature(self.{attr}.cpld.set_profile).parameters) "
            "!= ('channel', 'profile'):",
            "            raise ValueError('RAM export requires the ARTIQ 9 "
            "channel/profile CPLD API')",
            f"        if not 4 <= self.{attr}.chip_select <= 7:",
            "            raise ValueError('RAM export requires one "
            "individually addressed DDS')",
            f"        if self.{attr}.sync_data.io_update_delay != 0:",
            "            raise ValueError('RAM launch requires zero io_update_delay; "
            "nonzero calibration is unsupported')",
        ]
        if any(
            e.kind == "dds_ram_rearm" and e.signal == name
            for p in compiled.points
            for e in p.physical_events
        ):
            budget = devices[name].ram_rearm_duration_mu
            lines += [
                f"        if self.{attr}.cpld.proto_rev not in (8, 9):",
                "            raise ValueError('Unsupported Urukul "
                "RAM replay protocol')",
                f"        spi_cycles = 428 if self.{attr}.cpld.proto_rev == 9 else 368",
                f"        if {budget} < spi_cycles * "
                f"self.{attr}.bus.ref_period_mu + 16:",
                "            raise ValueError('RAM rearm budget is below "
                "the driver SPI cursor minimum')",
            ]
        if any(
            e.kind == "dds_ram_phase" and e.signal == name
            for p in compiled.points
            for e in p.physical_events
        ):
            budget = devices[name].ram_phase_update_duration_mu
            lines += [
                f"        if {budget} < 52 * self.{attr}.bus.ref_period_mu:",
                "            raise ValueError('RAM phase budget is below "
                "the driver SPI cursor minimum')",
            ]
    lines += [
        "",
        "    def _execute_point(self, point):",
    ]
    if mode == "auto":
        if len(groups) == 1:
            lines.append("        self.kernel_0(self._point_values[point])")
        else:
            lines.append("        group = self._point_groups[point]")
            for group in groups:
                word = "if" if group["index"] == 0 else "elif"
                lines += [
                    f"        {word} group == {group['index']}:",
                    f"            self.kernel_{group['index']}"
                    "(self._point_values[point])",
                ]
            lines += [
                "        else:",
                "            raise ValueError('Unknown compiled kernel group')",
            ]
    else:
        lines.append("        getattr(self, 'point_' + str(point))()")
    lines += [
        "",
        "    def _run_shot(self, point, shot):",
        "        self.result_adapter.before_shot(point, shot)",
        "        self._execute_point(point)",
        "        self.result_adapter.after_shot(point, shot)",
        "",
        "    def run(self):",
        "        if self.result_adapter is None:",
        "            raise RuntimeError('A configured result_adapter "
        "is required; see manifest.json')",
        "        self._validate_target()",
        "        self.initialize_devices()",
    ]
    if (
        ram_enabled
        or square_devices
        or any(
            e.kind == "dds_config" for p in compiled.points for e in p.physical_events
        )
    ):
        lines.append(
            "        self._validate_target()  # Recheck loaded EEPROM calibration."
        )
    lines += host_loop_lines(execution_plan_for(compiled))
    lines += ["        self.result_adapter.finish()", ""]
    source = "\n".join(lines)
    ast.parse(source)
    description = {
        "mode": mode,
        **(
            {
                "square_execution": "buffered_prepare_timed_commit",
                "requested_mode": requested_mode,
            }
            if square_devices
            else {}
        ),
        **(
            {
                "requested_mode": requested_mode,
                "ram_preparation": "per-point preload before timed origin; RF off",
                "ram_export": "resident waveform banks; per-point kernels",
                "ram_table_points": sorted(table_points),
                "ram_execution": "event_table" if table_points else "unrolled",
            }
            if ram_enabled
            else {}
        ),
        "kernel_count": len(groups) if mode == "auto" else len(compiled.points),
        "source_lines": len(source.splitlines()),
        "host_loops": "preserved",
        "groups": [
            {
                "kernel": f"kernel_{g['index']}",
                "points": [p.index for p in g["points"]],
                "parameter_columns": g["columns"],
                "event_slots": [
                    {"kind": kind, "device": dev} for kind, dev in g["signature"]
                ],
            }
            for g in groups
        ],
    }
    return source, description


def generate_source(compiled, *, mode="auto"):
    """Generate generic parameterized kernels by default; unrolled is a debug mode."""
    return _generate(compiled, mode)[0]
