"""Persistent, explicitly opened native Spectrum DDS20 execution.

The card starts once per configured session. Shots append bounded command packets
while the existing traps keep running. Completion uses committed DDS trigger
counts, never an empty FIFO or the AWG READY flag. Importing opens no hardware.
"""

from __future__ import annotations

import copy
import math
import time
from contextlib import contextmanager

from taiyidsl.backends.artiq_spectrum.dds import (
    AMPLITUDE_STEP,
    FREQUENCY_SLOPE_STEP,
    FREQUENCY_STEP_HZ,
    INITIAL_CONFIGURATION_COMMAND_RESERVE,
    LOGICAL_TICK_SECONDS,
    QUEUE_CAPACITY,
    dds_channel_cores,
    validate_dds_program,
)

from .awg import SpcmAdapter, SpectrumRuntimeError


def _close_number(actual, expected):
    return math.isfinite(float(actual)) and math.isclose(
        float(actual), float(expected), rel_tol=2e-12, abs_tol=1e-12
    )


def validate_dds_hardware(contract, program, observed):
    """Validate an offline shot against an explicit contract and read-only probe."""
    validate_dds_program(program)
    spec = program["card_spec"]
    for key in ("model", "serial_number"):
        if str(observed.get(key)) != str(contract.get(key)):
            raise SpectrumRuntimeError(f"DDS {key} differs from the contract")
    if observed.get("is_demo"):
        raise SpectrumRuntimeError("DDS hardware execution cannot use an SDK demo card")
    if not observed.get("dds_available") or not observed.get("dds20_available"):
        raise SpectrumRuntimeError("The native DDS20 feature/mode is unavailable")
    if observed.get("num_cores", 0) != 21:
        raise SpectrumRuntimeError("DDS20 dual-channel core topology differs")
    if observed.get("max_commands", 0) < program["command_count"]:
        raise SpectrumRuntimeError("DDS program exceeds the physical command queue")
    if observed.get("clock_mode") != "external_reference":
        raise SpectrumRuntimeError("DDS external reference clock is not configured")
    for key in ("sample_rate_hz", "reference_clock_hz"):
        if observed.get(key) != contract.get(key, 125_000_000):
            raise SpectrumRuntimeError(f"DDS {key} differs from its timing contract")
    firmware = contract.get("firmware")
    if firmware not in (None, "", "unmeasured") and firmware != observed.get(
        "firmware"
    ):
        raise SpectrumRuntimeError("DDS firmware differs from its timing contract")
    for key in (
        "model",
        "serial_number",
        "mode",
        "sample_rate_hz",
        "reference_clock_hz",
        "output_range_mv",
        "dds_routing",
        "dds_trigger_latency_ns",
        "dds_amplitude_slope_step_per_s",
    ):
        if spec.get(key) != contract.get(key):
            raise SpectrumRuntimeError(f"DDS program/contract {key} mismatch")
    if list(spec["active_channels"]) != list(contract["active_channels"]):
        raise SpectrumRuntimeError("DDS active channel contract mismatch")
    precision = {
        "frequency_step_hz": FREQUENCY_STEP_HZ,
        "frequency_slope_step_hz_per_s": FREQUENCY_SLOPE_STEP,
        "amplitude_step": AMPLITUDE_STEP,
    }
    amp_step = spec.get("dds_amplitude_slope_step_per_s")
    if amp_step is not None:
        precision["amplitude_slope_step_per_s"] = amp_step
    for key, expected in precision.items():
        if not _close_number(observed.get(key, float("nan")), expected):
            raise SpectrumRuntimeError(f"DDS {key} differs from the compiled profile")
    routing = dds_channel_cores(spec.get("dds_routing", "16/5"))
    actual_routes = observed.get("routing", {})
    for channel in spec["active_channels"]:
        actual = actual_routes.get(channel, actual_routes.get(str(channel)))
        if list(actual or ()) != list(routing[channel]):
            raise SpectrumRuntimeError("DDS core routing readback differs")
    return True


