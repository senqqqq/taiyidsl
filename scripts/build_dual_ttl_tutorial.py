"""Build the Chinese two-card bench lesson. Generation never opens an instrument."""

# As in the other lesson generators, preserve the Markdown rows and code of
# complete cells. The generated notebook is separately formatted and linted.
# ruff: noqa: E501
from pathlib import Path
from textwrap import dedent

import nbformat
from tutorial_notebook_format import write_formatted_notebook

OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "docs/tutorials/artiq_spectrum_dual_ttl_clock_zh.ipynb"
)


def main():
    cells = []

    def add(kind, uid, text):
        cells.append(kind(dedent(text).strip(), id=uid))

    md, code = nbformat.v4.new_markdown_cell, nbformat.v4.new_code_cell
    add(
        md,
        "wiring",
        """
        # 一个 Kasli、两张 Spectrum 卡、一个 shot

        | Kasli 触发输出 | Spectrum 接线 | 示波器上看什么 |
        |---|---|---|
        | `ttl20`（RTIO `0x14`） | `/dev/spcm0` 的 EXT0；该卡 CH0 → 示波器 CH1 | 200 kHz，每段 10 个周期 |
        | `ttl21`（RTIO `0x15`） | `/dev/spcm1` 的 EXT0；该卡 CH0 → 示波器 CH2 | 400 kHz，每段 20 个周期 |

        每张卡播放 **50 µs → 暗 25 µs → 50 µs**，第二段附加 90°。两个 64 ns 的 TTL 脉冲由同一个 Kasli kernel 在相同的 RTIO 时间戳发出；不需要 Python 连续发两次触发。两卡各用自己的内部 PLL（1.25 GS/s），不用外参考或校准文件。**同时发出 TTL 不等于两个 CH0 的起点、频率或相位已经同步。** 下图只预览编译样本，实际上升沿和相对延迟请用示波器测量。

        在项目 `.venv` 内核运行。默认 Run All 只编译和画图，不打开设备。
    """,
    )
    add(
        code,
        "inputs",
        """
        import math
        import os
        import runpy
        from pathlib import Path

        import matplotlib.pyplot as plt
        import numpy as np
        from taiyidsl import DeviceSetup, Experiment, LabConfig, Qubit, Square, delay, kHz, play, us
        from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window
        from taiyidsl.config.descriptor import resolve_device

        ROOT = next(p for p in (Path.cwd(), *Path.cwd().parents) if (p / "src/taiyidsl").is_dir())
        DEVICE_DB = None  # 实机：换成与当前 gateware 匹配的可信 device_db.py 路径
        SERIAL_0 = "OFFLINE-0"  # /dev/spcm0 铭牌序列号（字符串）
        SERIAL_1 = "OFFLINE-1"  # /dev/spcm1 铭牌序列号（字符串）
        ARTIQ_PYTHON = os.environ.get("TAIYIDSL_ARTIQ_PYTHON") or str(ROOT / "local_config/hardware-worker/bin/taiyidsl-artiq-python")
    """,
    )
    add(
        code,
        "configuration",
        """
        if DEVICE_DB is None:
            db = {"core": {"type": "local", "module": "artiq.coredevice.core", "class": "Core",
                           "arguments": {"host": None, "ref_period": 1e-9, "target": "rv32g"}}}
            for n in (20, 21):
                db[f"ttl{n}"] = {"type": "local", "module": "artiq.coredevice.ttl", "class": "TTLOut", "arguments": {"channel": n}}
        else:
            db = runpy.run_path(str(DEVICE_DB))["device_db"]  # 只加载设备声明，不运行实验
        # 只将本实验需要的三个设备交给 worker。不要把相机触发输出当作 AWG 触发。
        devices = {"core": resolve_device(db, "core")[1]}
        camera_channel = resolve_device(db, "ttl_Orca_Cam")[1]["arguments"]["channel"] if "ttl_Orca_Cam" in db else None
        for n in (20, 21):
            ttl = resolve_device(db, f"ttl{n}")[1]
            if (ttl.get("module"), ttl.get("class"), ttl["arguments"]["channel"]) != ("artiq.coredevice.ttl", "TTLOut", n) or n == camera_channel:
                raise ValueError(f"请核对 ttl{n} 的接线、输出方向、RTIO 通道和相机用途。")
            devices[f"ttl{n}"] = ttl

        # 时钟来源属于卡；是否使用实测校正属于同步组。64 samples 是台架估计，不是测量。
        awg = dict(active_channels=[0], output_range_mv=500, trigger_latency_samples=64, clock_policy="internal_pll_bench")
        spec_db = {
            "spcm0": dict(awg, serial_number=SERIAL_0, address="/dev/spcm0"),
            "spcm1": dict(awg, serial_number=SERIAL_1, address="/dev/spcm1"),
        }
        descriptor = {
            "backend": "artiq_spectrum",
            "instruments": {"core": {"kind": "artiq", "device": "core"},
                            "spcm0": {"kind": "spectrum", "device": "spcm0"},
                            "spcm1": {"kind": "spectrum", "device": "spcm1"}},
            "channels": {"ch0_a": {"instrument": "spcm0", "channel": 0}, "ch0_b": {"instrument": "spcm1", "channel": 0}},
            "signals": {"q0": {"clock": "ch0_a"}, "q1": {"clock": "ch0_b"}},
            "sync_groups": {"pair": {"cards": ["spcm0", "spcm1"], "triggers": {"spcm0": "ttl20", "spcm1": "ttl21"}, "output_basis": "setpoints_only"}},
        }
        setup = DeviceSetup.from_descriptor(descriptor, device_db=devices, spec_db=spec_db)
        q0, q1 = Qubit("q0", signals={"clock": "q0.clock"}), Qubit("q1", signals={"clock": "q1.clock"})
        lab = LabConfig(setup, qubits=[q0, q1], artiq_python=ARTIQ_PYTHON,
                        emulation=DEVICE_DB is None or not (SERIAL_0.isdecimal() and SERIAL_1.isdecimal()))
    """,
    )
    add(md, "experiment-title", "## 同一个触发组，两个输出并行播放")
    add(
        code,
        "one-shot",
        """
        pulse_a = Square(50 * us, amplitude=0.2, frequency=200 * kHz)
        pulse_b = Square(50 * us, amplitude=0.2, frequency=400 * kHz)
        gap = 25 * us
        second_phase = math.pi / 2
        exp = Experiment("two_ttls_one_shot", [q0, q1])
        with exp.average(1), exp.realtime(), exp.triggered_sequence(group="pair"):
            with exp.parallel():
                play(q0.clock, pulse_a)
                play(q1.clock, pulse_b)
            delay(gap)
            with exp.parallel():
                play(q0.clock, pulse_a, phase=second_phase)
                play(q1.clock, pulse_b, phase=second_phase)
        compiled = lab.compile(exp)
        print("已编译 1 shot；尚未连接仪器。")
    """,
    )
    add(
        code,
        "preview",
        """
        group = next(e.params for e in compiled.points[0].physical_events if e.kind == "spectrum_program")
        fig, axes = plt.subplots(2, 1, sharex=True, figsize=(9, 4), layout="constrained")
        for ax, (name, card) in zip(axes, group["cards"].items(), strict=True):
            program = card["program"]
            y = reconstruct_window(program, 0, program["physical_duration_samples"])[:, 0]
            ax.plot(np.arange(len(y)) * 1e6 / program["card_spec"]["sample_rate_hz"], y)
            ax.set_ylabel(f"{name} CH0\\nDAC code")
        axes[-1].set_xlabel("Time from each card's replay start (µs)")
        plt.show()
    """,
    )
    add(
        md,
        "live-instructions",
        """
        ## 实机只发一次

        填好设备库和两张卡的铭牌号后，重新运行上方单元。独占 Kasli 和两张卡；确保两卡已分别设为 **Internal PLL、1.25 GS/s**。EXT0 为上升沿、默认 1 V 和 50 Ω，CH0 接示波器 50 Ω；分接 TTL 时用高阻探头。确认接线、无相机或其他实验占用，示波器设为单次采集，才把下一格改为 `True`。

        卡都上传且就绪后才会执行该 shot。`core.reset()` 会清空待执行的 RTIO 事件；退出 session 会停止卡并释放设备。出错时不要自动重试。时钟不符也不会自动切换。[时钟与校准选项](../hybrid_session_options.md) · [Nix worker 配置](../nix_hardware_worker.md)
    """,
    )
    add(
        code,
        "explicit-run",
        """
        RUN_EXPERIMENT = False
        if RUN_EXPERIMENT:
            with lab.connect() as session:
                receipt = session.run(compiled)
            print(receipt["spectrum_receipts"][0]["receipt"]["cards"])
    """,
    )
    notebook = nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3 (ipykernel)",
                "language": "python",
                "name": "python3",
            }
        },
    )
    write_formatted_notebook(notebook, OUTPUT)


if __name__ == "__main__":
    main()
