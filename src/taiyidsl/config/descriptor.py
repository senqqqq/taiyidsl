"""Read declarative device databases and resolve laboratory signal wiring.

Database files are configuration, not executable loaders.  Only literal values,
previously defined constants and numeric arithmetic are accepted.  Supplying an
already evaluated mapping is the explicit escape hatch for dynamic device_db.py.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import operator
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path

from .hardware import Device, DeviceSetup, SignalMap
from .spectrum import SpectrumCard, SpectrumConfig, SpectrumGroup, SpectrumOutput


def read_database(source, name):
    """Return a detached database and its provenance, without executing code."""
    if isinstance(source, Mapping):
        data = copy.deepcopy(dict(source))
        encoded = json.dumps(data, sort_keys=True, allow_nan=False).encode("utf-8")
        return data, {"kind": "mapping", "sha256": hashlib.sha256(encoded).hexdigest()}
    path = Path(source).resolve(strict=True)
    raw = path.read_bytes()
    constants = {}
    arithmetic = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
    }

    def value(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in constants:
            return copy.deepcopy(constants[node.id])
        if isinstance(node, (ast.List, ast.Tuple)):
            items = [value(n) for n in node.elts]
            return tuple(items) if isinstance(node, ast.Tuple) else items
        if isinstance(node, ast.Dict) and all(k is not None for k in node.keys):
            pairs = [(value(k), value(v)) for k, v in zip(node.keys, node.values)]
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError("Duplicate database dictionary key")
            return result
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            number = value(node.operand)
            if type(number) not in (int, float):
                raise ValueError("Arithmetic requires numbers")
            return number if isinstance(node.op, ast.UAdd) else -number
        if isinstance(node, ast.BinOp) and type(node.op) in arithmetic:
            left, right = value(node.left), value(node.right)
            if type(left) not in (int, float) or type(right) not in (int, float):
                raise ValueError("Arithmetic requires numbers")
            return arithmetic[type(node.op)](left, right)
        raise ValueError("Unsupported dynamic database expression")

    try:
        for node in ast.parse(raw.decode("utf-8-sig"), filename=str(path)).body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # module docstring
            if isinstance(node, ast.Assign) and all(
                isinstance(target, ast.Name) for target in node.targets
            ):
                resolved = value(node.value)
                for target in node.targets:
                    constants[target.id] = resolved
            else:
                raise ValueError("Only constant assignments are supported")
        data = constants[name]
        if not isinstance(data, dict):
            raise ValueError(f"{name} must be a dictionary")
    except (ValueError, KeyError, TypeError, SyntaxError, ArithmeticError) as exc:
        raise ValueError(
            f"Cannot statically read {name} from {path}: {exc}. "
            "Provide a declarative file or an explicitly evaluated mapping. "
            "No database code was executed."
        ) from exc
    return copy.deepcopy(data), {
        "kind": "file",
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _keys(data, allowed, label, required=()):
    if not isinstance(data, Mapping):
        raise TypeError(f"{label} must be a mapping")
    extra, missing = set(data) - set(allowed), set(required) - set(data)
    if extra or missing:
        raise ValueError(
            f"Invalid {label}: unknown={sorted(extra)}, missing={sorted(missing)}"
        )


def resolve_device(database, name):
    """Resolve standard ARTIQ aliases, with cycle and missing-entry detection."""
    seen = set()
    while isinstance(database.get(name), str):
        if name in seen:
            raise ValueError(f"ARTIQ device alias cycle: {name}")
        seen.add(name)
        name = database[name]
    if name not in database or not isinstance(database[name], Mapping):
        raise ValueError(f"Unknown ARTIQ device: {name}")
    return name, database[name]


def _artiq_device(database, name, overrides=None):
    name, record = resolve_device(database, name)
    driver = record.get("class")
    args = record.get("arguments", {})
    if driver in {"TTLOut", "TTLInOut"}:
        result = dict(name=name, kind="ttl", grid_mu=1, min_duration_mu=1)
    elif driver == "AD9910":
        if "pll_n" in args:
            raise ValueError(
                "ARTIQ 9 AD9910 does not accept a pll_n constructor argument; "
                "its driver derives the multiplier from CPLD refclk/clk_div"
            )
        cpld_name, cpld = resolve_device(database, args["cpld_device"])
        cpld_args = cpld.get("arguments", {})
        clock = cpld_args.get("refclk", 125e6)
        divider = [4, 1, 2, 4][cpld_args.get("clk_div", 0)]
        input_clock = clock / divider
        sysclk = (
            input_clock * round(1e9 / input_clock)
            if args.get("pll_en", True)
            else input_clock
        )
        result = dict(
            name=name,
            kind="dds",
            bus=resolve_device(database, cpld_args["spi_device"])[0],
            update_group=cpld_name,
            sysclk_hz=sysclk,
        )
    else:
        raise ValueError(f"Unsupported DSL ARTIQ channel {name}: {driver}")
    overrides = dict(overrides or {})
    _keys(
        overrides,
        {f.name for f in fields(Device)}
        - {"name", "kind", "bus", "update_group", "sysclk_hz"},
        "channel timing",
    )
    return Device(**(result | overrides))


def _artiq_endpoint(database, name):
    """Identify physical outputs even when the database repeats driver entries."""
    name, record = resolve_device(database, name)
    args = record.get("arguments", {})
    if record.get("class") in {"TTLOut", "TTLInOut"}:
        return ("rtio", args["channel"])
    if record.get("class") == "AD9910":
        cpld = resolve_device(database, args["cpld_device"])[0]
        return ("dds", cpld, args["chip_select"])
    return ("device", name)


def _signal_routes(signals):
    """Normalize flat signals or one qubit/role level into logical names."""
    if not isinstance(signals, Mapping):
        raise TypeError("signals must be a mapping")
    result = {}

    def add(name, channel):
        if not isinstance(channel, str) or not channel:
            raise ValueError(f"Signal {name} must name a laboratory channel")
        if name in result:
            raise ValueError(f"Duplicate logical signal after grouping: {name}")
        result[name] = channel

    for name, value in signals.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Signal names must be nonempty strings")
        if isinstance(value, Mapping):
            if "." in name or not value:
                raise ValueError(
                    "Signal groups require an undotted qubit name and roles"
                )
            for role, channel in value.items():
                if not isinstance(role, str) or not role or "." in role:
                    raise ValueError(
                        "Grouped signal roles must be nonempty and undotted"
                    )
                add(f"{name}.{role}", channel)
        else:
            add(name, value)
    return result


def describe_setup(setup):
    """Return detached configuration and routing, never probe live devices."""
    descriptor = getattr(setup, "_descriptor", None)
    if descriptor is None:
        return {"name": setup.name, "target": setup.to_dict()}
    instruments = {}
    cards = {
        uid: card
        for group in (setup.spectrum.groups.values() if setup.spectrum else ())
        for uid, card in group.cards.items()
    }
    for uid, instrument in descriptor["instruments"].items():
        item = copy.deepcopy(instrument)
        if instrument["kind"] == "artiq":
            resolved, record = resolve_device(setup._device_db, instrument["device"])
            item["resolved_device"] = resolved
            item["connection"] = {
                "driver": f"{record['module']}.{record['class']}",
                "host": record.get("arguments", {}).get("host"),
            }
            item["settings"] = copy.deepcopy(record.get("arguments", {}))
        else:
            address = setup._spectrum_addresses[uid]
            # These are the actual spcm.Card selection arguments, not a
            # fabricated URI when the user chooses lookup by serial number.
            selector = (
                {"address": address}
                if address is not None
                else {"serial_number": cards[uid].serial_number}
            )
            item["connection"] = {"driver": "spcm.Card", **selector}
            item["settings"] = {**cards[uid].to_dict(), "mode": cards[uid].mode}
        instruments[uid] = item
    return {
        "name": setup.name,
        "backend": setup._backend,
        "databases": copy.deepcopy(setup._database_sources),
        "instruments": instruments,
        "channels": copy.deepcopy(descriptor["channels"]),
        "sync_groups": copy.deepcopy(descriptor.get("sync_groups", {})),
        "signals": copy.deepcopy(descriptor["signals"]),
        "connections": setup.connections(),
        "trigger_connections": setup.trigger_connections(),
    }


def setup_from_descriptor(descriptor, *, device_db, spec_db=None):
    """Resolve one source of truth for devices, wiring and logical signals."""
    _keys(
        descriptor,
        {
            "schema_version",
            "name",
            "backend",
            "instruments",
            "channels",
            "sync_groups",
            "signals",
            "rtio_coarse_mu",
            "rtio_lane_count",
        },
        "descriptor",
        ("backend", "instruments", "channels", "signals"),
    )
    if descriptor.get("schema_version", 1) != 1:
        raise ValueError("Unsupported descriptor schema_version")
    backend = descriptor["backend"].replace("-", "_")
    if backend not in {"artiq", "artiq_spectrum"}:
        raise ValueError("Descriptor supports artiq or artiq-spectrum")
    devices_db, artiq_source = read_database(device_db, "device_db")
    specs, spec_source = (
        read_database(spec_db, "spec_db") if spec_db is not None else ({}, None)
    )
    instruments = copy.deepcopy(dict(descriptor["instruments"]))
    cores, cards, addresses = [], {}, {}
    for uid, instrument in instruments.items():
        _keys(instrument, {"kind", "device"}, f"instrument {uid}", ("kind", "device"))
        if instrument["kind"] == "artiq":
            core_name, core = resolve_device(devices_db, instrument["device"])
            if core.get("class") != "Core":
                raise ValueError("ARTIQ instrument must refer to a Core device")
            if resolve_device(devices_db, "core")[0] != core_name:
                raise ValueError("ARTIQ instrument must resolve to device_db['core']")
            cores.append(core)
        elif instrument["kind"] == "spectrum":
            spec = copy.deepcopy(specs.get(instrument["device"]))
            if not isinstance(spec, dict):
                raise ValueError(f"Unknown spec_db entry: {instrument['device']}")
            addresses[uid] = spec.pop("address", None)
            if addresses[uid] is not None and not isinstance(addresses[uid], str):
                raise TypeError("Spectrum address must be text")
            cards[uid] = SpectrumCard(**spec)
        else:
            raise ValueError(f"Unknown instrument kind: {instrument['kind']}")
    if len(cores) != 1:
        raise ValueError("Exactly one ARTIQ core is currently supported")
    tick = cores[0].get("arguments", {}).get("ref_period", 1e-9)
    devices, outputs, mapping, rows, sharing = {}, {}, SignalMap(), [], {}
    coverage = {}
    endpoint_names = {}
    channels = descriptor["channels"]
    for alias, channel in channels.items():
        _keys(
            channel,
            {
                "instrument",
                "channel",
                "device",
                "sharing",
                "timing",
                "carrier_mode",
                "initial_frequency_hz",
                "coverage",
            },
            f"channel {alias}",
            ("instrument",),
        )
        instrument = instruments.get(channel["instrument"])
        if instrument is None:
            raise ValueError(f"Unknown channel instrument: {channel['instrument']}")
        if channel.get("sharing", "exclusive") not in {"exclusive", "global"}:
            raise ValueError("Channel sharing must be exclusive or global")
        zones = None
        if "coverage" in channel:
            _keys(channel["coverage"], {"zones"}, "channel coverage", ("zones",))
            zones = channel["coverage"]["zones"]
            if (
                not isinstance(zones, (list, tuple))
                or not zones
                or any(not isinstance(zone, str) or not zone for zone in zones)
                or len(set(zones)) != len(zones)
            ):
                raise ValueError("Coverage zones must be distinct nonempty names")
            if (
                instrument["kind"] != "spectrum"
                or cards[channel["instrument"]].mode != "awg"
            ):
                raise ValueError("Zone coverage requires a Spectrum AWG optical output")
        if instrument["kind"] == "spectrum":
            if "device" in channel or "timing" in channel:
                raise ValueError(
                    "Spectrum channels use a channel index, not ARTIQ device/timing"
                )
            outputs[alias] = SpectrumOutput(
                channel["instrument"],
                channel["channel"],
                channel.get("carrier_mode", "epoch"),
                channel.get("initial_frequency_hz"),
            )
            devices[alias] = Device(alias, "spectrum")
            physical, port = alias, channel["channel"]
            endpoint = (channel["instrument"], port)
        else:
            if set(channel) & {"channel", "carrier_mode", "initial_frequency_hz"}:
                raise ValueError("ARTIQ channels refer to device_db names")
            device = _artiq_device(devices_db, channel["device"], channel.get("timing"))
            devices[device.name] = device
            physical = device.name
            record = resolve_device(devices_db, physical)[1]
            port = record.get("arguments", {}).get("channel")
            endpoint = _artiq_endpoint(devices_db, physical)
        if endpoint in endpoint_names:
            raise ValueError(
                f"Duplicate physical channel: {alias}, {endpoint_names[endpoint]}; "
                "use one shared alias"
            )
        endpoint_names[endpoint] = alias
        sharing[physical] = channel.get("sharing", "exclusive")
        if zones is not None:
            coverage[physical] = tuple(zones)
        rows.append(
            dict(
                channel=alias,
                device=physical,
                instrument=channel["instrument"],
                port=port,
                sharing=sharing[physical],
            )
        )
    by_alias = {row["channel"]: row for row in rows}
    connections = []
    for signal, alias in _signal_routes(descriptor["signals"]).items():
        if alias not in by_alias:
            raise ValueError(f"Unknown signal channel: {signal} -> {alias}")
        row = by_alias[alias]
        mapping[signal] = row["device"]
        connections.append(dict(signal=signal, **row))
    shared = {}
    for physical, kind in sharing.items():
        users = tuple(
            signal for signal, target in mapping.items() if target == physical
        )
        if len(users) > 1 and kind != "global" and physical not in coverage:
            raise ValueError(f"Shared output {physical} must declare sharing='global'")
        if kind == "global" and physical not in coverage:
            shared[physical] = users
    groups, triggers = {}, []
    for name, group in descriptor.get("sync_groups", {}).items():
        _keys(
            group,
            {"cards", "triggers", "trigger", "timing", "calibration"},
            f"sync group {name}",
            ("cards",),
        )
        selected = {uid: cards[uid] for uid in group["cards"]}
        if ("triggers" in group) == ("trigger" in group):
            raise ValueError("Specify triggers per card or one shared trigger")
        route = group.get("triggers", dict.fromkeys(selected, group.get("trigger")))
        if set(route) != set(selected):
            raise ValueError("Trigger routes must cover exactly the sync group's cards")
        canonical = {}
        for uid, target in route.items():
            if target in by_alias:
                device = devices[by_alias[target]["device"]]
                if target in devices_db and (
                    resolve_device(devices_db, target)[0] != device.name
                ):
                    raise ValueError(
                        f"Ambiguous trigger {target}: channel and device_db entry "
                        "refer to different outputs"
                    )
            else:
                device = _artiq_device(devices_db, target)
                # A direct database name may already have a channel declaration.
                # Keep its explicit timing so backend trigger checks see it.
                device = devices.get(device.name, device)
            if device.kind != "ttl":
                raise ValueError("Spectrum trigger must be an ARTIQ TTL")
            devices[device.name] = device
            canonical[uid] = device.name
            triggers.append(dict(group=name, card=uid, device=device.name))
        timing = dict(group.get("timing", {}))
        _keys(
            timing,
            {
                "trigger_lead_mu",
                "trigger_width_mu",
                "epoch_grid_mu",
                "epoch_offset_mu",
                "max_tile_samples",
            },
            "sync timing",
        )
        groups[name] = SpectrumGroup(
            cards=selected,
            outputs={
                key: output
                for key, output in outputs.items()
                if output.card in selected
            },
            trigger_devices=canonical,
            calibration=group.get(
                "calibration", {"status": "unmeasured", "revision": "unmeasured"}
            ),
            **timing,
        )
    # Includes trigger-only devices: two names must not conceal one RTIO output.
    artiq_outputs = {}
    for name, device in devices.items():
        if device.kind == "spectrum":
            continue
        endpoint = _artiq_endpoint(devices_db, name)
        if endpoint in artiq_outputs and artiq_outputs[endpoint] != name:
            raise ValueError(
                f"Duplicate physical channel: {name}, {artiq_outputs[endpoint]}"
            )
        artiq_outputs[endpoint] = name
    if backend == "artiq_spectrum" and set(cards) != {
        uid for group in groups.values() for uid in group.cards
    }:
        raise ValueError("Every Spectrum card must belong to exactly one sync group")
    if backend == "artiq" and (cards or groups):
        raise ValueError("ARTIQ-only descriptor cannot declare Spectrum cards")
    setup = DeviceSetup(
        devices=devices,
        tick_seconds=tick,
        name=descriptor.get("name", "laboratory"),
        rtio_coarse_mu=descriptor.get("rtio_coarse_mu", 8),
        rtio_lane_count=descriptor.get("rtio_lane_count", 8),
        spectrum=SpectrumConfig(groups, artiq_tick_seconds=tick) if groups else None,
    )
    setup._descriptor = copy.deepcopy(dict(descriptor))
    setup._signal_map = mapping
    setup._backend = backend
    setup._connections = connections
    setup._trigger_connections = triggers
    setup._shared_outputs = shared
    setup._zone_coverage = coverage
    setup._device_db = devices_db
    setup._spec_db = specs
    setup._spectrum_addresses = addresses
    setup._database_sources = {"device_db": artiq_source, "spec_db": spec_source}
    return setup
