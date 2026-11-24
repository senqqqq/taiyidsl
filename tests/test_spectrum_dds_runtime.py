from types import SimpleNamespace

import pytest

from taiyidsl.backends.artiq_spectrum.dds import (
    AMPLITUDE_STEP,
    FREQUENCY_SLOPE_STEP,
    FREQUENCY_STEP_HZ,
    build_dds_program,
)
from taiyidsl.runtime.spectrum.awg import SpectrumRuntimeError
from taiyidsl.runtime.spectrum.dds import SpcmDDSAdapter


def fixture():
    """Independent shadow/FIFO fake: queue empty does not imply a frame committed."""
    spec = {
        "model": "M4i.6631-x8",
        "serial_number": "101",
        "mode": "dds",
        "active_channels": [0, 1],
        "sample_rate_hz": 1_250_000_000,
        "reference_clock_hz": 125_000_000,
        "output_range_mv": 500,
        "dds_routing": "16/5",
        "dds_trigger_latency_ns": 560,
        "dds_amplitude_slope_step_per_s": 1e-6,
    }
    initial = [
        dict(core=0, channel=0, frequency_hz=100e6, amplitude=0.125),
        dict(core=20, channel=1, frequency_hz=90e6, amplitude=0.125),
    ]
    middle = [{**initial[0], "frequency_hz": 101e6, "amplitude": 0.0625}, initial[1]]
    move = dict(
        start_mu=0,
        motion_duration_mu=8192,
        knots=[
            dict(offset_mu=0, tones=initial),
            dict(offset_mu=4096, tones=middle),
            dict(offset_mu=8192, tones=initial),
        ],
    )
    program = build_dds_program(spec, initial, [move], 8192)
    sdk = SimpleNamespace()
    names = [
        "SPC_PCIMEMSIZE",
        "SPC_PCIVERSION",
        "SPC_PCIMODULEVERSION",
        "SPC_MIINST_ISDEMOCARD",
        "SPC_AVAILCARDMODES",
        "SPC_DDS_NUM_CORES",
        "SPC_DDS_QUEUE_CMD_MAX",
        "SPC_DDS_NUM_QUEUED_CMD_IN_SW",
        "SPC_DDS_AVAIL_FREQ_STEP",
        "SPC_DDS_AVAIL_FREQ_SLOPE_STEP",
        "SPC_DDS_AVAIL_AMP_STEP",
        "SPC_DDS_AVAIL_AMP_SLOPE_STEP",
        "SPC_DDS_AVAIL_FREQ_MIN",
        "SPC_DDS_AVAIL_FREQ_MAX",
        "SPC_DDS_AVAIL_AMP_MIN",
        "SPC_DDS_AVAIL_AMP_MAX",
        "SPC_DDS_AVAIL_FREQ_SLOPE_MIN",
        "SPC_DDS_AVAIL_FREQ_SLOPE_MAX",
        "SPC_DDS_AVAIL_AMP_SLOPE_MIN",
        "SPC_DDS_AVAIL_AMP_SLOPE_MAX",
        "SPC_CM_EXTREFCLOCK",
        "SPC_TMASK_EXT0",
        "SPC_TM_POS",
        "COUPLING_DC",
        "M2CMD_CARD_ENABLETRIGGER",
        "M2CMD_CARD_FORCETRIGGER",
        "M2CMD_DATA_STOPDMA",
    ]
    for index, name in enumerate(names):
        setattr(sdk, name, index + 100)
    sdk.SPC_DDS_CORES_ON_CH0 = 1000
    sdk.SPC_REP_STD_DDS = 1 << 26
    sdk.SPCM_FEAT_EXTFW_DDS20 = 16
    sdk.SPCM_DDS_DTM_SINGLE = 0
    sdk.SPCM_DDS_STAT_WAITING_FOR_TRG = 1
    sdk.SPCM_DDS_STAT_QUEUE_UNDERRUN = 2
    sdk.SPCM_DDS_STAT_QUEUE_OVERRUN = 4
    sdk.SPCM_DDS_TRG_SRC_CARD = 2
    sdk.SPCM_DDS_TRG_SRC_TIMER = 1

    class Card:
        def __init__(self):
            self.log, self.mode_value, self.active = [], 0, {}
            self.routing = {0: (1 << 20) - 1, 1: 1 << 20}
            self.serial = 101
            self.dds = None
            self.clock_mode = sdk.SPC_CM_EXTREFCLOCK
            self.freq_step = FREQUENCY_STEP_HZ

        def open(self):
            self.log.append("open")

        def close(self):
            self.log.append("close")

        def sn(self):
            return self.serial

        def product_name(self):
            return "M4i.6631-x8"

        def ext_features(self):
            return sdk.SPCM_FEAT_EXTFW_DDS20

        def timeout(self, value):
            pass

        def write_setup(self):
            self.log.append("write_setup")

        def card_mode(self, value=None):
            if value is not None:
                self.mode_value = value
            return self.mode_value

        def stop(self, *args):
            self.log.append("stop")

        def start(self, *args):
            self.log.append("start")
            if sdk.M2CMD_CARD_FORCETRIGGER in args:
                self.advance()

        def advance(self):
            dds = self.dds
            if dds.shadow is None:
                dds.error |= sdk.SPCM_DDS_STAT_QUEUE_UNDERRUN
                return
            self.active.update(dds.shadow)
            dds.counter += 1
            dds.shadow = dds.queue.pop(0) if dds.queue else None

        def run_shot(self):
            self.advance()
            while self.active.get("source") == sdk.SPCM_DDS_TRG_SRC_TIMER:
                self.advance()

        def get_i(self, register):
            if register in (1000, 1001):
                return self.routing[register - 1000]
            if register == sdk.SPC_DDS_NUM_QUEUED_CMD_IN_SW:
                return len(self.dds.pending) + sum(
                    len(frame) + 1 for frame in self.dds.software
                )
            return {
                sdk.SPC_PCIMEMSIZE: 4 * 1024**3,
                sdk.SPC_PCIVERSION: 1,
                sdk.SPC_PCIMODULEVERSION: 2,
                sdk.SPC_MIINST_ISDEMOCARD: 0,
                sdk.SPC_AVAILCARDMODES: sdk.SPC_REP_STD_DDS,
                sdk.SPC_DDS_NUM_CORES: 21,
                sdk.SPC_DDS_QUEUE_CMD_MAX: 4096,
            }[register]

        def get_d(self, register):
            return {
                sdk.SPC_DDS_AVAIL_FREQ_STEP: self.freq_step,
                sdk.SPC_DDS_AVAIL_FREQ_SLOPE_STEP: FREQUENCY_SLOPE_STEP,
                sdk.SPC_DDS_AVAIL_AMP_STEP: AMPLITUDE_STEP,
                sdk.SPC_DDS_AVAIL_AMP_SLOPE_STEP: 1e-6,
                sdk.SPC_DDS_AVAIL_FREQ_MIN: 0,
                sdk.SPC_DDS_AVAIL_FREQ_MAX: 1.25e9,
                sdk.SPC_DDS_AVAIL_AMP_MIN: -1,
                sdk.SPC_DDS_AVAIL_AMP_MAX: 1,
                sdk.SPC_DDS_AVAIL_FREQ_SLOPE_MIN: -1e20,
                sdk.SPC_DDS_AVAIL_FREQ_SLOPE_MAX: 1e20,
                sdk.SPC_DDS_AVAIL_AMP_SLOPE_MIN: -1e20,
                sdk.SPC_DDS_AVAIL_AMP_SLOPE_MAX: 1e20,
            }[register]

    card = Card()

    class Channel:
        def __init__(self, index):
            self.index = index

        def enable(self, value):
            return value

        def amp(self, value):
            return value

        def offset(self, value):
            return value

    class Clock:
        def __init__(self, card):
            self.card = card

        def sample_rate(self):
            return 1_250_000_000

        def reference_clock(self):
            return 125_000_000

        def mode(self):
            return self.card.clock_mode

    class Trigger:
        def __init__(self, card):
            pass

        def __getattr__(self, name):
            return lambda value: value

    class Core:
        def __init__(self, index, dds):
            self.index, self.dds = index, dds

        def __getattr__(self, name):
            if name.startswith("get_"):
                return lambda: self.dds.written[(self.index, name[4:])]

            def setter(value):
                self.dds.pending[(self.index, name)] = value
                self.dds.written[(self.index, name)] = value
                self.dds.writes += 1

            return setter

    class DDS:
        def __init__(self, card, channels=None):
            self.card = card
            card.dds = self
            self.pending, self.written, self.software, self.queue = {}, {}, [], []
            self.shadow, self.counter, self.error, self.writes = None, 0, 0, 0

        def __getitem__(self, index):
            return Core(index, self)

        def reset(self):
            self.card.log.append("reset")

        def data_transfer_mode(self, mode):
            self.transfer_mode = mode

        def get_data_transfer_mode(self):
            return self.transfer_mode

        def cores_on_channel(self, channel, *masks):
            self.card.routing[channel] = sum(masks)
            self.pending[("routing", channel)] = sum(masks)

        def load_cores(self):
            pass

        def freq_ramp_stepsize(self, divider):
            self.freq_divider = divider
            self.pending["freq_divider"] = divider

        def amp_ramp_stepsize(self, divider):
            self.amp_divider = divider
            self.pending["amp_divider"] = divider

        def get_freq_ramp_stepsize(self):
            return self.freq_divider

        def get_amp_ramp_stepsize(self):
            return self.amp_divider

        def queue_cmd_max(self):
            return 4096

        def queue_cmd_count(self):
            return sum(len(frame) + 1 for frame in self.queue)

        def trg_count(self):
            return self.counter

        def status(self):
            return self.error | (1 if self.shadow is not None else 0)

        def trg_src(self, source):
            self.pending["source"] = source

        def trg_timer(self, timer):
            self.timer = timer
            self.pending["timer"] = timer

        def get_trg_timer(self):
            return self.timer

        def exec_at_trg(self):
            self.software.append(self.pending.copy())
            self.pending.clear()

        def write_to_card(self):
            self.card.log.append("write_to_card")
            self.queue.extend(self.software)
            self.software.clear()
            if self.shadow is None and self.queue:
                self.shadow = self.queue.pop(0)

    sdk.Card = lambda *args, **kwargs: card
    sdk.Clock, sdk.Trigger, sdk.DDS = Clock, Trigger, DDS
    sdk.Channels = lambda card, card_enable=3: [
        Channel(index) for index in (0, 1) if card_enable & (1 << index)
    ]
    adapter = SpcmDDSAdapter("101", sdk=sdk)
    return adapter, card, spec, program


