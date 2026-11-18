"""Execute trusted generated source against independent offline drivers."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from taiyidsl.backends.artiq._contracts import (
    ExportError,
    _devices,
    _point_end,
    _words,
    square_effect_record,
)
from taiyidsl.backends.artiq.codegen import _generate, generate_source
from taiyidsl.backends.artiq.mock_drivers import (
    _DriverClock,
    _fake_artiq,
    _MockCore,
    _MockDDS,
    _MockSwitch,
)
from taiyidsl.backends.artiq.submission import (
    validate_channel_order,
    validate_sed,
)
from taiyidsl.backends.artiq.verification_common import (
    _expected_events,
    _verify_host_order,
    _verify_ram_effects,
)
from taiyidsl.execution import device_execution_plan
from taiyidsl.serialization import to_jsonable as _jsonable


def _verify_source(compiled, source):
    from taiyidsl.backends.artiq.polar_contracts import uses_polar
    from taiyidsl.backends.artiq.verification_polar import verify_polar

    if uses_polar(compiled):
        return verify_polar(compiled, source)
    if hasattr(compiled, "assert_integrity"):
        compiled.assert_integrity()
    ast.parse(source)
    clock = _DriverClock()
    clock.tick_seconds = float(compiled.setup.tick_seconds)
    clock.rtio_coarse_mu = getattr(compiled.setup, "rtio_coarse_mu", 8)
    device_map = {
        "core": _MockCore(
            clock,
            float(compiled.setup.tick_seconds),
            getattr(compiled.setup, "rtio_coarse_mu", 8),
        )
    }
    for name, device in _devices(compiled).items():
        device_map[name] = (
            _MockDDS(clock, name, device)
            if device.kind == "dds"
            else _MockSwitch(clock, name, "ttl_", int(getattr(device, "grid_mu", 1)))
        )
    update_outputs = {}
    for name, device in _devices(compiled).items():
        if device.kind == "dds":
            group = (
                ("group", device.update_group)
                if device.update_group
                else ("device", name)
            )
            target = update_outputs.setdefault(group, 256 * (len(update_outputs) + 1))
            device_map[name].io_update.target_o = target
    buses = {}
    bus_channels = {}
    for name, device in _devices(compiled).items():
        if device.kind == "dds":
            device_map[name].bus.channel = buses.setdefault(
                device.bus or f"unknown:{name}", len(buses)
            )
            bus = device_map[name].bus.channel
            device_map[name].chip_select = 4 + bus_channels.get(bus, 0)
            bus_channels[bus] = bus_channels.get(bus, 0) + 1
    namespace = {"__name__": "generated_offline_check"}
    point_reports = []
    traces = []
    with _fake_artiq(clock, device_map):
        exec(compile(source, "<generated-artiq>", "exec"), namespace)  # noqa: S102 -- trusted generated test artifact
        instance = namespace["GeneratedExperiment"]()
        instance.build()
        execution = device_execution_plan(compiled)
        # The mock explicitly fulfils the external contract; real users must
        # prepare/own these resources before acknowledging them.
        instance.prepared_shared_resources = tuple(
            execution["prepared_shared_resources_required"]
        )
        instance._validate_target()
        clock.verifying_initialization = True
        try:
            instance.initialize_devices()
        finally:
            clock.verifying_initialization = False
        expected_initialization = []
        for name in execution["used_devices"]:
            if name in execution["initialize_dds"]:
                expected_initialization.append(("init", name))
            idle = (
                "on"
                if getattr(compiled.setup.devices[name], "ttl_idle_level", 0)
                else "off"
            )
            expected_initialization.append((idle, name))
        if clock.initialization_calls != expected_initialization:
            raise ExportError("Generated initialization differs from device usage plan")
        for point in compiled.points:
            clock.events = []
            clock.square_effects = []
            clock.ram_effects = []
            clock.rtio_events = []
            clock.spi_busy = {}
            clock.nco_epochs = []
            clock.planned_times = {int(e.start_mu) for e in point.physical_events} | {
                _point_end(point)
            }
            square_names = {
                e.signal for e in point.physical_events if e.kind == "dds_prepare"
            }
            for name in square_names:
                device = compiled.setup.devices[name]
                device_map[name].square_group_members = [
                    device_map[other]
                    for other in sorted(square_names)
                    if other == name
                    or (
                        device.update_group is not None
                        and device.update_group
                        == compiled.setup.devices[other].update_group
                    )
                ]
            if hasattr(instance, "_execute_point"):
                instance._execute_point(point.index)
            else:
                getattr(instance, f"point_{point.index}")()
            expected = _expected_events(compiled, point)
            validate_channel_order(clock.rtio_events, coarse_mu=clock.rtio_coarse_mu)
            validate_sed(
                [t for t, _ in clock.rtio_events],
                coarse_mu=clock.rtio_coarse_mu,
                lane_count=compiled.setup.rtio_lane_count,
            )
            if clock.events != expected:
                raise ExportError(
                    f"Point {point.index}: generated driver events "
                    "differ from compiled timeline"
                )
            expected_effects = []
            for event in point.physical_events:
                if event.kind == "dds_effect":
                    ftw, _, asf = _words(event, compiled.setup.devices[event.signal])
                    expected_effects.append(
                        square_effect_record(
                            event.signal,
                            event.start_mu,
                            ftw,
                            event.params["square_pow"],
                            asf,
                        )
                    )

            def effect_key(event):
                return (event["start_mu"], event["signal"])

            if sorted(clock.square_effects, key=effect_key) != sorted(
                expected_effects, key=effect_key
            ):
                raise ExportError(
                    "Committed Square output effects differ from the requested timeline"
                )
            if any(
                device_map[name].square_staged is not None
                or device_map[name].io_update.high_mu is not None
                for name in square_names
            ):
                raise ExportError(
                    "Shot ends with an incomplete Square preparation or commit"
                )
            ram_effects = _verify_ram_effects(compiled, point, clock)
            expected_epochs = {}
            for event in point.physical_events:
                if (
                    event.params.get("nco_mode") == "continuous"
                    and "ftw" in event.params
                ):
                    expected_epochs[event.signal] = dict(
                        signal=event.signal,
                        release_mu=event.params["nco_epoch_release_mu"],
                        effect_mu=event.params["nco_epoch_effect_mu"],
                        ftw=int(event.params["ftw"]),
                        pow=event.params["nco_initial_pow"],
                    )
            if sorted(clock.nco_epochs, key=lambda e: e["signal"]) != sorted(
                expected_epochs.values(), key=lambda e: e["signal"]
            ):
                raise ExportError(
                    "NCO release or initial reference differs from the plan"
                )
            ram_names = {
                e.signal
                for e in point.physical_events
                if e.kind == "dds_ram_start" and e.params.get("ram_boundary")
            }
            if any(
                device_map[name].ram_pow_pending
                or device_map[name].io_update.high_mu is not None
                for name in ram_names
            ):
                raise ExportError(
                    "Shot ends with an incomplete RAM preparation or commit"
                )
            endpoint = _point_end(point)
            if (
                clock.cursor - clock.origin != endpoint
                or clock.wait_endpoint != endpoint
            ):
                raise ExportError(
                    f"Point {point.index}: generated end/wait time "
                    "differs from compiled timeline"
                )
            trace = {
                "events": _jsonable(clock.events),
                **(
                    {"square_effects": sorted(clock.square_effects, key=effect_key)}
                    if expected_effects
                    else {}
                ),
                **({"ram_effects": ram_effects} if ram_effects else {}),
                **({"nco_epochs": list(clock.nco_epochs)} if clock.nco_epochs else {}),
                "end_mu": endpoint,
                "wait_mu": clock.wait_endpoint,
            }
            traces.append(trace)
            point_reports.append(
                {
                    "point": point.index,
                    "events": len(clock.events),
                    "status": "passed",
                    "end_mu": endpoint,
                    "trace_sha256": hashlib.sha256(
                        json.dumps(trace, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
        host_order = _verify_host_order(instance, compiled)
    return {
        "offline_export_check": "passed",
        "python_syntax": "passed",
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
        "points": point_reports,
        "host_order": host_order,
        "initialization": {"status": "passed", **execution},
        "limitations": [
            "Mock verifies declared timing contracts, not real driver timing",
            "RTIO throughput and optical phase are not verified",
        ],
    }, traces


def verify_export(compiled, source_path=None, *, source=None, mode="auto"):
    """Execute trusted generated source against independent mock drivers, per point.

    Also checks completion time and actual host-loop order/hook association.
    This test harness executes Python; it is not a sandbox for untrusted source.
    """
    if source is None:
        source = (
            Path(source_path).read_text(encoding="utf-8")
            if source_path
            else generate_source(compiled, mode=mode)
        )
    return _verify_source(compiled, source)[0]


def verify_equivalence(compiled):
    """Compare executed compact and unrolled traces, not just their source plans."""
    auto_source, auto_info = _generate(compiled, "auto")
    unrolled_source, unrolled_info = _generate(compiled, "unrolled")
    auto_report, auto_traces = _verify_source(compiled, auto_source)
    unrolled_report, unrolled_traces = _verify_source(compiled, unrolled_source)
    if (
        auto_traces != unrolled_traces
        or auto_report["host_order"] != unrolled_report["host_order"]
    ):
        raise ExportError("Parameterized and unrolled execution are not equivalent")
    return {
        "status": "passed",
        "points": auto_report["points"],
        "host_order": auto_report["host_order"],
        "auto": auto_info,
        "unrolled": unrolled_info,
        "compared": [
            "ordered physical events",
            "event timestamps",
            "DDS words",
            "phase references",
            "driver durations",
            "shot end/wait times",
            "host order and result hooks",
        ],
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
    }
