"""Independent executable POWASF/mixed-driver model and trace checks."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

from taiyidsl.backends.artiq._contracts import square_effect_record
from taiyidsl.backends.artiq.polar_contracts import _KINDS, _parameters, _signed
from taiyidsl.backends.artiq.submission import (
    validate_channel_order,
    validate_sed,
)
from taiyidsl.execution import device_execution_plan


def _fail(message):
    from taiyidsl.backends.artiq._contracts import ExportError

    raise ExportError(message)


class _PolarSwitch:
    def __init__(self, clock, name, prefix, grid):
        self.clock, self.name, self.prefix, self.grid = clock, name, prefix, grid
        self.is_on = False
        self.target_o = 0

    def _edge(self, value):
        clock = self.clock
        edge = "on" if value else "off"
        self.is_on = value
        if clock.verifying_initialization:
            clock.initialization_calls.append((edge, self.name))
        elif clock.awaiting_origin:
            if value:
                _fail("RF/TTL output enabled during preload")
        else:
            if clock.cursor % self.grid:
                _fail("RF/TTL edge is off its declared grid")
            clock.events.append(
                (self.prefix + edge, self.name, clock.cursor - clock.origin)
            )
            clock.edge(("switch", self.target_o))

    def on(self):
        self._edge(True)

    def off(self):
        self._edge(False)


class _PolarUpdate:
    def __init__(self, dds):
        self.dds, self.high_mu, self.target_o = dds, None, 0

    def on(self):
        dds, clock = self.dds, self.dds.clock
        if self.high_mu is not None:
            _fail("IO_UPDATE rises without falling")
        self.high_mu = clock.cursor
        if clock.awaiting_origin:
            dds._commit_preload()
            return
        clock.edge(("update", self.target_o))
        if dds.active_cfr[2]:
            if dds.staged_cfr != (1, 3, 0, 0) or not dds.held_clear:
                _fail("NCO epoch must release previously held clear in POWASF mode")
            if dds.selected_profile != 0:
                _fail("NCO epoch requires the initial RF-off phase profile")
            words = dds._words()
            if len(words) != 1 or ((words[0] >> 2) & 0x3FFF):
                _fail("NCO initial profile must have one zero-ASF word")
            dds.active_cfr = dds.staged_cfr
            dds.released = True
            clock.nco_epochs.append(
                dict(
                    signal=dds.name,
                    release_mu=clock.cursor - clock.origin,
                    effect_mu=clock.cursor
                    - clock.origin
                    + dds.device.nco_epoch_latency_mu,
                    ftw=dds.ftw,
                    pow=(words[0] >> 16) & 0xFFFF,
                )
            )
            kind = "dds_nco_epoch"
        else:
            if not dds.released:
                _fail("Playback precedes NCO reference release")
            kind = "dds_polar_retrigger"
            dds._launch("update", clock.cursor)
        clock.events.append((kind, dds.name, clock.cursor - clock.origin))

    def off(self):
        clock, dds = self.dds.clock, self.dds
        if self.high_mu is None or clock.cursor != self.high_mu + 8:
            _fail("IO_UPDATE must have a paired 8-mu fall")
        self.high_mu = None
        if not clock.awaiting_origin:
            clock.edge(("update", self.target_o))
            clock.events.append(("update_off", dds.name, clock.cursor - clock.origin))

    def pulse_mu(self, duration):
        self.on()
        self.dds.clock.cursor += int(duration)
        self.off()


class _PolarProfile:
    def __init__(self, dds):
        self.dds = dds
        self.proto_rev = 8 if dds.device.ram_profile_control == "urukul_rev8" else 9

    def set_profile(self, channel, profile):
        dds, clock = self.dds, self.dds.clock
        if channel != dds.chip_select - 4 or not 0 <= profile <= 7:
            _fail("Invalid CPLD profile/channel")
        start = clock.cursor
        coarse = dds.bus.ref_period_mu
        duration = (52 if self.proto_rev == 8 else 112) * coarse
        offsets = (
            (0, coarse)
            if self.proto_rev == 8
            else (0, coarse, 52 * coarse, 53 * coarse)
        )
        clock.spi(dds.device, duration, offsets)
        clock.cursor += duration
        changed = profile != dds.selected_profile
        dds.selected_profile = int(profile)
        if not clock.awaiting_origin:
            if not changed:
                _fail("Selecting an unchanged profile cannot retrigger RAM")
            if not dds.released:
                _fail("Profile playback precedes NCO epoch")
            clock.events.append(
                (
                    "dds_polar_select",
                    dds.name,
                    start - clock.origin,
                    duration,
                    int(profile),
                )
            )
            dds._launch("profile", clock.cursor)


class _PolarDDS:
    def __init__(self, clock, name, device):
        self.clock, self.name, self.device = clock, name, device
        self.sysclk = device.sysclk_hz
        self.bus = SimpleNamespace(ref_period_mu=device.spi_ref_period_mu, channel=0)
        self.sync_data = SimpleNamespace(io_update_delay=0)
        self.chip_select = 4
        self.sw = _PolarSwitch(clock, name, "dds_", device.grid_mu)
        self.io_update = _PolarUpdate(self)
        self.cpld = _PolarProfile(self)
        self.profiles, self.memory = {}, [None] * 1024
        self.selected_profile = 0
        self.active_cfr = self.staged_cfr = (0, 0, 0, 0)
        self.held_clear, self.released = False, False
        self.ftw = 0
        self.matched_latency = 0

    def init(self):
        if not self.clock.verifying_initialization:
            _fail("DDS init is only permitted in device initialization")
        self.clock.initialization_calls.append(("init", self.name))

    def _preload(self, duration):
        if not self.clock.awaiting_origin or self.sw.is_on:
            _fail("POWASF register/RAM writes must remain in RF-off preload")
        self.clock.cursor += duration

    def set_cfr1(
        self, ram_enable=0, ram_destination=0, phase_clear=0, phase_autoclear=0
    ):
        self._preload(88 * self.bus.ref_period_mu)
        if phase_autoclear:
            _fail("Continuous NCO prohibits phase_autoclear")
        self.staged_cfr = (ram_enable, ram_destination, phase_clear, phase_autoclear)

    def set_cfr2(self, asf_profile_enable=1, matched_latency_enable=0):
        self._preload(88 * self.bus.ref_period_mu)
        if asf_profile_enable or matched_latency_enable != 1:
            _fail("POWASF requires disabled profile ASF and declared matched latency")
        self.matched_latency = matched_latency_enable

    def _commit_preload(self):
        if not self.staged_cfr[2]:
            _fail("Preload IO_UPDATE must keep phase_clear asserted")
        self.active_cfr = self.staged_cfr
        self.held_clear, self.released = True, False

    def set_ftw(self, value):
        self._preload(88 * self.bus.ref_period_mu)
        if not self.active_cfr[2]:
            _fail("FTW preload must hold NCO clear")
        self.ftw = _signed(value)

    def set_profile_ram(self, start, end, step=1, profile=0, mode=1):
        self._preload(156 * self.bus.ref_period_mu)
        if not 0 <= start <= end < 1024 or not 1 <= step <= 65535 or mode not in (0, 1):
            _fail("Invalid resident RAM profile descriptor")
        self.profiles[int(profile)] = (int(start), int(end), int(step), int(mode))

    def write_ram(self, words):
        self._preload((67 * len(words) + 22) * self.bus.ref_period_mu)
        if self.active_cfr[0] or not self.active_cfr[2]:
            _fail("RAM preload requires disabled RAM and held phase clear")
        start, end, _, _ = self.profiles[self.selected_profile]
        if len(words) != end - start + 1:
            _fail("RAM upload length differs from selected profile")
        self.memory[start : end + 1] = [int(w) & 0xFFFFFFFF for w in words]

    def _words(self):
        if self.selected_profile not in self.profiles:
            _fail("Selected RAM profile has no descriptor")
        start, end, _, _ = self.profiles[self.selected_profile]
        words = self.memory[start : end + 1]
        if any(w is None for w in words):
            _fail("Selected RAM profile has missing samples")
        return words

    def _launch(self, trigger, timestamp):
        if self.active_cfr != (1, 3, 0, 0) or self.matched_latency != 1:
            _fail("Timed playback lost continuous NCO POWASF mode")
        words = self._words()
        _, _, step, mode = self.profiles[self.selected_profile]
        prefix = "ram_profile_" if trigger == "profile" else "ram_"
        amp_latency = getattr(self.device, prefix + "latency_mu")
        phase_latency = (
            self.device.ram_profile_phase_latency_mu
            if trigger == "profile"
            else self.device.ram_polar_phase_latency_mu
        )
        self.clock.ram_effects.append(
            {
                "signal": self.name,
                "trigger": trigger,
                "launch_mu": timestamp - self.clock.origin,
                "profile": self.selected_profile,
                "ftw": self.ftw,
                "amplitude_mu": timestamp - self.clock.origin + amp_latency,
                "phase_mu": timestamp - self.clock.origin + phase_latency,
                "step": step,
                "mode": mode,
                "asf": [(w >> 2) & 0x3FFF for w in words],
                "pow": [(w >> 16) & 0xFFFF for w in words],
            }
        )


@contextmanager
def _fake(clock, device_map):
    from taiyidsl.backends.artiq.mock_drivers import _MockRAMUpdate

    class EnvExperiment:
        def get_device(self, name):
            return device_map[name]

    modules = {
        name: ModuleType(name)
        for name in (
            "artiq",
            "artiq.experiment",
            "artiq.coredevice",
            "artiq.coredevice.ad9910",
            "artiq.coredevice.ttl",
        )
    }
    modules["artiq.experiment"].__dict__.update(
        EnvExperiment=EnvExperiment,
        kernel=lambda f: f,
        at_mu=clock.at_mu,
        now_mu=lambda: clock.cursor,
    )
    modules["artiq.coredevice.ad9910"].RAM_DEST_POWASF = 3
    modules["artiq.coredevice.ad9910"].RAM_DEST_ASF = 2
    modules["artiq.coredevice.ad9910"].RAM_MODE_RAMPUP = 1
    modules["artiq.coredevice.ttl"].TTLOut = (_PolarUpdate, _MockRAMUpdate)
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def verify_polar(compiled, source):
    from taiyidsl.backends.artiq._contracts import _devices, _point_end
    from taiyidsl.backends.artiq.mock_drivers import _DriverClock, _MockCore, _MockDDS
    from taiyidsl.backends.artiq.validation_polar_output import verify_polar_output
    from taiyidsl.backends.artiq.verification_common import _verify_host_order

    compiled.assert_integrity()
    output_verification = verify_polar_output(compiled)
    ast.parse(source)
    clock = _DriverClock()
    clock.tick_seconds = float(compiled.setup.tick_seconds)
    clock.rtio_coarse_mu = compiled.setup.rtio_coarse_mu
    devices = _devices(compiled)
    device_map = {"core": _MockCore(clock, clock.tick_seconds, clock.rtio_coarse_mu)}
    buses, updates, chips = {}, {}, {}
    for index, (name, device) in enumerate(devices.items()):
        if device.kind == "dds":
            dds = (_PolarDDS if device.ram_destination == "powasf" else _MockDDS)(
                clock, name, device
            )
            if device.ram_destination != "powasf":
                dds.cpld.proto_rev = device.urukul_proto_rev or 9
            dds.bus.channel = buses.setdefault(device.bus or name, len(buses))
            dds.chip_select = 4 + chips.get(dds.bus.channel, 0)
            chips[dds.bus.channel] = dds.chip_select - 3
            dds.io_update.target_o = updates.setdefault(
                device.update_group or name, len(updates) + 1
            )
            device_map[name] = dds
            dds.sw.target_o = 1000 + index
        else:
            device_map[name] = _PolarSwitch(clock, name, "ttl_", device.grid_mu)
            device_map[name].target_o = 1000 + index
    execution = device_execution_plan(compiled)
    traces, reports = [], []
    with _fake(clock, device_map):
        namespace = {"__name__": "generated_polar_offline_check"}
        exec(compile(source, "<generated-polar-artiq>", "exec"), namespace)  # noqa: S102 -- trusted generated program
        instance = namespace["GeneratedExperiment"]()
        instance.build()
        instance.prepared_shared_resources = tuple(
            execution["prepared_shared_resources_required"]
        )
        instance._validate_target()
        clock.verifying_initialization = True
        try:
            instance.initialize_devices()
        finally:
            clock.verifying_initialization = False
        expected_init = []
        for name in sorted(devices):
            if name in execution["initialize_dds"]:
                expected_init.append(("init", name))
            idle = "on" if getattr(devices[name], "ttl_idle_level", 0) else "off"
            expected_init.append((idle, name))
        if clock.initialization_calls != expected_init:
            _fail("Generated initialization differs from used-device plan")
        for point in compiled.points:
            clock.events, clock.ram_effects, clock.nco_epochs = [], [], []
            clock.square_effects = []
            clock.rtio_events, clock.spi_busy = [], {}
            clock.planned_times = {int(e.start_mu) for e in point.physical_events} | {
                _point_end(point)
            }
            square_names = {
                e.signal for e in point.physical_events if e.kind == "dds_prepare"
            }
            for name in square_names:
                device = devices[name]
                device_map[name].square_group_members = [
                    device_map[other]
                    for other in sorted(square_names)
                    if other == name
                    or (
                        device.update_group is not None
                        and devices[other].update_group == device.update_group
                    )
                ]
            instance._execute_point(point.index)
            expected = _expected_mixed_events(compiled, point, devices)
            if clock.events != expected:
                _fail("Executed POWASF events differ from compiled submission plan")
            validate_channel_order(clock.rtio_events, coarse_mu=clock.rtio_coarse_mu)
            validate_sed(
                [t for t, _ in clock.rtio_events],
                coarse_mu=clock.rtio_coarse_mu,
                lane_count=compiled.setup.rtio_lane_count,
            )
            _verify_state(point, devices, device_map, clock)
            _verify_other_paths(compiled, point, device_map, clock)
            endpoint = _point_end(point)
            if (
                clock.cursor - clock.origin != endpoint
                or clock.wait_endpoint != endpoint
            ):
                _fail("Generated end/wait differs from compiled shot")
            trace = dict(
                events=clock.events,
                ram_effects=clock.ram_effects,
                square_effects=clock.square_effects,
                nco_epochs=clock.nco_epochs,
                end_mu=endpoint,
                wait_mu=clock.wait_endpoint,
            )
            traces.append(trace)
            reports.append(
                dict(
                    point=point.index,
                    status="passed",
                    events=len(clock.events),
                    end_mu=endpoint,
                    trace_sha256=hashlib.sha256(
                        json.dumps(trace, sort_keys=True).encode()
                    ).hexdigest(),
                )
            )
        host_order = _verify_host_order(instance, compiled)
    return {
        "offline_export_check": "passed",
        "python_syntax": "passed",
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
        "points": reports,
        "host_order": host_order,
        "initialization": {"status": "passed", **execution},
        "polar_output": output_verification,
        "limitations": [
            "POWASF phase/amplitude effects use explicit declared pipeline contracts",
            "CPLD pin latch, analog phase, CPU/FIFO throughput "
            "and hardware are not measured",
        ],
    }, traces


def _other_point(point, devices):
    return SimpleNamespace(
        physical_events=[
            e
            for e in point.physical_events
            if devices[e.signal].kind == "dds"
            and devices[e.signal].ram_destination != "powasf"
        ],
        origin_mu=point.origin_mu,
    )


def _expected_mixed_events(compiled, point, devices):
    from taiyidsl.backends.artiq.verification_common import _expected_events

    regular = iter(_expected_events(compiled, _other_point(point, devices)))
    result = []
    for event in point.physical_events:
        if event.kind not in _KINDS:
            continue
        device = devices[event.signal]
        if device.kind == "dds" and device.ram_destination != "powasf":
            result.append(next(regular))
            continue
        kind = (
            "update_off"
            if event.kind in {"dds_nco_epoch_off", "dds_polar_commit_off"}
            else event.kind
        )
        row = (kind, event.signal, event.start_mu)
        if event.kind == "dds_polar_select":
            row += (event.duration_mu, event.params["polar_profile"])
        result.append(row)
    return result


def _verify_other_paths(compiled, point, device_map, clock):
    """Reuse independent buffered Square/ASF models on the combined clock."""
    from taiyidsl.backends.artiq._contracts import _words
    from taiyidsl.backends.artiq.verification_common import _verify_ram_effects

    other = _other_point(point, compiled.setup.devices)
    names = {e.signal for e in other.physical_events}
    expected_square = []
    expected_epochs = {}
    for event in other.physical_events:
        p = event.params
        if event.kind == "dds_effect":
            ftw, _, asf = _words(event, compiled.setup.devices[event.signal])
            expected_square.append(
                square_effect_record(
                    event.signal, event.start_mu, ftw, p["square_pow"], asf
                )
            )
        if "ftw" in p:
            expected_epochs[event.signal] = dict(
                signal=event.signal,
                release_mu=p["nco_epoch_release_mu"],
                effect_mu=p["nco_epoch_effect_mu"],
                ftw=_signed(p["ftw"]),
                pow=p["nco_initial_pow"],
            )

    def key(e):
        return (e["start_mu"], e["signal"])

    if sorted(clock.square_effects, key=key) != sorted(expected_square, key=key):
        _fail("Committed Square output effects differ from the requested timeline")
    epochs = [e for e in clock.nco_epochs if e["signal"] in names]
    if sorted(epochs, key=lambda e: e["signal"]) != sorted(
        expected_epochs.values(), key=lambda e: e["signal"]
    ):
        _fail("Mixed DDS NCO release or initial reference differs from plan")
    filtered_clock = SimpleNamespace(
        ram_effects=[e for e in clock.ram_effects if e["signal"] in names],
        events=[
            e for e in clock.events if isinstance(e, dict) and e["signal"] in names
        ],
    )
    _verify_ram_effects(compiled, other, filtered_clock)
    if any(
        device_map[n].square_staged is not None
        or device_map[n].ram_pow_pending
        or device_map[n].io_update.high_mu is not None
        for n in names
    ):
        _fail("Shot ends with an incomplete mixed DDS preparation or commit")


def _verify_state(point, devices, device_map, clock):
    expected_epochs = []
    polar_names = set(_parameters(point))
    for name, params in _parameters(point).items():
        dds = device_map[name]
        expected_epochs.append(
            dict(
                signal=name,
                release_mu=params["nco_epoch_release_mu"],
                effect_mu=params["nco_epoch_effect_mu"],
                ftw=_signed(params["ftw"]),
                pow=params["nco_initial_pow"],
            )
        )
        if dds.io_update.high_mu is not None:
            _fail("Shot ends with an incomplete IO_UPDATE")
        for bank in params["polar_bank"]:
            profile = bank["profile"]
            descriptor = (bank["start"], bank["end"], bank["step"], bank["mode"])
            if dds.profiles.get(profile) != descriptor:
                _fail("Uploaded RAM descriptor differs from resident bank")
            if dds.memory[bank["start"] : bank["end"] + 1] != [
                int(w) & 0xFFFFFFFF for w in bank["words"]
            ]:
                _fail("Uploaded POWASF RAM samples differ from unchanged envelope bank")
    if sorted(
        (e for e in clock.nco_epochs if e["signal"] in polar_names),
        key=lambda e: e["signal"],
    ) != sorted(expected_epochs, key=lambda e: e["signal"]):
        _fail("NCO epoch/initial phase differs from declared reference")
    effects = [e for e in point.physical_events if e.kind == "dds_polar_effect"]
    effects.sort(key=lambda e: (e.params["polar_commit_mu"], e.signal))
    actual = sorted(
        (e for e in clock.ram_effects if e["signal"] in polar_names),
        key=lambda e: (e["launch_mu"], e["signal"]),
    )
    if len(actual) != len(effects):
        _fail("Missing or extra POWASF playback/frame launch")
    for event, effect in zip(effects, actual):
        params = event.params
        bank = next(
            b for b in params["polar_bank"] if b["profile"] == params["polar_profile"]
        )
        if (
            effect["signal"] != event.signal
            or effect["profile"] != params["polar_profile"]
            or effect["ftw"] != _signed(params["ftw"])
        ):
            _fail("POWASF selected profile/FTW differs from requested effect")
        if effect["asf"] != list(bank["asf"]) or effect["pow"] != list(
            bank["pow_samples"]
        ):
            _fail("Decoded POWASF amplitude/phase samples differ from bank")
        interval = (
            bank["step"] * 4 / devices[event.signal].sysclk_hz / clock.tick_seconds
        )
        prefix = params["polar_phase_prefix_samples"]
        carrier = effect["phase_mu"] + prefix * interval
        if carrier != params["carrier_effect_mu"] or carrier != event.start_mu:
            _fail("POWASF phase sample compensation misses the strict boundary")
        if not params["polar_frame_only"] and effect["amplitude_mu"] != event.start_mu:
            _fail("POWASF amplitude pipeline misses the original gate boundary")
        if any(p != params["incoming_pow"] for p in effect["pow"][:prefix]) or any(
            p != params["ram_pow"] for p in effect["pow"][prefix:]
        ):
            _fail("POWASF phase samples change before/after the requested boundary")
