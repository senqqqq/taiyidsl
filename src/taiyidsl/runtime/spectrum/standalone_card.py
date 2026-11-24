"""SDK operations for one internally clocked, software-launched Spectrum card."""

from __future__ import annotations

import time

from ...backends.artiq_spectrum.dds import dds_channel_cores
from ...backends.spectrum.dds import AMPLITUDE_STEP, FREQUENCY_STEP_HZ, PHASE_STEP_DEG
from .awg import SegmentCache, SpcmAdapter, SpectrumRuntimeError
from .dds import _close_number


def _wait_for(predicate, timeout_s, message):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise SpectrumRuntimeError(message)
        time.sleep(0.001)


class StandaloneSpcmCard(SpcmAdapter):
    """The card stays muted until a complete shot is resident and launched."""

    def inspect(self, contract):
        card, sdk = self._open(), self._sdk
        observed = {
            "model": card.product_name(),
            "serial_number": str(card.sn()),
            "firmware": (
                f"{card.get_i(sdk.SPC_PCIVERSION):08x}/"
                f"{card.get_i(sdk.SPC_PCIMODULEVERSION):08x}"
            ),
        }
        if (
            observed["model"] != contract["model"]
            or observed["serial_number"] != contract["serial_number"]
            or card.get_i(sdk.SPC_MIINST_ISDEMOCARD)
        ):
            raise SpectrumRuntimeError(
                "Standalone execution requires the declared physical card"
            )
        if contract.get("firmware") not in (
            None,
            "",
            "unmeasured",
            observed["firmware"],
        ):
            raise SpectrumRuntimeError(
                "Card firmware differs from the compiled contract"
            )
        if contract["mode"] == "awg":
            if not card.features() & sdk.SPCM_FEAT_SEQUENCE:
                raise SpectrumRuntimeError("Card does not support sequence replay")
            observed.update(
                memory_bytes=card.get_i(sdk.SPC_PCIMEMSIZE),
                max_segments=card.get_i(sdk.SPC_SEQMODE_AVAILMAXSEGMENT),
                max_steps=card.get_i(sdk.SPC_SEQMODE_AVAILMAXSTEPS),
            )
            if observed["memory_bytes"] < contract["memory_bytes"]:
                raise SpectrumRuntimeError(
                    "Installed AWG memory is below its declared capacity"
                )
        else:
            if not (
                card.get_i(sdk.SPC_AVAILCARDMODES) & sdk.SPC_REP_STD_DDS
                and card.ext_features() & sdk.SPCM_FEAT_EXTFW_DDS20
                and card.get_i(sdk.SPC_DDS_NUM_CORES) == 21
            ):
                raise SpectrumRuntimeError(
                    "The physical DDS20 mode with 21 cores is required"
                )
            observed["max_commands"] = card.get_i(sdk.SPC_DDS_QUEUE_CMD_MAX)
        return observed

    def prepare_shot(self, contract, program):
        card, sdk = self._card, self._sdk
        self._contract = contract  # Stop even if configuration fails partway.
        card.stop()
        card.timeout(10_000)
        awg = contract["mode"] == "awg"
        mode = sdk.SPC_REP_STD_SEQUENCE if awg else sdk.SPC_REP_STD_DDS
        if card.card_mode(mode) != mode:
            raise SpectrumRuntimeError("Card refused the requested replay mode")
        clock = sdk.Clock(card)
        clock.mode(sdk.SPC_CM_INTPLL)
        clock.sample_rate(contract["sample_rate_hz"])
        trigger = sdk.Trigger(card)
        # Mask automatic and external sources. FORCETRIGGER alone starts the shot.
        if trigger.or_mask(0) or trigger.and_mask(0) or trigger.delay(0):
            raise SpectrumRuntimeError("Software trigger mask/delay readback mismatch")
        channels = self._configure_outputs(contract, awg=awg)
        if awg:
            # WRITESETUP validates the entry step, even on a stopped M4i.6631.
            # upload() commits only after loading segments, steps and start_step(0).
            self._sequence = sdk.Sequence(card)
            self.upload(program, SegmentCache().load)
        else:
            card.write_setup()
        if (
            clock.mode() != sdk.SPC_CM_INTPLL
            or clock.sample_rate() != contract["sample_rate_hz"]
        ):
            raise SpectrumRuntimeError("Internal PLL/sample rate readback mismatch")
        if not awg:
            self._upload_dds(channels, program)
            for channel in channels:
                channel.enable(True)
            card.write_setup()

    def _upload_dds(self, channels, program):
        card, sdk = self._card, self._sdk
        self._dds = dds = sdk.DDS(card, channels=channels)
        dds.reset()
        # RESET clears the DDS mixer and ramps. Verify all routed and unused cores
        # while both amplifiers are still disabled.
        for core in dds:
            if core.get_amp() or core.get_amp_slope() or core.get_freq_slope():
                raise SpectrumRuntimeError("DDS reset left an active tone or ramp")
        for field, register, expected in (
            ("amplitude step", sdk.SPC_DDS_AVAIL_AMP_STEP, AMPLITUDE_STEP),
            ("frequency step", sdk.SPC_DDS_AVAIL_FREQ_STEP, FREQUENCY_STEP_HZ),
            ("phase step", sdk.SPC_DDS_AVAIL_PHASE_STEP, PHASE_STEP_DEG),
        ):
            if not _close_number(card.get_d(register), expected):
                raise SpectrumRuntimeError(
                    f"DDS {field} differs from compiled precision"
                )
        dds.data_transfer_mode(sdk.SPCM_DDS_DTM_SINGLE)
        if dds.get_data_transfer_mode() != sdk.SPCM_DDS_DTM_SINGLE:
            raise SpectrumRuntimeError("DDS SINGLE transfer mode readback mismatch")
        for channel, cores in dds_channel_cores(self._contract["dds_routing"]).items():
            mask = sum(1 << core for core in cores)
            dds.cores_on_channel(channel, mask)
            if dds.get_cores_on_channel(channel) != mask:
                raise SpectrumRuntimeError("DDS channel/core routing readback mismatch")
        dds.load_cores()
        dds.phase_behaviour(sdk.SPCM_DDS_PHASE_JUMP)
        if dds.get_phase_behaviour() != sdk.SPCM_DDS_PHASE_JUMP:
            raise SpectrumRuntimeError("DDS JUMP phase behaviour readback mismatch")
        # Apply trigger selection while the DDS cores are still muted after RESET.
        # The next EXEC_AT_TRG commits frame zero on the single software launch.
        dds.trg_src(sdk.SPCM_DDS_TRG_SRC_CARD)
        dds.exec_now()
        counter = sdk.SPC_DDS_NUM_QUEUED_CMD_IN_SW
        setup_commands = card.get_i(counter)
        capacity = dds.queue_cmd_max()
        if setup_commands + program["command_count"] > capacity:
            raise SpectrumRuntimeError(
                "DDS setup and shot exceed the physical command queue"
            )
        for frame in program["frames"]:
            cycles = frame["timer_cycles"]
            dds.trg_src(
                sdk.SPCM_DDS_TRG_SRC_CARD
                if cycles is None
                else sdk.SPCM_DDS_TRG_SRC_TIMER
            )
            if cycles is not None:
                timer = cycles * 6.4e-9
                dds.trg_timer(timer)
                if not _close_number(dds.get_trg_timer(), timer):
                    raise SpectrumRuntimeError("DDS timer readback mismatch")
            for tone in frame["tones"]:
                core = dds[tone["core"]]
                for field, method in (
                    ("frequency_hz", "freq"),
                    ("amplitude", "amp"),
                    ("phase_deg", "phase"),
                ):
                    value = tone[field]
                    getattr(core, method)(value)
                    if not _close_number(getattr(core, f"get_{method}")(), value):
                        raise SpectrumRuntimeError(f"DDS {method} readback mismatch")
            dds.exec_at_trg()
        total = card.get_i(counter)
        if not (0 < total <= min(capacity, setup_commands + program["command_count"])):
            raise SpectrumRuntimeError(
                "Actual DDS command count exceeds compiled queue budget"
            )
        dds.write_to_card()

    def _dds_status(self):
        sdk = self._sdk
        value = self._dds.status()
        if value & (sdk.SPCM_DDS_STAT_QUEUE_UNDERRUN | sdk.SPCM_DDS_STAT_QUEUE_OVERRUN):
            raise SpectrumRuntimeError("DDS command queue underrun/overrun")
        return value

    def play_shot(self, program, timeout_s):
        card, sdk = self._card, self._sdk
        if self._contract["mode"] == "awg":
            self.arm()
            _wait_for(self.ready, 5.0, "AWG failed to arm")
            card.cmd(sdk.M2CMD_CARD_FORCETRIGGER)
            self.wait_complete(timeout_s)
            return
        card.start(sdk.M2CMD_CARD_ENABLETRIGGER)
        _wait_for(
            lambda: self._dds_status() & sdk.SPCM_DDS_STAT_WAITING_FOR_TRG,
            5.0,
            "DDS failed to arm the first frame",
        )
        expected = self._dds.trg_count() + program["execution_count"]
        card.cmd(sdk.M2CMD_CARD_FORCETRIGGER)

        def terminal_frame_committed():
            self._dds_status()
            count = self._dds.trg_count()
            if count > expected:
                raise SpectrumRuntimeError("DDS received extra triggers")
            return count == expected

        _wait_for(
            terminal_frame_committed, timeout_s, "DDS terminal mute did not execute"
        )
        card.stop()
