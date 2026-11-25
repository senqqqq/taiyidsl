"""In-memory Spectrum sequence SDK, usable in an isolated worker without pytest."""

from types import SimpleNamespace

import numpy as np


class FakeSdk:
    def __init__(self):
        self.log = []
        names = (
            "SPC_PCIMEMSIZE SPC_PCIVERSION SPC_PCIMODULEVERSION "
            "SPC_SEQMODE_AVAILMAXSEGMENT SPC_SEQMODE_AVAILMAXSTEPS "
            "SPC_MIINST_ISDEMOCARD SPC_CM_EXTREFCLOCK SPC_REP_STD_SEQUENCE "
            "SPC_TMASK_EXT0 SPC_TM_POS COUPLING_DC M2CMD_DATA_STARTDMA "
            "M2CMD_DATA_WAITDMA SPCSEQ_END SPCSEQ_ENDLOOPALWAYS "
            "M2CMD_CARD_ENABLETRIGGER M2CMD_CARD_WAITREADY M2CMD_DATA_STOPDMA "
            "SPCM_STOPLVL_ZERO SPC_REP_STD_SINGLE SPC_MEMSIZE SPC_CM_INTPLL"
        ).split()
        for index, name in enumerate(names, 10):
            setattr(self, name, index)
        self.SPCM_FEAT_SEQUENCE = 0x1000
        self.M2STAT_CARD_READY, self.M2STAT_CARD_TRIGGER = 4, 2
        self.clock = {
            "mode": self.SPC_CM_EXTREFCLOCK,
            "sample_rate": 1_250_000_000,
            "reference_clock": 125_000_000,
        }
        self.card = FakeCard(self)

    def Card(self, **kwargs):
        assert kwargs == {"serial_number": 101}
        return self.card

    def Clock(self, card):
        def method(name):
            def access(value=None):
                if value is not None:
                    self.log.append(("clock_write", name, value))
                    self.clock[name] = value
                return self.clock[name]

            return access

        return SimpleNamespace(**{name: method(name) for name in self.clock})

    def Channels(self, card, card_enable):
        card.channel_mask = card_enable
        return [FakeChannel(self, i) for i in range(2) if card_enable & (1 << i)]

    def Trigger(self, card):
        def record(name):
            def access(value):
                self.log.append(("trigger", name, value))
                return value + (getattr(self, "bad_trigger_readback", None) == name)

            return access

        return SimpleNamespace(
            **{
                name: record(name)
                for name in (
                    "or_mask",
                    "and_mask",
                    "ext0_mode",
                    "ext0_coupling",
                    "termination",
                    "ext0_level0",
                    "delay",
                )
            }
        )

    def Sequence(self, card):
        def record(name):
            def call(*args, **kwargs):
                self.log.append(("sequence", name, args, kwargs))
                if name == "max_segments":
                    card.steps.clear()
                    card.loaded.clear()
                elif name == "write_segment":
                    card.segment = args[0]
                elif name == "step_memory":
                    card.steps[args[0]] = kwargs["segment_index"]
                elif name == "start_step":
                    card.entry = args[0]

            return call

        return SimpleNamespace(
            **{
                name: record(name)
                for name in (
                    "max_segments",
                    "write_segment",
                    "segment_size",
                    "step_memory",
                    "start_step",
                )
            }
        )

    def DataTransfer(self, card):
        sdk = self

        class Transfer:
            def allocate_buffer(self, count):
                self.buffer = np.zeros(
                    (card.channel_mask.bit_count(), count), dtype=np.int16
                )

            def start_buffer_transfer(self, *flags):
                sdk.log.append(("dma", flags, self.buffer.copy()))
                card.loaded.add(card.segment)

        return Transfer()


class FakeCard:
    def __init__(self, sdk):
        self.sdk = sdk
        self.state = 4
        self.channel_mask = 0
        self.mode = sdk.SPC_REP_STD_SEQUENCE  # Unknown prior session; no valid table.
        self.entry = 0
        self.steps = {}
        self.loaded = set()

    def open(self):
        self.sdk.log.append(("open",))

    def sn(self):
        return 101

    def product_name(self):
        return "M4i.6631-x8"

    def get_i(self, key):
        sdk = self.sdk
        return {
            sdk.SPC_PCIMEMSIZE: 4 * 1024**3,
            sdk.SPC_PCIVERSION: 1,
            sdk.SPC_PCIMODULEVERSION: 2,
            sdk.SPC_SEQMODE_AVAILMAXSEGMENT: 65536,
            sdk.SPC_SEQMODE_AVAILMAXSTEPS: 4096,
            sdk.SPC_MIINST_ISDEMOCARD: 0,
        }[key]

    def features(self):
        return self.sdk.SPCM_FEAT_SEQUENCE

    def stop(self, *flags):
        self.state = 4
        self.sdk.log.append(("stop", flags))

    def card_mode(self, value):
        self.mode = value
        self.sdk.log.append(("card_mode", value))
        return value

    def set_i(self, reg, value):
        self.sdk.log.append(("set_i", reg, value))

    def write_setup(self):
        if self.mode == self.sdk.SPC_REP_STD_SEQUENCE:
            if (
                self.entry not in self.steps
                or self.steps[self.entry] not in self.loaded
            ):
                raise ValueError(f"Sequence check: start step ({self.entry}) not valid")
        self.sdk.log.append(("write_setup",))

    def timeout(self, value):
        self.sdk.log.append(("timeout", value))

    def start(self, *flags):
        self.state = 0
        self.sdk.log.append(("start", flags))

    def status(self):
        return self.state

    def cmd(self, flag):
        assert flag == self.sdk.M2CMD_CARD_WAITREADY
        self.state = 6

    def close(self):
        self.sdk.log.append(("close",))


class FakeChannel:
    def __init__(self, sdk, index):
        self.sdk, self.index = sdk, index

    def enable(self, value):
        self.sdk.log.append(("output", self.index, value))

    def amp(self, value):
        self.sdk.log.append(("range", self.index, value))
        return value

    def offset(self, value):
        self.sdk.log.append(("offset", self.index, value))
        return value

    def stop_level(self, value):
        self.sdk.log.append(("stop_level", self.index, value))
        return value
