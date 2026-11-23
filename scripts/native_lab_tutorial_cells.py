"""Runnable Chinese lesson cells for native ARTIQ and Spectrum clock spectroscopy."""

from textwrap import dedent


def demo_cells():
    """Build the offline worked example, using ROOT and OUT from the first cell."""
    cells = []

    def markdown(source):
        cells.append(("markdown", dedent(source).strip()))

    def code(source):
        cells.append(("code", dedent(source).strip()))

    markdown("""
        ## 在离线实验台上走完一个 shot

        先用仓库自带的四个 ARTIQ 类练习接线、注册与定时。三个准备阶段共用一根 TTL，
        第四个类给相机发触发脉冲。它们的实现很短，适合与生成的顶层 kernel 对照阅读。
        这里把 Spectrum 卡 B 的 CH0 接到假想的 578 nm 探测链路，
        由另一根 ARTIQ TTL 触发卡 B 的 EXT0。
        上游用 `AOM_578_Clock` 指向 `urukul6_ch3`。实验室若改用 Spectrum 驱动
        这条光路，须确认实际 RF 接线和切换方式，查明旧 DDS 驱动何时退出。
        运行这些单元只会读取源码、编译并写本地文件。

        | 一条信号在哪里定义 | 此例中可以核对的内容 |
        |---|---|
        | ARTIQ `device_db` | core、原生阶段的两个 TTL、AWG 的触发 TTL |
        | Spectrum `spec_db` | 卡标识、采样率、参考时钟、输出范围及示例触发延迟 |
        | `descriptor` | `q.clock → clock_drive → B/CH0`、`awg_trigger_b → B/EXT0` |
        | `Qubit.parameters.clock` | 教学探测脉冲的电载波中心频率 |

        本例沿用教学设备库中的卡 B，只启用一个输出。合并两份**教学**设备表前检查重名；
        上机时从实验室的完整设备表读取，逐根核对 TTL、别名与物理接线。
    """)
    code("""
        import json
        from pprint import pprint

        import numpy as np
        from IPython.display import Image, display
        from taiyidsl import (
            DeviceSetup, Experiment, LabConfig, NativeModule, NativeProgram,
            NativeStage, Qubit, Square, SweepParameter, delay, kHz, play, us,
        )
        from examples.artiq_spectrum.ramsey_lab.device_db import device_db as example_db
        from examples.artiq_spectrum.ramsey_lab.spec_db import spec_db as card_db

        native_root = ROOT / "examples" / "artiq_spectrum"
        native_dir = native_root / "native_stages"
        # 此表只补充教学原生类要用的两根 TTL。覆盖重名项会丢失原有接线。
        extra_db_file = native_dir / "device_db_offline.json"
        native_db = json.loads(extra_db_file.read_text(encoding="utf-8"))
        assert native_db.keys().isdisjoint(example_db)
        fixture_db = example_db | native_db
        fixture_cards = {"B": {**card_db["B"], "active_channels": [0]}}

        descriptor = {
            "name": "offline_clock_spectroscopy",  # 接线方案名，便于追溯。
            "backend": "artiq-spectrum",  # core 排时，Spectrum 生成 DAC 样本。
            "instruments": {
                # 左侧是 descriptor 内的名字，device 是相应设备库中的键。
                "rtio": {"kind": "artiq", "device": "core"},
                "B": {"kind": "spectrum", "device": "B"},
            },
            "channels": {
                # channel=0 是卡 B 的模拟 CH0。触发通道则引用 ARTIQ 设备名。
                "clock_drive": {"instrument": "B", "channel": 0},
                "clock_trigger": {"instrument": "rtio", "device": "awg_trigger_b"},
            },
            "sync_groups": {
                # 后文用 group="clock_awg" 调用本组。B 的 EXT0 接 clock_trigger。
                "clock_awg": {"cards": ["B"], "triggers": {"B": "clock_trigger"}},
            },
            "signals": {"q": {"clock": "clock_drive"}},  # 逻辑角色 → 物理输出。
        }
        setup = DeviceSetup.from_descriptor(
            # descriptor 定义映射，device_db 解析 core/TTL，spec_db 解析卡 B。
            descriptor, device_db=fixture_db, spec_db=fixture_cards
        )
        # "q" 是实例 ID；signals 绑定角色；frequency 是假定的电载波中心。
        q = Qubit("q", signals={"clock": "q.clock"},
                  parameters={"clock": {"frequency": 200 * kHz}})
        # setup 提供接线，qubits 注册实例，emulation=True 启用离线模式。
        lab = LabConfig(setup=setup, qubits=[q], emulation=True)
        pprint(setup.connections())
        pprint(setup.trigger_connections())
    """)
    markdown("""
        ### 读一个类，再注册四个组件

        下面直接显示本次要注册的四个源文件。先对照 `MOT.build()` 里绑定的设备名，
        再看 `prepare_MOT()` 里的 `break_realtime()` 和 GM 方法的最后一个边沿。
        原工程接入时也从这两处入手。类里声明的每个外设都应能在实验室设备库里解析。
    """)
    code("""
        for filename in ("mot.py", "cooling.py", "state_prep.py", "readout.py"):
            print(f"—— {filename} ——")
            print((native_dir / filename).read_text(encoding="utf-8"))
    """)
    markdown("""
        `root` 指向**包含**顶层 Python 包 `native_stages` 的目录；`entry` 写成
        `包.模块:类`。这一步读取源码并固定哈希。实验定义里的 `native.GM_cooling(...)`
        只会记录未来要执行的方法和参数。导出的顶层 kernel 会在对应位置调用它。

        一个模块对应一个 ARTIQ 实例。这里让 MOT 独占共享准备线的初始化；
        cooling 和 state prep 可以按阶段操作同一根线。相机有独立 TTL。
        `initialize` 在原生会话开始时执行，`after_shot` 在每个完整 shot 后执行，
        `finalize` 在会话结束时执行。真实组件还需梳理共享 DDS、CPLD 和总线的初始化归属。
    """)
    code("""
        # ① 每个声明描述一个类。from_package 在注册时读取源码与依赖。
        mot = NativeModule.from_package(
            root=native_root,  # 包的父目录，本例是 examples/artiq_spectrum。
            entry="native_stages.mot:MOT",  # native_stages/mot.py 中的 MOT 类。
            resources=("native_prepare",),  # MOT 使用的外设名；core 隐式共享。
            # MOT.build(self) 没有配置形参，故 build_kwargs 留空。
            # 有 build(self, mode) 时可传 build_kwargs={"mode": "green"}。
            # 它固定于本次实例构建，不能在这里传 SweepParameter。
            initialize="initialize",  # 首个 shot 前调用一次，准备线置低。
            after_shot="after_shot",  # 每个完整 shot 后使准备线恢复低电平。
            finalize="finalize",  # 会话结束时调用一次，恢复准备线。
            # 若要读静态宿主配置，可设 host_prepare="实际宿主方法名"。
            # 该钩子每个会话一次。本例没有宿主准备方法，故省略。
            # 此包的静态依赖都在 root 内。若有根目录外的受审源码，可用
            # source_overrides={"模块名": 源文件路径} 补充，再重新导出。
        )
        # ② Cooling 实例复用准备线。省略 initialize，使 MOT 负责初始化。
        cooling = NativeModule.from_package(
            root=native_root,  # 与 MOT 属于同一源码包。
            entry="native_stages.cooling:Cooling",  # 模块 cooling 中的类。
            resources=("native_prepare",),  # GM 方法会拉高再拉低这根线。
        )
        # ③ 第三个实例也使用准备线；它有自己的方法和实例变量。
        state = NativeModule.from_package(
            root=native_root,
            entry="native_stages.state_prep:StatePreparation",  # 态制备类。
            resources=("native_prepare",),
        )
        # ④ Readout 单独初始化相机 TTL。它发触发，并读取控制末端参考系相位。
        readout = NativeModule.from_package(
            root=native_root,
            entry="native_stages.readout:Readout",  # readout.py 中的读出类。
            resources=("native_camera",),  # 与准备 TTL、AWG 触发都不同。
            initialize="initialize",  # 相机 TTL 在首个 shot 前置低。
        )
    """)
    markdown("""
        ### 给四个可调用阶段标上时序边界

        MOT 含有 `break_realtime()`，把它作为 `prefix`。组合器在最后一个 prefix
        返回后恢复调度余量并建立时间原点。教学 GM 与制备各执行一次定长 TTL 脉冲，
        因此可分别声明为 20 µs、8 µs 的 `timed`。读出在控制段结束后执行，声明为
        10 µs 的 `suffix`。这些长度也作为方法实参传入，顶层 kernel 校验返回游标。
        将生产方法映射到 `timed` 前，要审查其完整调用链，含等待、RPC 或
        `break_realtime()` 的方法应放在 prefix 或重新划分边界。
    """)
    code("""
        gm_hold, state_hold, camera_width = 20 * us, 8 * us, 10 * us
        # modules 建立“组件名 → 声明”的表，供 NativeStage.component 引用。
        # 程序级 initialize/after_shot/finalize/host_prepare 均可指定组件名
        # 元组，用来选择并排序已声明的钩子。这里省略，按 modules 的顺序执行。
        native = NativeProgram(
            modules={"mot": mot, "gm": cooling, "state": state, "camera": readout},
            # stages 的键是 DSL 中 native.<键>(...) 使用的名字。
            # 各阶段的实际调用先后由下一个代码单元的 realtime 主体确定。
            stages={
                "prepare_MOT": NativeStage(
                    component="mot",  # 从 modules 选 MOT 实例。
                    method="prepare_MOT",  # 调用该类上的同名 @kernel。
                    timing="prefix",  # 可变耗时；省略 duration 和 end_boundary。
                ),
                "GM_cooling": NativeStage(
                    component="gm",  # 查找上面的 cooling 声明。
                    method="GM_cooling",  # 真正被调用的方法，接收 duration。
                    timing="timed",  # 在 DSL 控制段之前精确定时。
                    duration=gm_hold,  # 校验从调用到返回共 20 µs。
                    # end_boundary 说明把哪个事件认作结束，由工程师核实。
                    end_boundary="native_prepare falling edge after GM",
                ),
                "state_prep": NativeStage(
                    component="state",  # 与 modules 中的组件名一致。
                    method="state_prep",  # 类方法接收 duration。
                    timing="timed",  # 从前一个 timed 阶段的末端接续。
                    duration=state_hold,  # 声明游标前进 8 µs。
                    # 这里的边界是数字输出，不包含额外的模拟场稳定时间。
                    end_boundary="native_prepare falling edge after state preparation",
                ),
                "readout": NativeStage(
                    component="camera",  # 选择独立的 Readout 实例。
                    method="execute_readout",  # DSL 名为 readout，类方法可不同。
                    timing="suffix",  # 在 DSL 控制段结束后调用。
                    duration=camera_width,  # 仅指 10 µs 相机 TTL 脉宽。
                    # 此方法读取 native_terminal_f_q，即控制末端的参考系相位。
                    # 前三阶段使用默认 False；普通触发可不依赖相位。
                    phase_sensitive=True,
                    end_boundary="native_camera falling edge",  # TTL 下降沿。
                ),
            },
            # 键是设备名，值列全使用它的组件。只给 mot 注册了准备线初始化。
            # 漏列 gm、或给 gm 也加 initialize，都会导致共享资源校验失败。
            shared_resources={"native_prepare": ("mot", "gm", "state")},
        )
    """)
    markdown("""
        ### 写 578 nm 单脉冲频谱扫描

        每个失谐点都重新装载、冷却、制备、探测并触发读出。示例取五个**光学失谐**
        Δν，假设从 AWG 电频率到光学频移的有符号斜率 α 为 +1 Hz/Hz，示例中心为
        200 kHz 电载波，故命令为 `f_AWG = f_center + Δν / α`。
        578 nm 表示目标跃迁的光波长。实机要用已标定的 RF/IF 中心和变频关系，
        按所选衍射级、通光次数和混频边带确认符号与倍率；下列 200 kHz 和 +1
        仅用于演示换算。频率扫描只施加一次。

        原生调用直接写在 `realtime()` 内；仅探测脉冲写在 `triggered_sequence`
        内。state prep 的末边沿与探测脉冲之间预留 10 µs，供本例触发提前量使用。
        真实电路还需核实模拟场稳定时间和光路传播延迟。固定探测宽度和幅度，便于
        把观测量按光学失谐归档。每点做两次 shot，用于检查索引。
    """)
    code("""
        def clock_spectroscopy(q, native, probe, awg_frequency, *,
                               gm_time, state_time, guard, readout_time, shots):
            # q 是已注册的 qubit，native 是上一步具名阶段的集合。
            exp = Experiment("native_578_clock_spectroscopy", [q])
            # average 规定每点 shot 数；sweep 绑定本点频率；realtime 包住一发。
            with exp.average(shots), exp.sweep(awg_frequency), exp.realtime():
                # 类方法 prepare_MOT(self) 只有 self，调用者传零个实参。
                native.prepare_MOT()
                # 按方法签名绑定到 GM_cooling(self, duration) 的 duration。
                # 这是每次调用的实参；NativeStage.duration 只负责核对游标。
                native.GM_cooling(gm_time)
                # state_prep 同样以第一个位置实参接收教学 TTL 的高电平时长。
                native.state_prep(state_time)
                # 让上一个 timed 阶段结束后有明确空档，留给 AWG 提前触发。
                delay(guard)
                # group 对应 descriptor.sync_groups 的键，触发路由由它决定。
                with exp.triggered_sequence(group="clock_awg"):
                    # 信号选卡口，probe 给包络，frequency 覆盖本点电载波。
                    play(q.clock, probe, frequency=awg_frequency)
                # 回到 realtime 的直接子层，绑定 execute_readout(duration)。
                # 只发相机 TTL。曝光、取帧与状态判别属于实验室读出链。
                native.readout(readout_time)
            return exp

        optical_detuning_hz = [-20_000, -10_000, 0, 10_000, 20_000]
        optical_hz_per_awg_hz = +1.0
        center_hz = q.parameters.clock.frequency
        drive_hz = [center_hz + delta / optical_hz_per_awg_hz
                    for delta in optical_detuning_hz]
        # 扫描参数的名字、每点电频率列表、列表数值的单位。
        frequency_axis = SweepParameter("awg_frequency", drive_hz, "Hz")
        # 方形包络的持续时间与归一化幅度；每点的频率在 play 时给出。
        probe = Square(duration=40 * us, amplitude=0.2)
        exp = clock_spectroscopy(
            q, native, probe, frequency_axis,
            # 三个原生方法的时长实参复用前文合同值，guard 属于控制空档。
            gm_time=gm_hold, state_time=state_hold,
            guard=10 * us, readout_time=camera_width, shots=2,
        )
        # 修改 gm_hold 后，须重建 native 和 exp 再 compile，否则旧合同仍在。
        # 单改调用实参会使实际游标与旧合同不符，运行至边界校验时会报错。
        compiled = lab.compile(exp)
        execution_order = list(compiled.execution_order())
        print("扫描点、每点 shot 数、总 shot 数：",
              len(compiled.points), compiled.shots, len(execution_order))
    """)
    markdown("""
        ### 核对 AWG 输出与顶层 kernel

        从每个编译点取出探测事件的实际频率，再从最终的分段表回放探测前 8 µs 的整数
        DAC 样本。下图时间以探测脉冲的逻辑起点为零，纵轴是相对于满量程的输出。
        运行后应看到五个不同频率，且各点的实际样本不同。此图可发现
        “只扫了表格标签、没有改变硬件指令”这类接线或参数错误。原子响应需要现场采集。
    """)
    code("""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        assert len(compiled.points) == len(drive_hz)
        assert len(execution_order) == len(drive_hz) * compiled.shots
        fig, ax = plt.subplots(figsize=(8, 3.4))
        samples = []
        for point, expected, delta in zip(
            compiled.points, drive_hz, optical_detuning_hz, strict=True
        ):
            event, = [e for e in point.logical_events if e.kind == "play"]
            assert event.signal == q.clock and event.params["frequency"] == expected
            start_s = event.start_mu * float(compiled.setup.tick_seconds)
            window = compiled.simulate_output(point=point.index, start_s=start_s,
                                              duration_s=8e-6)
            trace, = [t for t in window["traces"]
                      if (t["card"], t["channel"]) == ("B", 0)]
            assert np.any(trace["dac_codes"])
            samples.append(trace["dac_codes"].tobytes())
            ax.plot((trace["time_s"] - start_s) * 1e6, trace["normalized"],
                    label=f"{delta / 1000:+g} kHz")
            print(f"point {point.index}: Δν {delta:+g} Hz, AWG {expected:g} Hz")
        assert len(set(samples)) == len(drive_hz)
        ax.set(xlabel="Time from probe start (us)", ylabel="DAC / full scale")
        ax.legend(title="Optical offset", loc="center left", bbox_to_anchor=(1, 0.5))
        fig.tight_layout()
        figure_path = OUT / "clock_probe_waveforms.png"
        fig.savefig(figure_path, dpi=140)
        plt.close(fig)
        display(Image(filename=str(figure_path)))
    """)
    code("""
        export_dir = OUT / "export"
        compiled.export(export_dir)
        compiled.visualize(OUT / "pulse_sheet.html", waveform_path="waveform.html")
        compiled.waveform_simulator(OUT / "waveform.html")
        hybrid = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
        core_manifest = export_dir / "artiq/manifest.json"
        artiq_manifest = json.loads(core_manifest.read_text(encoding="utf-8"))
        composition = artiq_manifest["native_composition"]
        assert hybrid["backend"] == "artiq_spectrum"
        assert hybrid["verification"]["offline"] == "passed"
        assert composition["execution"] == "one_top_level_kernel_per_shot"
        first = composition["points"][0]
        assert [s["stage"] for s in first["prefix"]] == ["prepare_MOT"]
        assert [s["stage"] for s in first["timed"]] == ["GM_cooling", "state_prep"]
        assert [s["stage"] for s in first["suffix"]] == ["readout"]
        for section in ("prefix", "timed", "suffix"):
            pprint({section: first[section]})
        print("生成的 ARTIQ 顶层 kernel：", export_dir / "artiq/experiment.py")
        print("脉冲表：", OUT / "pulse_sheet.html")
        print("最终 DAC 波形：", OUT / "waveform.html")
    """)
    markdown("""
        在 `export/artiq/experiment.py` 中搜索 `def point_0(`，
        按顺序找到 MOT、恢复调度余量、GM、state prep、控制段调用和相机触发。
        `prefix` 的耗时没有固定值，所以不能从 pulse sheet 倒推出 MOT 开始时刻。
        查看 `export/experiment.py` 可找到混合实验的宿主入口。交付时应保留完整
        `export/`、匹配的原生 Python 源和实验室的读出适配器；在实际 worker 里让
        `native_stages` 或替换后的实验室包可导入。

        真机采集要记录 point、shot、电载波、光学失谐和帧 ID。
        根据已验证的态选择读出方式计算每点激发概率，再给出有效 shot 数与不确定度。
        本例中的 `native_camera` 只发 TTL；此处的十个计划 shot 没有任何实测激发概率。
    """)
    return cells
