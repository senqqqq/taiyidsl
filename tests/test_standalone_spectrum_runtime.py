"""Exercise complete card programs against an SDK double, without instruments."""

import json
from types import SimpleNamespace

import pytest
from spectrum_sdk_double import FakeChannel, FakeSdk
from test_spectrum_dds_runtime import fixture as transport_sdk

from examples.spectrum_standalone import single_card_lab, two_pulses
from taiyidsl import Qubit, Square, ns
from taiyidsl.backends.spectrum.dds import PHASE_STEP_DEG
from taiyidsl.runtime.spectrum import standalone_card
from taiyidsl.runtime.spectrum.awg import SpectrumRuntimeError
from taiyidsl.runtime.spectrum.standalone import StandaloneSpectrumSession


def awg_sdk():
    sdk = FakeSdk()
    sdk.SPC_CM_INTPLL = 73
    sdk.M2CMD_CARD_FORCETRIGGER = 74
    original_cmd = sdk.card.cmd

    def cmd(command):
        sdk.log.append(("cmd", command))
        if command == sdk.M2CMD_CARD_FORCETRIGGER:
            assert sdk.card.state == 0
            sdk.card.state = sdk.M2STAT_CARD_TRIGGER
        else:
            assert sdk.card.state == sdk.M2STAT_CARD_TRIGGER
            original_cmd(command)

    sdk.card.cmd = cmd
    return sdk, sdk.card


def dds_sdk():
    adapter, card, _, _ = transport_sdk()
    sdk = adapter._sdk
    # Extend the existing independent shadow/FIFO model with standalone setup.
    sdk.SPC_CM_INTPLL = 77
    sdk.SPC_DDS_AVAIL_PHASE_STEP = 78
    sdk.SPCM_STOPLVL_ZERO = 79
    sdk.SPCM_DDS_PHASE_JUMP = 0
    original_get_d = card.get_d
    card.get_d = lambda key: (
        PHASE_STEP_DEG if key == sdk.SPC_DDS_AVAIL_PHASE_STEP else original_get_d(key)
    )
    sdk.log = card.log
    clock = FakeSdk()
    sdk.Clock = clock.Clock
    sdk.Channels = lambda card, card_enable: [
        FakeChannel(sdk, index) for index in (0, 1) if card_enable & (1 << index)
    ]
    sdk.Trigger = clock.Trigger

    class DDS(sdk.DDS):
        def __iter__(self):
            return (self[index] for index in range(21))

        def reset(self):
            super().reset()
            for index in range(21):
                for name in ("amp", "amp_slope", "freq_slope"):
                    self.written[(index, name)] = 0

        def get_cores_on_channel(self, ch):
            return card.routing[ch]

        def phase_behaviour(self, value):
            self.behaviour = value
            self.pending["phase_behaviour"] = value

        def get_phase_behaviour(self):
            return self.behaviour

        def exec_now(self):
            assert self.pending["source"] == sdk.SPCM_DDS_TRG_SRC_CARD
            self.pending["immediate"] = True
            self.exec_at_trg()

    sdk.DDS = DDS
    original_start = card.start

    def start(*flags):
        original_start(*flags)
        assert flags == (sdk.M2CMD_CARD_ENABLETRIGGER,)
        assert card.dds.shadow["immediate"]
        card.advance()  # The muted EXEC_NOW setup needs no external trigger.

    def cmd(command):
        assert command == sdk.M2CMD_CARD_FORCETRIGGER
        assert card.active["source"] == sdk.SPCM_DDS_TRG_SRC_CARD
        assert not card.active.get((0, "amp"), 0)
        card.log.append(("cmd", command))
        card.run_shot()  # Firmware timers consume every remaining frame.

    card.start, card.cmd = start, cmd
    return sdk, card


def shot_input(mode, frequencies=(10, 11)):
    lab = single_card_lab(mode, serial_number="101", address=None, emulation=False)
    pulse = Square(640 * ns, amplitude=0.25)
    return lab, lab.compile(two_pulses(Qubit("q0"), pulse, list(frequencies), shots=2))