class SpcmDDSAdapter(SpcmAdapter):
    """One explicitly identified card, with continuous hold until stop/close.

    ``configure`` is idempotent only for an identical live contract. Changing a
    live configuration requires closing the session. Native driver calls are
    performed exclusively when the caller explicitly invokes these methods.
    """

    def __init__(self, serial_number, *, address=None, sdk=None, clock_session_id=None):
        super().__init__(
            serial_number, address=address, sdk=sdk, clock_session_id=clock_session_id
        )
        self._dds = None
        self._running = False
        self._hold = None
        self._program = None
        self._baseline = None
        self._last_count = None
        self._needs_initialization = False

    @contextmanager
    def _guard(self):
        try:
            yield
        except BaseException:
            try:
                self.stop()
            except Exception:
                # Preserve the original failure; callers must invalidate the
                # session even if a disconnected device cannot acknowledge STOP.
                pass
            raise

    def probe(self):
        card, sdk = self._open(), self._sdk
        clock = sdk.Clock(card)
        cores = card.get_i(sdk.SPC_DDS_NUM_CORES)
        fields = {
            "frequency_step_hz": "SPC_DDS_AVAIL_FREQ_STEP",
            "frequency_slope_step_hz_per_s": "SPC_DDS_AVAIL_FREQ_SLOPE_STEP",
            "amplitude_step": "SPC_DDS_AVAIL_AMP_STEP",
            "amplitude_slope_step_per_s": "SPC_DDS_AVAIL_AMP_SLOPE_STEP",
        }
        return {
            "model": card.product_name(),
            "serial_number": str(card.sn()),
            "memory_bytes": card.get_i(sdk.SPC_PCIMEMSIZE),
            "firmware": (
                f"{card.get_i(sdk.SPC_PCIVERSION):08x}/"
                f"{card.get_i(sdk.SPC_PCIMODULEVERSION):08x}"
            ),
            "is_demo": bool(card.get_i(sdk.SPC_MIINST_ISDEMOCARD)),
            "dds_available": bool(
                card.get_i(sdk.SPC_AVAILCARDMODES) & sdk.SPC_REP_STD_DDS
            ),
            "dds20_available": bool(card.ext_features() & sdk.SPCM_FEAT_EXTFW_DDS20),
            "num_cores": cores,
            "max_commands": card.get_i(sdk.SPC_DDS_QUEUE_CMD_MAX),
            "routing": {
                channel: [
                    core
                    for core in range(cores)
                    if card.get_i(sdk.SPC_DDS_CORES_ON_CH0 + channel) & (1 << core)
                ]
                for channel in (0, 1)
            },
            "sample_rate_hz": clock.sample_rate(),
            "reference_clock_hz": clock.reference_clock(),
            "clock_mode": "external_reference"
            if clock.mode() == sdk.SPC_CM_EXTREFCLOCK
            else "other",
            **{
                key: card.get_d(getattr(sdk, register))
                for key, register in fields.items()
            },
        }

    def configure(self, contract):
        with self._guard():
            if self._contract is not None:
                if self._contract != contract:
                    raise SpectrumRuntimeError(
                        "Cannot change a live DDS session contract"
                    )
                self._check_identity(contract)
                return
            self._check_identity(contract)
            card, sdk = self._card, self._sdk
            card.stop()
            card.timeout(10_000)
            if card.card_mode(sdk.SPC_REP_STD_DDS) != sdk.SPC_REP_STD_DDS:
                raise SpectrumRuntimeError("Device did not enter native DDS mode")
            for channel in sdk.Channels(card, card_enable=3):
                channel.enable(False)
            mask = sum(1 << channel for channel in contract["active_channels"])
            channels = sdk.Channels(card, card_enable=mask)
            for channel in channels:
                levels = contract["output_range_mv"]
                level = (
                    levels.get(str(channel.index), levels.get(channel.index))
                    if isinstance(levels, dict)
                    else levels
                )
                if channel.amp(level) != level or channel.offset(0) != 0:
                    raise SpectrumRuntimeError("DDS output setup readback mismatch")
                channel.enable(True)
            trigger = sdk.Trigger(card)
            for method, value in (
                ("or_mask", sdk.SPC_TMASK_EXT0),
                ("and_mask", 0),
                ("ext0_mode", sdk.SPC_TM_POS),
                ("ext0_coupling", sdk.COUPLING_DC),
                ("termination", int(contract.get("trigger_termination_ohm", 50) == 50)),
                ("ext0_level0", contract.get("trigger_level_mv", 1000)),
                ("delay", 0),
            ):
                if getattr(trigger, method)(value) != value:
                    raise SpectrumRuntimeError(
                        f"DDS trigger {method} readback mismatch"
                    )
            card.write_setup()
            self._dds = sdk.DDS(card, channels=channels)
            self._dds.reset()
            self._dds.data_transfer_mode(sdk.SPCM_DDS_DTM_SINGLE)
            if self._dds.get_data_transfer_mode() != sdk.SPCM_DDS_DTM_SINGLE:
                raise SpectrumRuntimeError("DDS SINGLE transfer mode was not accepted")
            for channel, cores in dds_channel_cores(
                contract.get("dds_routing", "16/5")
            ).items():
                self._dds.cores_on_channel(channel, *(1 << core for core in cores))
            self._dds.load_cores()
            self._dds.freq_ramp_stepsize(1)
            self._dds.amp_ramp_stepsize(1)
            if (
                self._dds.get_freq_ramp_stepsize() != 1
                or self._dds.get_amp_ramp_stepsize() != 1
            ):
                raise SpectrumRuntimeError("DDS ramp update divider was not accepted")
            self._contract = copy.deepcopy(contract)

    def _check_identity(self, contract):
        observed = self.probe()
        if contract.get("mode") != "dds" or observed["model"] != "M4i.6631-x8":
            raise SpectrumRuntimeError(
                "Only the M4i.6631 native DDS contract is supported"
            )
        for key in ("model", "serial_number", "sample_rate_hz", "reference_clock_hz"):
            if str(contract.get(key)) != str(observed[key]):
                raise SpectrumRuntimeError(f"DDS {key} differs from the hardware")
        if (
            observed["is_demo"]
            or not observed["dds_available"]
            or not observed["dds20_available"]
        ):
            raise SpectrumRuntimeError("A physical DDS20 capable card is required")
        if observed["clock_mode"] != "external_reference":
            raise SpectrumRuntimeError(
                "DDS external reference clock must be configured"
            )

    def _status(self):
        status = self._dds.status()
        errors = (
            self._sdk.SPCM_DDS_STAT_QUEUE_UNDERRUN
            | self._sdk.SPCM_DDS_STAT_QUEUE_OVERRUN
        )
        if status & errors:
            raise SpectrumRuntimeError("DDS command queue underrun/overrun")
        return status

    def _set_tones(self, tones, *, set_frequency=True, set_amplitude=True):
        for tone in tones:
            core = self._dds[tone["core"]]
            for setter, getter, value, minimum, maximum in (
                (
                    "freq",
                    "get_freq",
                    tone["frequency_hz"],
                    "SPC_DDS_AVAIL_FREQ_MIN",
                    "SPC_DDS_AVAIL_FREQ_MAX",
                ),
                (
                    "amp",
                    "get_amp",
                    tone["amplitude"],
                    "SPC_DDS_AVAIL_AMP_MIN",
                    "SPC_DDS_AVAIL_AMP_MAX",
                ),
                (
                    "freq_slope",
                    "get_freq_slope",
                    tone.get("frequency_slope_hz_per_s", 0.0),
                    "SPC_DDS_AVAIL_FREQ_SLOPE_MIN",
                    "SPC_DDS_AVAIL_FREQ_SLOPE_MAX",
                ),
                (
                    "amp_slope",
                    "get_amp_slope",
                    tone.get("amplitude_slope_per_s", 0.0),
                    "SPC_DDS_AVAIL_AMP_SLOPE_MIN",
                    "SPC_DDS_AVAIL_AMP_SLOPE_MAX",
                ),
            ):
                if (setter == "freq" and not set_frequency) or (
                    setter == "amp" and not set_amplitude
                ):
                    continue
                if (
                    not self._card.get_d(getattr(self._sdk, minimum))
                    <= value
                    <= self._card.get_d(getattr(self._sdk, maximum))
                ):
                    raise SpectrumRuntimeError(f"DDS {setter} exceeds physical limits")
                getattr(core, setter)(value)
                if not _close_number(getattr(core, getter)(), value):
                    raise SpectrumRuntimeError(
                        f"DDS {setter} quantization readback mismatch"
                    )

    def upload(self, program, segment_loader=None):
        del segment_loader
        with self._guard():
            if self._dds is None or self._armed or self._uploaded:
                raise SpectrumRuntimeError(
                    "DDS upload needs an idle configured session"
                )
            validate_dds_hardware(self._contract, program, self.probe())
            self._status()
            if self._dds.queue_cmd_count() != 0:
                raise SpectrumRuntimeError(
                    "Unexpected commands remain in the DDS queue"
                )
            if self._hold is not None and self._hold != program["initial_tones"]:
                raise SpectrumRuntimeError(
                    "DDS program does not start at the live hold state"
                )
            self._needs_initialization = not self._running
            initial_commands = (
                program["initialization_command_count"]
                if self._needs_initialization
                else 0
            )
            command_budget = program["command_count"] + initial_commands
            if command_budget > min(QUEUE_CAPACITY, self._dds.queue_cmd_max()):
                raise SpectrumRuntimeError(
                    "DDS initial hold and shot exceed the resident queue"
                )
            # spcm 1.13.4 exposes this through Card.get_i, not a DDS helper.
            # Manual Table 148/152: this is the driver list BEFORE transfer,
            # distinct from queue_cmd_count() on the hardware FIFO.
            software_counter = self._sdk.SPC_DDS_NUM_QUEUED_CMD_IN_SW
            pending = self._card.get_i(software_counter)
            allowed_setup = (
                INITIAL_CONFIGURATION_COMMAND_RESERVE
                if self._needs_initialization
                else 0
            )
            if not 0 <= pending <= allowed_setup:
                raise SpectrumRuntimeError(
                    "Unexpected commands remain in the DDS software queue"
                )
            count = self._dds.trg_count()
            if self._last_count is not None and count != self._last_count:
                raise SpectrumRuntimeError("Unexpected DDS trigger between shots")
            self._baseline = count + int(self._needs_initialization)
            if self._needs_initialization:
                self._dds.trg_src(self._sdk.SPCM_DDS_TRG_SRC_CARD)
                self._set_tones(program["initial_tones"])
                self._dds.exec_at_trg()
            for frame in program["frames"]:
                source = (
                    self._sdk.SPCM_DDS_TRG_SRC_CARD
                    if frame["trigger_source"] == "card"
                    else self._sdk.SPCM_DDS_TRG_SRC_TIMER
                )
                self._dds.trg_src(source)
                if frame["timer_mu"] is not None:
                    timer = frame["timer_mu"] * LOGICAL_TICK_SECONDS
                    self._dds.trg_timer(timer)
                    if not _close_number(self._dds.get_trg_timer(), timer):
                        raise SpectrumRuntimeError(
                            "DDS timer readback differs from compiled ticks"
                        )
                self._set_tones(
                    frame["tones"],
                    set_frequency=frame["set_frequency"],
                    set_amplitude=frame["set_amplitude"],
                )
                self._dds.exec_at_trg()
            pending = self._card.get_i(software_counter)
            if not 0 < pending <= command_budget:
                raise SpectrumRuntimeError(
                    "DDS software command count exceeds the compiled queue budget"
                )
            self._dds.write_to_card()
            self._status()
            self._program = copy.deepcopy(program)
            self._uploaded = True

    def arm(self):
        with self._guard():
            if not self._uploaded or self._armed:
                raise SpectrumRuntimeError("Upload a complete DDS shot before arming")
            if not self._running:
                self._card.start(
                    self._sdk.M2CMD_CARD_ENABLETRIGGER,
                    self._sdk.M2CMD_CARD_FORCETRIGGER,
                )
                self._running = True
            self._armed = True

    def ready(self):
        with self._guard():
            if not self._armed:
                return False
            status = self._status()
            count = self._dds.trg_count()
            if count > self._baseline:
                raise SpectrumRuntimeError(
                    "Unexpected DDS trigger before the ARTIQ launch"
                )
            if count < self._baseline:
                return False
            if status & self._sdk.SPCM_DDS_STAT_WAITING_FOR_TRG:
                self._hold = copy.deepcopy(self._program["initial_tones"])
                return True
            return False

    def wait_complete(self, timeout_s):
        with self._guard():
            if not self._armed or not math.isfinite(timeout_s) or timeout_s <= 0:
                raise SpectrumRuntimeError(
                    "DDS completion requires an armed shot and timeout"
                )
            expected = self._baseline + self._program["execution_count"]
            deadline = time.monotonic() + timeout_s
            while True:
                self._status()
                count = self._dds.trg_count()
                if count > expected:
                    raise SpectrumRuntimeError("DDS received extra execution triggers")
                if count == expected:
                    self._last_count = count
                    self._hold = copy.deepcopy(self._program["final_tones"])
                    self._program = None
                    self._uploaded = self._armed = False
                    return
                if time.monotonic() >= deadline:
                    raise SpectrumRuntimeError(
                        "DDS shot did not reach its terminal hold frame"
                    )
                time.sleep(min(0.001, max(0, deadline - time.monotonic())))

    def select_entry(self, step):
        if step != 0:
            raise SpectrumRuntimeError("DDS programs are uploaded explicitly per shot")

    def stop(self):
        if self._card is not None:
            self._card.stop(self._sdk.M2CMD_DATA_STOPDMA)
        self._running = self._armed = self._uploaded = False
        self._hold = self._program = self._baseline = self._last_count = None
        self._contract = self._dds = None

    def close(self):
        try:
            self.stop()
        finally:
            if self._card is not None:
                self._card.close()
            self._card = None