def test_persistent_hold_closed_shots_start_once_and_stop_on_close():
    adapter, card, spec, program = fixture()
    assert card.log == []
    adapter.configure(spec)
    configured = card.log.copy()
    adapter.configure(spec)
    assert card.log == configured
    for _ in range(2):
        adapter.upload(program)
        adapter.arm()
        assert adapter.ready()
        card.run_shot()
        # The terminal packet is already executed: no queued EXEC remains.
        assert not card.dds.status() & 1
        adapter.wait_complete(0.1)
        assert card.active[(0, "amp")] == 0.125
        assert card.active[(0, "freq_slope")] == 0
        assert card.active[(0, "amp_slope")] == 0
        assert card.active["source"] == 2
    assert card.log.count("start") == 1
    assert card.log.count("reset") == 1
    assert card.log.count("stop") == 1  # initial configure only
    prior = card.log.copy()
    with pytest.raises(SpectrumRuntimeError, match="Close the live DDS session"):
        adapter.configure_for_calibration(spec)
    assert card.log == prior  # Recalibration must not interrupt a persistent hold.
    adapter.close()
    assert card.log[-2:] == ["stop", "close"]


def test_empty_fifo_is_not_completion_and_timeout_stops():
    adapter, card, spec, program = fixture()
    adapter.configure(spec)
    adapter.upload(program)
    adapter.arm()
    assert adapter.ready()
    for _ in range(program["execution_count"] - 1):
        card.advance()
    assert card.dds.queue_cmd_count() == 0
    assert card.dds.shadow is not None
    with pytest.raises(SpectrumRuntimeError, match="terminal hold"):
        adapter.wait_complete(0.001)
    assert card.log[-1] == "stop"


