"""Build the Chinese standalone Spectrum clock-probe bench tutorial."""

# Keep notebook code cells and Markdown links readable in the rendered lesson.
# ruff: noqa: E501
import hashlib
from pathlib import Path
from textwrap import dedent

import nbformat
from tutorial_notebook_format import write_formatted_notebook

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "docs/tutorials/spectrum_clock_578_zh.ipynb"


def build():
    cells = []

    def add(kind, source):
        source = dedent(source).strip()
        cell = kind(source)
        cell.id = hashlib.sha256(source.encode()).hexdigest()[:16]
        cells.append(cell)

    md = nbformat.v4.new_markdown_cell
    code = nbformat.v4.new_code_cell
    add(
        md,
        """
        # 单卡 Spectrum 调试：为 578 nm 钟谱产生探测脉冲

        **先在示波器上认出一个 40 µs 射频脉冲，再确认五个频点都按预期改变。**
        本课使用一张 M4i.6631-x8 的 AWG 模式，由卡内时钟安排波形，主机每个 shot
        发一次软件触发。接线从 **Spectrum CH0 → 示波器 50 Ω 输入** 开始。
        验证电信号之后，再按实验室标定的混频、AOM 和光路接入 578 nm 探测系统。

        首次使用：在 TaiyiDSL 源码目录执行 `uv sync --locked --group notebooks`，
        在 VS Code 打开本文件，选择项目已有的 `.venv` 运行「全部运行」。默认路径
        会导出卡程序并显示量化后的 DAC 波形。最后两格给出实卡入口，调用行默认注释。

        这张卡只控制探测电波形。要对同一批原子完成装载、态制备、同步探测与读出，
        见[混合后端教程](native_mot_clock_zh.ipynb)。两个程序若使用同一张卡，需分时使用；
        单卡播放会停止当前回放、重新配置整卡的通道、时钟与触发。
    """,
    )
    add(
        code,
        """
        import json
        from pathlib import Path

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from IPython.display import Image, display

        from taiyidsl import (
            DeviceSetup, Experiment, LabConfig, Qubit, Square, SweepParameter, delay, play, us,
        )
        from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window

        ROOT = next(p for p in (Path.cwd(), *Path.cwd().parents)
                    if (p / "src/taiyidsl").is_dir())
        OUT = ROOT / "output/spectrum_clock_578"
        OUT.mkdir(parents=True, exist_ok=True)
    """,
    )
    add(
        md,
        """
        ## 1　声明卡、物理输出和逻辑信号

        `backend="spectrum"` 选择独立单卡。配置分为两层：`spec_db` 定义卡本身，
        `descriptor` 把实验中的 `q0.clock` 接到该卡的 CH0。这个后端使用内部 PLL
        和软件触发，无需 ARTIQ 环境、`device_db.py`、外触发线或外部参考时钟。
    """,
    )
    add(
        code,
        """
        SAMPLE_RATE_HZ = 1_250_000_000  # M4i.6631-x8 在本后端的采样率，0.8 ns/点。
        RANGE_MV = 500                 # 选定输出量程，供驱动设置和名义电压换算使用。
        q0 = Qubit("q0", signals={"clock": "q0.clock"})

        def clock_lab(serial, *, emulation):
            spec_db = {
                "A": {
                    "serial_number": serial,     # 实卡用铭牌上的十进制序列号字符串。
                    "model": "M4i.6631-x8",      # 驱动会检查型号和序列号。
                    "mode": "awg",               # 生成并下载离散 DAC 样本。
                    "active_channels": [0],      # 此教学接线只用 CH0。
                    "sample_rate_hz": SAMPLE_RATE_HZ,
                    "output_range_mv": RANGE_MV,
                }
            }
            descriptor = {
                "backend": "spectrum",
                "instruments": {"A": {"kind": "spectrum", "device": "A"}},
                "channels": {"probe_out": {"instrument": "A", "channel": 0}},
                "signals": {"q0": {"clock": "probe_out"}},
            }
            setup = DeviceSetup.from_descriptor(descriptor, spec_db=spec_db)
            return LabConfig(setup, qubits=[q0], emulation=emulation)

        offline_lab = clock_lab("OFFLINE", emulation=True)
        print(offline_lab.setup.connections())
    """,
    )
    add(
        md,
        """
        ## 2　把光学失谐换算为 AWG 电频率

        为了与混合后端的图直接对照，取 **200 kHz 教学中频**，在中心两侧各扫
        10、20 kHz。578 nm 是光波长；示波器测到的是射频链输入的电载波。
        写成 `f_AWG = f_center + Δν_opt / slope`：`slope` 是光学频移随
        AWG 频率变化的有符号倍率。混频边带、AOM 衍射级与单程/双程影响该值。
        本例取 +1，得到 180、190、200、210、220 kHz。接入实验台时请填写实测的
        中心和倍率；真实原生钟谱中 Urukul 的工作频率不能直接作为这里的中频。
    """,
    )
    add(
        code,
        """
        CENTER_HZ = 200_000.0
        OPTICAL_HZ_PER_AWG_HZ = +1.0
        OPTICAL_OFFSETS_HZ = [-20_000, -10_000, 0, 10_000, 20_000]
        LEAD_US, WIDTH_US, TAIL_US = 8.0, 40.0, 8.0
        AMPLITUDE = 0.2  # 相对 DAC 满量程；不是光功率，也不是原子 Rabi 频率。

        def probe_experiment(qubit, pulse, drive_hz, *, shots, lead, tail):
            # drive_hz 是每个点实际送往 DAC 的电载波，单位显式指定为 Hz。
            frequency = SweepParameter("drive_hz", drive_hz, "Hz")
            exp = Experiment("single_card_578_probe", [qubit])
            # cyclic 先完整扫一轮频点，再下一轮。每个 point/shot 启动一次整段回放。
            with exp.average(shots, mode="cyclic"), exp.sweep(frequency), exp.realtime():
                delay(lead)  # 从卡本次回放开始计时，先保持零输出。
                play(qubit.clock, pulse, frequency=frequency)  # 每点仅在这里施加频率。
                delay(tail)  # 留出零输出窗口，便于观察关断。
            return exp

        drive_hz = [CENTER_HZ + d / OPTICAL_HZ_PER_AWG_HZ for d in OPTICAL_OFFSETS_HZ]
        probe = Square(WIDTH_US * us, amplitude=AMPLITUDE)  # 方形包络，内含电载波。
        scan = probe_experiment(q0, probe, drive_hz, shots=2, lead=LEAD_US*us, tail=TAIL_US*us)
        compiled = offline_lab.compile(scan)
        files = compiled.export(OUT / "scan")
        manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
        assert manifest["backend"] == "spectrum" and manifest["verification"]["offline"] == "passed"
        print("单卡导出", files["manifest"])
        print("主机执行顺序 (point, shot)", list(compiled.execution_order()))
    """,
    )
    add(
        md,
        """
        ## 3　从卡的最终回放样本检查频率、脉宽与静默段

        每个频点编译成一个 `spectrum_shot`。`reconstruct_window` 按卡的段表、步表和
        循环次数恢复实际 int16 样本；它是两个后端共用的回放工具。下格检查每点的脉冲
        起止位置与前后零样本，还用脉冲内部的上升过零间距独立估频。表格中的“回放”
        数值来自这些样本，模拟电压刻度按配置量程换算。卡可能在逻辑 shot 后填零以对齐段长。
    """,
    )
    add(
        code,
        """
        fig, (full, zoom) = plt.subplots(2, 1, figsize=(8, 5), layout="constrained")
        print("光学偏移(kHz)   请求电载波(kHz)   回放估频(kHz)   名义峰峰值(mV)")
        for point, delta, expected_hz in zip(compiled.points, OPTICAL_OFFSETS_HZ, drive_hz, strict=True):
            shot, = point.physical_events
            assert shot.kind == "spectrum_shot"
            program = shot.params["program"]
            pulse, = program["pulses"]
            rate = program["card_spec"]["sample_rate_hz"]
            start, width = pulse["start_sample"], pulse["duration_samples"]
            assert (start, width) == (round(LEAD_US*1e-6*rate), round(WIDTH_US*1e-6*rate))
            assert program["logical_duration_samples"] == start + width + round(TAIL_US*1e-6*rate)
            codes = reconstruct_window(program, 0, program["physical_duration_samples"])[:, 0]
            assert not codes[:start].any() and not codes[start + width:].any()
            burst = codes[start:start + width]
            rising = np.flatnonzero((burst[:-1] <= 0) & (burst[1:] > 0))
            assert len(rising) >= 2, "探测太短，至少需要两个上升过零点才能估频"
            replay_hz = rate * (len(rising) - 1) / (rising[-1] - rising[0])
            assert np.isclose(replay_hz, expected_hz, rtol=5e-4)
            mv = codes.astype(float) * RANGE_MV / 32767
            t_us = np.arange(len(codes)) * 1e6 / rate
            print(f"{delta/1000:12.1f} {expected_hz/1000:16.2f} "
                  f"{replay_hz/1000:17.2f} {np.ptp(mv):17.1f}")
            if delta == 0:
                full.plot(t_us, mv)
            zoom.plot(t_us, mv, label=f"{expected_hz/1000:g} kHz")
        full.set(title="Center point: complete card shot", ylabel="Nominal mV", xlabel="Card time (us)")
        zoom.set(xlim=(LEAD_US, LEAD_US + 12), xlabel="Card time (us)", ylabel="Nominal mV")
        zoom.legend(ncols=3, fontsize=8)
        figure = OUT / "probe_waveforms.png"
        fig.savefig(figure, dpi=140)
        plt.close(fig)
        display(Image(filename=str(figure)))
    """,
    )
    add(
        md,
        """
        ## 4　示波器上先发一次中心频点，再发整轮扫描

        在实验室安装 Spectrum 原生驱动和 Python SDK（项目目录运行
        `uv sync --locked --group notebooks --extra spectrum`）。从面板确认卡型号、
        序列号和 Sequence Replay 功能，释放其他软件对该卡的占用。先接通 CH0
        到示波器的 50 Ω 输入，在示波器上选单次、CH0 边沿触发；教学幅度可先试
        +30 mV 触发电平。示波器以信号边沿为零点；8 µs 前导按卡的回放起点计算。

        以下函数按填写的序列号建立实际配置、重新编译、导出对应的卡程序，
        再用 `lab.connect()` 打开它。每次调用创建独立会话，程序、频点表和
        驱动回执一起保存在打印出的目录。首次只发中心点一次。在本次接线和量程下，
        示波器应看到约 8 周期、40 µs 的 200 kHz 波列，预期约 200 mV 峰峰值。
        测量和记录实际值；若幅度相差约两倍，先核对端接方式与示波器衰减设置。
    """,
    )
    add(
        code,
        """
        def run_card(serial, optical_offsets_hz, *, shots):
            # 此函数只有被调用时才连接；serial 填实际铭牌上的数字。
            if not isinstance(serial, str) or not serial.isdecimal():
                raise ValueError("请输入真实 Spectrum 卡的十进制序列号")
            offsets = list(optical_offsets_hz)
            frequencies = [CENTER_HZ + d / OPTICAL_HZ_PER_AWG_HZ for d in offsets]
            lab = clock_lab(serial, emulation=False)
            experiment = probe_experiment(
                q0, probe, frequencies, shots=shots, lead=LEAD_US * us, tail=TAIL_US * us
            )
            artifact = lab.compile(experiment)
            with lab.connect() as session:
                folder = session.directory
                print("本次会话目录", folder)
                artifact.export(folder / "program")
                coordinates = [
                    {"point": i, "optical_offset_hz": d, "awg_frequency_hz": f}
                    for i, (d, f) in enumerate(zip(offsets, frequencies, strict=True))
                ]
                (folder / "scan_points.json").write_text(
                    json.dumps(coordinates, indent=2), encoding="utf-8"
                )
                receipt = session.run(artifact)  # 上传、逐 shot 触发并等待卡完成。
            assert receipt["compiled_sha256"] == artifact.sha256
            print("驱动状态", receipt["status"], "已完成", receipt["completed_shots"])
            return receipt

        CARD_SERIAL = ""  # 填自己的卡序列号。先在示波器上点击 Single，再手动运行下一行。
        # center_receipt = run_card(CARD_SERIAL, [0], shots=1)
    """,
    )
    add(
        code,
        """
        # 实测中心点的频率、幅度和脉宽后，在示波器上准备分段采集，再运行整轮。
        # scan_receipt = run_card(CARD_SERIAL, OPTICAL_OFFSETS_HZ, shots=2)
    """,
    )
    add(
        md,
        """
        完整扫描会产生 10 个波列。示波器可启用分段存储，按回执中的 `point, shot`
        对应到 `scan_points.json`，用每段内部的周期检查表中五个频点。连续 shot 的
        间隔由主机调度决定；需要固定的空挡就把它写进同一个 `realtime()` 序列。
        卡出错时先查看会话里的 `run_1/execution_receipt.json`，失败的 shot 可能已发出。

        **从电调试到原子钟谱**：先用示波器确认 RF，再用光电探测器测 578 光脉冲
        的到达时刻与消光。测原子响应时，需要独立的制备与读出系统将每个频点、
        探测开/关分支和帧号对应起来。驱动回执只记录卡执行状态；纵轴上的激发概率
        必须来自这些实测数据。单卡软件触发与 MOT 系统之间没有确定的 shot 相位关系。

        [单卡后端接口与时序](../spectrum_standalone.md) · [混合控制与原子制备](native_mot_clock_zh.ipynb)
    """,
    )
    notebook = nbformat.v4.new_notebook(cells=cells)
    notebook.metadata.kernelspec = {
        "name": "python3",
        "display_name": "Python 3 (TaiyiDSL)",
        "language": "python",
    }
    write_formatted_notebook(notebook, DESTINATION)


if __name__ == "__main__":
    build()
