"""Spectrum 单卡双脉冲扫频。修改下面的参数，保存后在 VS Code 点 Run。"""

import math
from pathlib import Path

from taiyidsl import (
    DeviceSetup,
    Experiment,
    LabConfig,
    Qubit,
    Square,
    SweepParameter,
    delay,
    ns,
    play,
    us,
)

# 1. 设备和运行方式。"run" 会输出波形，请先确认这张卡没有被其他程序占用。
ACTION = "compile"  # "run": 真机播放；"check": 只检查空闲卡；"compile": 不连接仪器
CARD_MODE = "awg"  # 也可选 "dds"，需要卡上装有 DDS20
CARD_SERIAL = "OFFLINE"  # 真机：填卡上标注的序列号，再选 "check" 或 "run"
CARD_ADDRESS = None  # 默认按序列号找卡；也可填写本机设备的绝对路径
CHANNEL = 0  # 0 = CH0，1 = CH1
OUTPUT_RANGE_MV = 500

# 2. 实验参数。每个频点播放一对脉冲，重复 SHOTS_PER_POINT 次。
FREQUENCIES_MHZ = [10, 11, 12]
SHOTS_PER_POINT = 20
PULSE_WIDTH = 640 * us
PULSE_AMPLITUDE = 0.2  # 归一化幅度，不是 mV
PULSE_GAP = 1280 * ns  # 第一脉冲结束到第二脉冲开始的间隔
SECOND_PHASE = math.pi / 2  # 弧度，相对第一脉冲偏移 90°
OUTPUT_DIR = (
    Path(__file__).resolve().parents[1] / "output" / "spectrum_standalone_plain"
)


def two_pulses(q0, pulse, frequencies_mhz, *, gap, second_phase, shots):
    frequency = SweepParameter("frequency", frequencies_mhz, "MHz")
    exp = Experiment("two_pulses", [q0])
    with exp.average(shots), exp.sweep(frequency), exp.realtime():
        play(q0.rf, pulse, frequency=frequency)
        delay(gap)
        play(q0.rf, pulse, frequency=frequency, phase=second_phase)
    return exp


if __name__ == "__main__":
    if ACTION not in ("compile", "check", "run"):
        raise ValueError('ACTION 只能是 "compile"、"check" 或 "run"')

    # 把逻辑信号 q0.rf 连到 A 卡上选定的通道。
    spec_db = {
        "A": {
            "serial_number": CARD_SERIAL,
            "address": CARD_ADDRESS,
            "mode": CARD_MODE,
            "active_channels": [CHANNEL],
            "output_range_mv": OUTPUT_RANGE_MV,
        }
    }
    descriptor = {
        "backend": "spectrum",
        "instruments": {"A": {"kind": "spectrum", "device": "A"}},
        "channels": {"rf_out": {"instrument": "A", "channel": CHANNEL}},
        "signals": {"q0": {"rf": "rf_out"}},
    }
    setup = DeviceSetup.from_descriptor(descriptor, spec_db=spec_db)
    lab = LabConfig(setup, emulation=(ACTION == "compile"))

    # compile 和 run 都先生成相同的实验与波形；check 跳过这一步。
    if ACTION in ("compile", "run"):
        q0 = Qubit("q0")
        pulse = Square(PULSE_WIDTH, amplitude=PULSE_AMPLITUDE)
        exp = two_pulses(
            q0,
            pulse,
            FREQUENCIES_MHZ,
            gap=PULSE_GAP,
            second_phase=SECOND_PHASE,
            shots=SHOTS_PER_POINT,
        )
        compiled = lab.compile(exp)
        destination = OUTPUT_DIR / CARD_MODE
        compiled.export(destination)
        print(f"已导出 {len(compiled.points)} 个频点：{destination}")

    # check 只连接空闲卡。只有 run 才会播放，退出 with 时释放卡。
    if ACTION in ("check", "run"):
        with lab.connect() as session:
            card = session.observed
            print(f"已连接 {card['model']}，S/N {card['serial_number']}。")
            if ACTION == "run":
                receipt = session.run(compiled)
                print(f"已完成 {len(receipt['completed_shots'])} 次 shot。")
                print(f"运行记录：{session.directory}")