@pytest.mark.parametrize(("mode", "factory"), [("awg", awg_sdk), ("dds", dds_sdk)])
def test_standalone_launch_once_per_shot_and_confirm_end(
    mode, factory, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    lab, compiled = shot_input(mode)
    sdk, card = factory()
    with StandaloneSpectrumSession(lab, sdk=sdk) as session:
        assert not any(item[0] == "cmd" for item in sdk.log)
        receipt = session.run(compiled)
        assert receipt["completed_shots"] == [
            {"point": p, "shot": s} for p, s in compiled.execution_order()
        ]
        assert receipt["hardware_executed"] is False
        assert receipt["compiled_sha256"] == compiled.sha256
        (file,) = session.directory.glob("run_*/execution_receipt.json")
        assert json.loads(file.read_text(encoding="utf-8")) == receipt
        force = ("cmd", sdk.M2CMD_CARD_FORCETRIGGER)
        assert sdk.log.count(force) == 4
        if mode == "awg":
            assert sdk.log.count(("cmd", sdk.M2CMD_CARD_WAITREADY)) == 4
            assert ("trigger", "or_mask", 0) in sdk.log
            assert ("clock_write", "mode", sdk.SPC_CM_INTPLL) in sdk.log
        else:
            assert card.active[(0, "amp")] == 0
            assert sdk.log.count("write_to_card") == 4
            assert sdk.log.count("reset") == 4
            assert not any(
                isinstance(item, tuple) and item[0] == "stop_level" for item in sdk.log
            )
            assert sdk.log.index("reset") < sdk.log.index(("output", 0, True))
    assert ("output", 0, False) in sdk.log[-5:]
    with pytest.raises(SpectrumRuntimeError, match="not connected"):
        session.run(compiled)


def test_repeated_awg_shots_rearm_one_resident_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lab, compiled = shot_input("awg", frequencies=(10,))
    sdk, _ = awg_sdk()
    with StandaloneSpectrumSession(lab, sdk=sdk) as session:
        receipt = session.run(compiled)
    assert len(receipt["completed_shots"]) == 2
    assert sdk.log.count(("card_mode", sdk.SPC_REP_STD_SEQUENCE)) == 1
    assert sdk.log.count(("cmd", sdk.M2CMD_CARD_FORCETRIGGER)) == 2


@pytest.mark.parametrize("fail_dma", [False, True])
def test_awg_setup_never_commits_an_undefined_entry(tmp_path, monkeypatch, fail_dma):
    monkeypatch.chdir(tmp_path)
    lab, compiled = shot_input("awg", frequencies=(10,))
    sdk, card = awg_sdk()
    write_setup = card.write_setup
    commits = []

    def checked_setup():
        mode = next(x[1] for x in reversed(sdk.log) if x[0] == "card_mode")
        commits.append(mode)
        if mode == sdk.SPC_REP_STD_SEQUENCE:
            # The physical card validates step zero even on WRITESETUP without START.
            assert any(x[0] == "dma" for x in sdk.log)
            assert any(x[:3] == ("sequence", "step_memory", (0,)) for x in sdk.log)
            assert ("sequence", "start_step", (0,), {}) in sdk.log
        else:
            assert mode == sdk.SPC_REP_STD_SINGLE
            assert any(x[:2] == ("set_i", sdk.SPC_MEMSIZE) for x in sdk.log)
            stop = max(i for i, x in enumerate(sdk.log) if x[0] == "stop")
            assert all(("output", ch, False) in sdk.log[stop:] for ch in (0, 1))
        write_setup()

    card.write_setup = checked_setup
    if fail_dma:

        def fail_transfer(card):
            raise RuntimeError("injected upload failure before step zero")

        sdk.DataTransfer = fail_transfer
        with pytest.raises(RuntimeError, match="injected upload failure") as caught:
            with StandaloneSpectrumSession(lab, sdk=sdk) as session:
                session.run(compiled)
        assert not getattr(caught.value, "__notes__", [])
        assert commits == [sdk.SPC_REP_STD_SINGLE]
        assert not any(x[0] in {"start", "cmd"} for x in sdk.log)
    else:
        with StandaloneSpectrumSession(lab, sdk=sdk) as session:
            assert len(session.run(compiled)["completed_shots"]) == 2
        assert commits == [sdk.SPC_REP_STD_SEQUENCE, sdk.SPC_REP_STD_SINGLE]
    assert sdk.log[-1] == ("close",)


def test_awg_failure_preserves_partial_receipt_and_stops(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lab, compiled = shot_input("awg")
    sdk, card = awg_sdk()
    original_cmd = card.cmd

    def cmd(command):
        if command == sdk.M2CMD_CARD_WAITREADY and sdk.log.count(("cmd", command)) == 1:
            raise TimeoutError("No completion observed")
        original_cmd(command)

    card.cmd = cmd
    session = StandaloneSpectrumSession(lab, sdk=sdk).open()
    with pytest.raises(TimeoutError, match="No completion"):
        session.run(compiled)
    (file,) = session.directory.glob("run_*/execution_receipt.json")
    receipt = json.loads(file.read_text(encoding="utf-8"))
    assert receipt["execution_state"] == "unknown"
    assert receipt["completed_shots"] == [{"point": 0, "shot": 0}]
    assert ("close",) == sdk.log[-1]
    assert ("output", 1, False) in sdk.log[-5:]
    with pytest.raises(SpectrumRuntimeError, match="closed"):
        session.open()


def test_dds_extra_execution_trigger_invalidates_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lab, compiled = shot_input("dds")
    sdk, card = dds_sdk()
    cmd = card.cmd

    def extra_trigger(command):
        cmd(command)
        card.dds.counter += 1

    card.cmd = extra_trigger
    with StandaloneSpectrumSession(lab, sdk=sdk) as session:
        with pytest.raises(SpectrumRuntimeError, match="extra triggers"):
            session.run(compiled)
        with pytest.raises(SpectrumRuntimeError, match="not connected"):
            session.run(compiled)
    assert card.log[-1] == "close"


def test_dds_empty_fifo_with_last_frame_in_shadow_times_out(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lab, compiled = shot_input("dds")
    sdk, card = dds_sdk()
    elapsed = iter(range(0, 60, 10))
    monkeypatch.setattr(
        standalone_card,
        "time",
        SimpleNamespace(monotonic=lambda: next(elapsed), sleep=lambda _: None),
    )

    def lose_terminal_timer(command):
        assert command == sdk.M2CMD_CARD_FORCETRIGGER
        while card.dds.queue:
            card.advance()
        assert card.dds.queue_cmd_count() == 0
        assert card.dds.shadow[(0, "amp")] == 0
        assert card.active[(0, "amp")] > 0

    card.cmd = lose_terminal_timer
    with StandaloneSpectrumSession(lab, sdk=sdk) as session:
        with pytest.raises(SpectrumRuntimeError, match="terminal mute did not execute"):
            session.run(compiled)
        (file,) = session.directory.glob("*/execution_receipt.json")
        receipt = json.loads(file.read_text(encoding="utf-8"))
        assert receipt["status"] == "failed" and not receipt["completed_shots"]
        assert "stop" in card.log[-5:]
        assert ("output", 0, False) in card.log[-5:]
        assert card.log[-1] == "close"


def test_failed_identity_reprobe_after_completed_run_closes_card(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lab, compiled = shot_input("awg", frequencies=(10,))
    sdk, card = awg_sdk()
    with StandaloneSpectrumSession(lab, sdk=sdk) as session:
        session.run(compiled)
        card.sn = lambda: 202
        with pytest.raises(SpectrumRuntimeError, match="declared physical card"):
            session.run(compiled)
        assert sdk.log[-1] == ("close",)
        assert ("output", 0, False) in sdk.log[-5:]


def test_reject_other_contract_before_card_programming(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lab, _ = shot_input("awg")
    _, dds = shot_input("dds")
    sdk, _ = awg_sdk()
    with StandaloneSpectrumSession(lab, sdk=sdk) as session:
        with pytest.raises(ValueError, match="differs from the connected lab"):
            session.run(dds)
        assert sdk.log == [("open",)]