def test_native_software_queue_expansion_fails_before_hardware_transfer():
    adapter, card, spec, program = fixture()
    adapter.configure(spec)
    original_get = card.get_i

    def expanded_count(register):
        value = original_get(register)
        if register == adapter._sdk.SPC_DDS_NUM_QUEUED_CMD_IN_SW and card.dds.software:
            return value + 1
        return value

    card.get_i = expanded_count
    with pytest.raises(SpectrumRuntimeError, match="compiled queue budget"):
        adapter.upload(program)
    assert "write_to_card" not in card.log
    assert "start" not in card.log
    assert card.log[-1] == "stop"


@pytest.mark.parametrize(
    "failure", ["early_trigger", "underrun", "extra_trigger", "changed_profile"]
)
def test_runtime_fails_closed_on_protocol_or_profile_changes(failure):
    adapter, card, spec, program = fixture()
    adapter.configure(spec)
    if failure == "changed_profile":
        card.freq_step *= 2
        writes = card.dds.writes
        with pytest.raises(SpectrumRuntimeError, match="compiled profile"):
            adapter.upload(program)
        assert card.dds.writes == writes
    else:
        adapter.upload(program)
        adapter.arm()
        if failure == "early_trigger":
            card.advance()
            with pytest.raises(SpectrumRuntimeError, match="before the ARTIQ"):
                adapter.ready()
        elif failure == "underrun":
            card.dds.error = 2
            with pytest.raises(SpectrumRuntimeError, match="underrun"):
                adapter.ready()
        else:
            card.run_shot()
            card.dds.counter += 1
            with pytest.raises(SpectrumRuntimeError, match="extra"):
                adapter.wait_complete(0.1)
    assert card.log[-1] == "stop"
