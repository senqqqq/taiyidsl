"""Independent offline driver emulator; no ARTIQ installation or hardware IO."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from types import ModuleType

from taiyidsl.backends.artiq._contracts import ExportError, square_effect_record


class _DriverClock:
    def __init__(self):
        self.cursor = 0
        self.origin = 0
        self.events = []
        self.square_effects = []
        self.ram_effects = []
        self.rtio_coarse_mu = 8
        self.awaiting_origin = True
        self.wait_endpoint = None
        self.verifying_initialization = False
        self.initialization_calls = []
        self.planned_times = set()
        self.spi_busy = {}
        self.rtio_events = []
        self.nco_epochs = []

    def reset(self):
        self.cursor = (
            125_001  # Deliberately unaligned, to exercise exported origin alignment.
        )
        self.origin = self.cursor
        self.awaiting_origin = True
        self.wait_endpoint = None

    def at_mu(self, timestamp):
        if (
            not self.awaiting_origin
            and int(timestamp - self.origin) not in self.planned_times
        ):
            raise ExportError(
                "Generated code uses a timestamp outside the certified submission plan"
            )
        self.cursor = int(timestamp)
        if self.awaiting_origin:
            self.origin = self.cursor
            self.awaiting_origin = False

    def spi(self, device, duration, offsets):
        if self.awaiting_origin:
            return
        bus = device.bus or "unknown"
        if self.cursor < self.spi_busy.get(bus, -1):
            raise ExportError("Generated SPI transactions overlap on the same bus")
        self.spi_busy[bus] = self.cursor + duration
        self.rtio_events.extend(
            (self.cursor + offset, ("spi", bus)) for offset in offsets
        )

    def edge(self, channel):
        if not self.awaiting_origin and not self.verifying_initialization:
            self.rtio_events.append((self.cursor, channel))


class _MockCore:
    def __init__(self, clock, ref_period=1e-9, ref_multiplier=8):
        self.clock = clock
        self.ref_period = ref_period
        self.ref_multiplier = ref_multiplier

    def reset(self):
        self.clock.reset()

    def break_realtime(self):
        # A frozen virtual hardware counter at 1 mu; CPU execution is not modeled.
        self.clock.cursor = max(self.clock.cursor, 125_001)

    def wait_until_mu(self, timestamp):
        if timestamp < self.clock.cursor:
            raise ExportError("Invalid wait endpoint")
        self.clock.wait_endpoint = int(timestamp - self.clock.origin)


class _MockSwitch:
    def __init__(self, clock, name, prefix, grid_mu=1):
        self.clock, self.name, self.prefix = clock, name, prefix
        self.grid_mu = grid_mu
        self.is_on = False
        self.ram_dds = None

    def _edge(self, edge):
        self.is_on = edge == "on"
        if self.clock.verifying_initialization:
            self.clock.initialization_calls.append((edge, self.name))
            return
        if self.clock.awaiting_origin:
            if edge != "off":
                raise ExportError("RF output must remain off during RAM preparation")
            return
        if self.clock.cursor % self.grid_mu:
            raise ExportError(f"Switch event violates {self.name} timing grid")
        dds = self.ram_dds
        if dds is not None and dds.ram_enabled:
            if dds.last_ram_launch is None:
                raise ExportError("RAM output gate has no preceding launch")
            expected = dds.last_ram_launch + dds.device.ram_latency_mu
            if edge == "off":
                expected += round(
                    (len(dds.ram_words) - 1)
                    * dds.ram_profile[2]
                    * 4
                    / dds.sysclk
                    / self.clock.tick_seconds
                )
            if (
                dds.continuous
                or getattr(dds.device, "ram_phase_latency_mu", None) is not None
            ):
                candidates = []
                for effect in self.clock.ram_effects:
                    if effect["signal"] != self.name:
                        continue
                    endpoint = effect["start_mu"]
                    if edge == "off":
                        endpoint += round(
                            (len(effect["ram_asf"]) - 1)
                            * effect["ram_step"]
                            * 4
                            / dds.sysclk
                            / self.clock.tick_seconds
                        )
                    candidates.append(endpoint + self.clock.origin)
                valid = self.clock.cursor in candidates
            else:
                valid = self.clock.cursor == expected
            if not valid:
                raise ExportError("RAM gate violates launch-to-output pipeline latency")
        self.clock.events.append(
            {
                "kind": self.prefix + edge,
                "signal": self.name,
                "start_mu": self.clock.cursor - self.clock.origin,
            }
        )
        self.clock.edge((self.prefix, self.name))

    def on(self):
        self._edge("on")

    def off(self):
        self._edge("off")


class _MockDDS:
    def __init__(self, clock, name, device):
        self.clock, self.name, self.device = clock, name, device
        self.sysclk = float(getattr(device, "sysclk_hz", 1e9))
        self.grid_mu = int(getattr(device, "grid_mu", 1))
        self.config_grid_mu = int(getattr(device, "config_grid_mu", 8))
        self.bus = type("MockSPI", (), {"ref_period_mu": device.spi_ref_period_mu})()
        self.sw = _MockSwitch(clock, name, "dds_", self.grid_mu)
        self.sw.ram_dds = self
        self.last_ram_launch = None
        self.asf_profile_enable = 1
        self.staged_asf = 0
        self.committed_zero = False
        self.ram_memory = [None] * 1024
        self.ram_profiles = {}
        self.committed_profiles = {}
        self.committed_ram_enabled = 0
        self.matched_latency_enable = 0
        self.rearm_start = None
        self.rearm_updates = 0
        self.rearm_registers = set()
        self.ram_enabled = 0
        self.phase_autoclear = 0
        self.phase_clear = 0
        self.committed_phase_clear = 0
        self.nco_released = False
        self.ram_destination = 0
        self.selected_profile = 7
        self.ftw = self.pow = 0
        self.committed_pow = 0
        self.ram_pow_pending = False
        self.ram_pow_ready_mu = None
        self.chip_select = 4
        self.sync_data = type("MockSync", (), {"io_update_delay": 0})()
        self.io_update = _MockRAMUpdate(self)
        self.cpld = _MockRAMProfile(self)
        self.square_staged = None
        self.square_ready_mu = None
        self.square_group_members = [self]
        self.square_active = None

    @property
    def continuous(self):
        return self.device.nco_mode == "continuous"

    @property
    def ram_profile(self):
        return self.ram_profiles.get(self.selected_profile)

    @property
    def ram_words(self):
        if self.ram_profile is None:
            return []
        start, end, _ = self.ram_profile
        return self.ram_memory[start : end + 1]

    def _register_write(self):
        if self.sw.is_on:
            raise ExportError("RAM register updates require RF off")
        if not self.clock.awaiting_origin and self.rearm_start is None:
            raise ExportError("RAM parameter update outside a rearm window")

    def _preload_only(self):
        if not self.clock.awaiting_origin or self.sw.is_on:
            raise ExportError("RAM writes must occur before the timed shot with RF off")

    def set_cfr1(
        self, ram_enable=0, ram_destination=0, phase_autoclear=0, phase_clear=0
    ):
        if self.continuous and not self.clock.awaiting_origin:
            raise ExportError(
                "Continuous NCO cannot rewrite CFR1 during the timed shot"
            )
        if self.continuous and self.clock.awaiting_origin and phase_clear:
            self.nco_released = False
        if not self.clock.awaiting_origin and not ram_enable:
            if self.rearm_start is not None:
                raise ExportError("Repeated RAM disable in one rearm window")
            self.rearm_start = self.clock.cursor
            self.rearm_updates = 0
            self.rearm_registers = set()
        self._register_write()
        if not self.clock.awaiting_origin and ram_enable:
            if self.rearm_updates != 2 or self.rearm_registers != {"ftw", "pow"}:
                raise ExportError(
                    "RAM rearm must commit phase/frequency while disabled"
                )
            self.clock.events.append(
                {
                    "kind": "dds_ram_rearm",
                    "signal": self.name,
                    "start_mu": self.rearm_start - self.clock.origin,
                    "duration_mu": self.device.ram_rearm_duration_mu,
                    "ftw": self.ftw,
                    "ram_pow": self.pow,
                    "ram_profile": self.selected_profile,
                }
            )
            # Emulator uses the declared driver contract, not measured SPI timing.
            self.clock.cursor = max(
                self.clock.cursor, self.rearm_start + self.device.ram_rearm_duration_mu
            )
            self.rearm_start = None
        self.ram_enabled = ram_enable
        self.ram_destination = ram_destination
        self.phase_autoclear = phase_autoclear
        self.phase_clear = phase_clear
        if not ram_enable:
            self.last_ram_launch = None

    def set_cfr2(self, asf_profile_enable=1, matched_latency_enable=0):
        self._preload_only()
        if matched_latency_enable not in (0, 1):
            raise ExportError("Invalid RAM matched-latency mode")
        self.matched_latency_enable = matched_latency_enable
        self.asf_profile_enable = asf_profile_enable

    def set_asf(self, value):
        self._preload_only()
        self.staged_asf = value

    def set_profile_ram(self, start, end, step=1, profile=0, mode=1):
        self._preload_only()
        if (
            not 0 <= start < end < 1024
            or not 1 <= step <= 65535
            or not 0 <= profile <= 7
            or mode != 1
        ):
            raise ExportError("Invalid RAM profile")
        self.ram_profiles[profile] = (start, end, step)

    def write_ram(self, words):
        self._preload_only()
        if (
            self.ram_enabled
            or self.committed_ram_enabled
            or self.ram_profile is None
            or self.committed_profiles.get(self.selected_profile) != self.ram_profile
        ):
            raise ExportError(
                "RAM must be disabled and profile configured before loading"
            )
        if len(words) != self.ram_profile[1] - self.ram_profile[0] + 1:
            raise ExportError("RAM upload length differs from profile")
        if any(not -(1 << 31) <= word < (1 << 31) for word in words):
            raise ExportError("RAM words must be signed int32")
        start, end, _ = self.ram_profile
        self.ram_memory[start : end + 1] = [int(word) & 0xFFFFFFFF for word in words]

    def set_ftw(self, word):
        self._register_write()
        self.ftw = int(word)
        self.rearm_registers.add("ftw")

    def set_pow(self, word):
        if not self.clock.awaiting_origin and self.rearm_start is None:
            boundary = (
                self.continuous
                or getattr(self.device, "ram_phase_latency_mu", None) is not None
            )
            if (
                (self.sw.is_on and not boundary)
                or not self.ram_enabled
                or not self.committed_ram_enabled
                or self.phase_autoclear != int(not self.continuous)
                or (self.continuous and (not self.nco_released or self.phase_clear))
                or not self.matched_latency_enable
                or self.device.ram_phase_update_duration_mu is None
            ):
                raise ExportError(
                    "RAM phase preparation requires the declared enabled RAM mode"
                )
            if boundary and self.ram_pow_pending:
                raise ExportError("RAM preparation overwrites an uncommitted POW")
            self.pow = int(word)
            self.ram_pow_pending = True
            self.ram_pow_ready_mu = self.clock.cursor + 52 * self.bus.ref_period_mu
            self.clock.spi(
                self.device, 52 * self.bus.ref_period_mu, (0, self.bus.ref_period_mu)
            )
            self.clock.events.append(
                {
                    "kind": "dds_ram_phase",
                    "signal": self.name,
                    "start_mu": self.clock.cursor - self.clock.origin,
                    "duration_mu": self.device.ram_phase_update_duration_mu,
                    "ftw": self.ftw,
                    "ram_pow": self.pow,
                    "ram_profile": self.selected_profile,
                }
            )
            self.clock.cursor = (
                self.ram_pow_ready_mu
                if boundary
                else self.clock.cursor + self.device.ram_phase_update_duration_mu
            )
            return
        self._register_write()
        self.pow = int(word)
        self.rearm_registers.add("pow")

    def init(self):
        if not self.clock.verifying_initialization:
            raise ExportError("DDS initialization is forbidden inside a shot")
        self.clock.initialization_calls.append(("init", self.name))

    def write64(self, address, high, low):
        """Model the profile buffer independently of the committed output."""
        clock = self.clock
        if (
            address != 0x15
            or self.selected_profile != 7
            or self.ram_enabled
            or self.phase_autoclear != int(not self.continuous)
            or self.asf_profile_enable != 1
            or self.matched_latency_enable != 1
        ):
            raise ExportError(
                "Square preparation requires matched single-tone profile seven"
            )
        if clock.awaiting_origin and self.continuous:
            if not self.phase_clear:
                raise ExportError(
                    "Initial Square profile must be loaded while NCO is held clear"
                )
            self.square_staged = (int(low), int(high) & 0xFFFF, int(high) >> 16)
            self.ftw, self.pow, self.staged_asf = self.square_staged
            return
        if clock.awaiting_origin or clock.cursor % self.config_grid_mu:
            raise ExportError("Square preparation outside its timed configuration grid")
        if self.square_staged is not None:
            raise ExportError("Square preparation overwrites an uncommitted profile")
        ftw, pow_, asf = int(low), int(high) & 0xFFFF, int(high) >> 16
        if not -(1 << 31) <= ftw < (1 << 31) or not 0 <= asf <= 0x3FFF:
            raise ExportError("Invalid Square profile word")
        self.square_staged = (ftw, pow_, asf)
        self.square_ready_mu = clock.cursor + 156 * self.bus.ref_period_mu
        clock.spi(
            self.device,
            156 * self.bus.ref_period_mu,
            tuple(k * self.bus.ref_period_mu for k in (0, 1, 20, 21, 88, 89)),
        )
        clock.events.append(
            {
                "kind": "dds_prepare",
                "signal": self.name,
                "start_mu": clock.cursor - clock.origin,
                "duration_mu": self.device.square_prepare_duration_mu,
                "ftw": ftw,
                "square_pow": pow_,
                "asf": asf,
            }
        )
        # Derive the cursor cost from the actual driver's write64 SPI sequence,
        # never from the duration asserted by the plan under test.
        clock.cursor = self.square_ready_mu

    def set_mu(self, ftw, pow_=0, asf=0x3FFF, phase_mode=-1, ref_time_mu=-1):
        if self.clock.verifying_initialization:
            raise ExportError("Unexpected DDS register write during initialization")
        if self.clock.cursor % self.config_grid_mu or ref_time_mu % self.grid_mu:
            raise ExportError(
                "DDS configuration or phase reference violates timing grid"
            )
        if phase_mode != 2:
            raise ExportError("Expected tracking phase mode")
        if self.selected_profile != 7:
            raise ExportError("Single-tone output requires profile seven after RAM")
        if self.asf_profile_enable != 1:
            raise ExportError("Single-tone output requires profile amplitude source")
        if (
            not -(1 << 31) <= ftw < (1 << 31)
            or not 0 <= pow_ < (1 << 16)
            or not 0 <= asf <= 0x3FFF
        ):
            raise ExportError("Invalid DDS tuning word")
        self.clock.events.append(
            {
                "kind": "dds_config",
                "signal": self.name,
                "start_mu": self.clock.cursor - self.clock.origin,
                "duration_mu": int(self.device.config_duration_mu),
                "ftw": ftw,
                "pow": pow_,
                "asf": asf,
                "ref_time_mu": int(ref_time_mu - self.clock.origin),
            }
        )
        # Two CFR1 writes, a 64-bit profile write, then an 8-mu update;
        # the installed tracking driver clears the fine timestamp afterwards.
        self.clock.cursor += (
            332 * self.bus.ref_period_mu + 8 + (self.sync_data.io_update_delay // 8) * 8
        )


class _MockRAMProfile:
    def __init__(self, dds):
        self.dds = dds
        self.proto_rev = getattr(dds.device, "urukul_proto_rev", None) or 9

    def set_profile(self, channel, profile):
        self.dds._register_write()
        if self.dds.ram_enabled or self.dds.committed_ram_enabled:
            raise ExportError("Select a RAM profile only while RAM is disabled")
        if channel != self.dds.chip_select - 4:
            raise ExportError("RAM profile selects a different DDS channel")
        if not 0 <= profile <= 7:
            raise ExportError("Invalid RAM profile selection")
        self.dds.selected_profile = profile


class _MockRAMUpdate:
    def __init__(self, dds):
        self.dds = dds
        self.high_mu = None
        self.last_fall_mu = None
        self.target_o = 0
        self.epoch_high = False

    def on(self):
        dds, clock = self.dds, self.dds.clock
        clock.edge(("update", self.target_o))
        if dds.continuous and not dds.nco_released:
            if self.high_mu is not None:
                raise ExportError("Repeated NCO epoch rise")
            for member in dds.square_group_members:
                if (
                    not member.continuous
                    or member.phase_clear
                    or not member.committed_phase_clear
                    or member.phase_autoclear
                    or member.sw.is_on
                ):
                    raise ExportError(
                        "NCO epoch must release a held-clear accumulator with RF off"
                    )
                member.committed_phase_clear = 0
                member.nco_released = True
                clock.nco_epochs.append(
                    dict(
                        signal=member.name,
                        release_mu=clock.cursor - clock.origin,
                        effect_mu=clock.cursor
                        - clock.origin
                        + member.device.nco_epoch_latency_mu,
                        ftw=member.ftw,
                        pow=member.committed_pow,
                    )
                )
            self.high_mu = clock.cursor
            self.epoch_high = True
            clock.events.append(
                dict(
                    kind="dds_nco_epoch",
                    signal=dds.name,
                    start_mu=clock.cursor - clock.origin,
                )
            )
            return
        if dds.ram_enabled:
            if (
                not dds.continuous
                and getattr(dds.device, "ram_phase_latency_mu", None) is None
            ):
                raise ExportError("Nonblocking RAM commit lacks a carrier contract")
            if self.high_mu is not None:
                raise ExportError("Repeated RAM IO_UPDATE rise")
            if self.last_fall_mu is not None and (
                clock.cursor < self.last_fall_mu + clock.rtio_coarse_mu
            ):
                raise ExportError("RAM IO_UPDATE requires a full coarse-cycle low")
            self.high_mu = clock.cursor
            self._launch(boundary=True)
            return
        if self.high_mu is not None:
            raise ExportError("Repeated Square IO_UPDATE rise")
        for member in dds.square_group_members:
            if (
                member.continuous
                and member.square_staged is None
                and member.square_active is not None
            ):
                continue
            if (
                member.square_staged is None
                or member.square_ready_mu > clock.cursor
                or member.ram_enabled
                or member.phase_autoclear != int(not member.continuous)
                or (
                    member.continuous
                    and (member.phase_clear or not member.nco_released)
                )
                or member.matched_latency_enable != 1
            ):
                raise ExportError(
                    "Square commit requires every group member fully prepared"
                )
        self.high_mu = clock.cursor
        clock.events.append(
            {
                "kind": "dds_commit",
                "signal": dds.name,
                "start_mu": clock.cursor - clock.origin,
            }
        )
        for member in dds.square_group_members:
            if member.square_staged is None:
                continue
            ftw, pow_, asf = member.square_staged
            member.square_active = member.square_staged
            clock.square_effects.append(
                square_effect_record(
                    member.name,
                    clock.cursor - clock.origin + member.device.square_latency_mu,
                    ftw,
                    pow_,
                    asf,
                )
            )
            member.square_staged = None
            member.square_ready_mu = None

    def off(self):
        dds, clock = self.dds, self.dds.clock
        if self.high_mu is None or clock.cursor - self.high_mu != 8:
            raise ExportError("IO_UPDATE must fall exactly 8 mu after its rise")
        self.high_mu = None
        self.last_fall_mu = clock.cursor
        clock.edge(("update", self.target_o))
        clock.events.append(
            {
                "kind": "dds_nco_epoch_off"
                if self.epoch_high
                else "dds_ram_commit_off"
                if dds.ram_enabled
                else "dds_commit_off",
                "signal": self.dds.name,
                "start_mu": clock.cursor - clock.origin,
            }
        )
        self.epoch_high = False

    def pulse_mu(self, duration):
        dds, clock = self.dds, self.dds.clock
        if duration != 8:
            raise ExportError("RAM IO_UPDATE pulse must last 8 mu")
        dds.committed_ram_enabled = dds.ram_enabled
        dds.committed_profiles = dict(dds.ram_profiles)
        dds.committed_pow = dds.pow
        dds.committed_phase_clear = dds.phase_clear
        if not clock.awaiting_origin:
            if not dds.ram_enabled and dds.rearm_start is not None:
                dds.rearm_updates += 1
                if dds.rearm_updates == 2 and dds.rearm_registers != {"ftw", "pow"}:
                    raise ExportError(
                        "RAM rearm commits before writing phase/frequency"
                    )
                clock.cursor += 8
                return
            self._launch(boundary=False)
        elif not dds.ram_enabled:
            dds.committed_zero = dds.asf_profile_enable == 0 and dds.staged_asf == 0
            if dds.square_staged is not None:
                dds.square_active = dds.square_staged
                dds.ftw, dds.committed_pow, _ = dds.square_staged
            dds.square_staged = None
            dds.square_ready_mu = None
            dds.ram_pow_pending = False
            dds.ram_pow_ready_mu = None
            self.last_fall_mu = None
        clock.cursor += 8

    def _launch(self, *, boundary):
        dds, clock = self.dds, self.dds.clock
        if (
            not dds.ram_enabled
            or dds.ram_destination != 2
            or dds.phase_autoclear != int(not dds.continuous)
            or (dds.continuous and (dds.phase_clear or not dds.nco_released))
        ):
            raise ExportError(
                "RAM launch must enable ASF playback with its declared NCO mode"
            )
        if (
            (dds.sw.is_on and not boundary)
            or not dds.ram_words
            or None in dds.ram_words
            or dds.ram_words[-1] != 0
        ):
            raise ExportError("RAM launch has missing data, gate or final zero")
        if not dds.committed_zero:
            raise ExportError("RAM staging must commit zero amplitude before launch")
        if boundary and (
            dds.matched_latency_enable != 1
            or (
                dds.ram_pow_ready_mu is not None and dds.ram_pow_ready_mu > clock.cursor
            )
        ):
            raise ExportError("RAM commit occurs before its POW preparation is ready")
        dds.committed_ram_enabled = dds.ram_enabled
        dds.committed_profiles = dict(dds.ram_profiles)
        dds.committed_pow = dds.pow
        dds.ram_pow_pending = False
        dds.ram_pow_ready_mu = None
        dds.last_ram_launch = clock.cursor
        event = {
            "kind": "dds_ram_start",
            "signal": dds.name,
            "start_mu": clock.cursor - clock.origin,
            "duration_mu": 0 if boundary else 8,
            "ftw": dds.ftw,
            "ram_pow": dds.committed_pow,
            "ram_step": dds.ram_profile[2],
            "ram_asf": [word >> 18 for word in dds.ram_words],
            "ram_profile": dds.selected_profile,
            "ram_start": dds.ram_profile[0],
            "ram_end": dds.ram_profile[1],
            "ram_matched_latency": dds.matched_latency_enable,
        }
        clock.events.append(event)
        if boundary:
            clock.ram_effects.append(
                {
                    **{
                        k: event[k]
                        for k in (
                            "signal",
                            "ftw",
                            "ram_pow",
                            "ram_step",
                            "ram_asf",
                            "ram_profile",
                        )
                    },
                    "start_mu": event["start_mu"] + dds.device.ram_latency_mu,
                    "carrier_effect_mu": event["start_mu"]
                    + (
                        dds.device.ram_pow_latency_mu
                        if dds.continuous
                        else dds.device.ram_phase_latency_mu
                    ),
                }
            )


@contextmanager
def _fake_artiq(clock, device_map):
    module_names = (
        "artiq",
        "artiq.experiment",
        "artiq.coredevice",
        "artiq.coredevice.ad9910",
        "artiq.coredevice.ttl",
        "artiq.coredevice.urukul",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    modules = {name: ModuleType(name) for name in module_names}

    class EnvExperiment:
        def get_device(self, name):
            return device_map[name]

    modules["artiq.experiment"].EnvExperiment = EnvExperiment
    modules["artiq.experiment"].kernel = lambda function: function
    modules["artiq.experiment"].at_mu = clock.at_mu
    modules["artiq.experiment"].now_mu = lambda: clock.cursor
    modules["artiq.experiment"].TInt64 = int
    modules["artiq.experiment"].TArray = lambda dtype, dimensions: object
    modules["artiq.coredevice.ad9910"].PHASE_MODE_TRACKING = 2
    modules["artiq.coredevice.ad9910"].RAM_DEST_ASF = 2
    modules["artiq.coredevice.ad9910"].RAM_MODE_RAMPUP = 1
    modules["artiq.coredevice.ttl"].TTLOut = _MockRAMUpdate
    modules["artiq.coredevice.urukul"].ProtoRev9 = _MockRAMProfile
    try:
        sys.modules.update(modules)
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
