"""Derive a separate transport lesson from the saved plain Ramsey notebook."""

# Tutorial prose is intentionally kept as editable paragraphs.
# ruff: noqa: E501
import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docs/tutorials/ramsey_clock_plain.ipynb"
DESTINATION = ROOT / "docs/tutorials/ramsey_clock_transport.ipynb"


def build():
    base = nbformat.read(BASE, as_version=4)
    cells = []

    def md(source):
        cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))

    def code(source):
        source = textwrap.dedent(source).strip() + "\n"
        formatted = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "format",
                "--stdin-filename",
                "lesson.py",
                "-",
            ],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        cells.append(nbformat.v4.new_code_cell(formatted.strip()))

    def original(prefix):
        found = [
            c.source
            for c in base.cells
            if c.cell_type == "code" and c.source.startswith(prefix)
        ]
        if len(found) != 1:
            raise ValueError(f"Expected one base cell starting with {prefix!r}")
        return found[0]

    md("""
    # Clock Ramsey + 原子往返搬运：A / B / C 共用时间轴

    本版以 [ramsey_clock_plain.ipynb](ramsey_clock_plain.ipynb) 的 **clock_ramsey** 为基础另存。
    保留单个 q0、完整接线、命名 pulse、plain DSL、原生准备/相机读出、第二 RF 相位扫描；
    在 clock 前加入 `move(q0, to=iz0[0], ...)`，clock 后搬回 `q0.position`。
    原 notebook 不覆盖。这里聚焦带搬运的 clock Ramsey，普通 RF Ramsey 仍见原版。
    上手顺序保持 **device_db / spec_db → descriptor → LabConfig 与 Qubit → native ARTIQ → 实验定义 → 编译与可视化 → 显式连接实机**；额外的 DDS 命令检查放在实验之后。

    | 卡 | 模式 / 用途 | 本例输出 |
    |---|---|---|
    | A | AWG / 全局 RF | 31 kHz，前后各100 μs，幅度0.2 |
    | B | AWG / IZ clock、Rydberg | clock 200 kHz / 200 μs / 0.2；Rydberg通道为零 |
    | C | 原生 DDS20 / X、Y AOD | q0从(0,0) μm搬到(30,10) μm，再回到(0,0)；两路持续非零 |

    默认 Run All **只离线编译与导出**，不 connect、不执行仪器。MOT是原版TTL fixture，
    没有模拟原子制备；相机TTL也不代表已经得到图像。位置、频率标定、触发延迟均为示例。
    图中的位置是计划位置，C曲线是DDS命令预测，不是相机观测或最终DAC样本。

    环境沿用项目 `uv sync --locked --group notebooks`，选择项目CPython3.12 kernel。
    输出单独保存在 `output/ramsey_clock_transport/`。配置、参数或实验修改后，从相关单元格往后重跑。
    """)
    code("""
    import json
    from pathlib import Path

    import numpy as np
    from IPython.display import Markdown, display

    from taiyidsl import (
        IZ, DeviceSetup, Experiment, Hz, LabConfig, MoveProfile, NativeKernelPackage,
        Position, Qubit, Square, SweepParameter, TransportConfig,
        delay, kHz, move, ms, ns, play, reset_phase, um, us,
    )

    ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src/taiyidsl").is_dir())
    NOTEBOOK = ROOT / "docs/tutorials/ramsey_clock_transport.ipynb"
    OUTPUT = ROOT / "output/ramsey_clock_transport"
    OUTPUT.mkdir(parents=True, exist_ok=True)

    """)
    md("""
    ## 1. device_db 和 spec_db：第三张卡单独选择 DDS

    以下直接展示完整配置，不连接驱动。沿用原版 A/B、MOT和camera的设备编号，
    新增 `dds_trigger_c` 的示例 RTIO channel 4；实际实验室须引用真实存在且未被占用的TTL。
    三张卡各接一条外触发线，共用参考时钟。`SIM_*` 与文档用 core 地址不能作为实机配置。
    """)
    source = original('"""OFFLINE example:')
    source += '\n\ndevice_db["dds_trigger_c"] = {\n    "type": "local",\n    "module": "artiq.coredevice.ttl",\n    "class": "TTLOut",\n    "arguments": {"channel": 4},\n}'
    code(source)
    code(
        original('"""Project configuration,')
        + """

spec_db["A"]["mode"] = "awg"
spec_db["B"]["mode"] = "awg"
spec_db["C"] = {
    "address": "/dev/spcm2",
    "serial_number": "SIM_C",
    "model": "M4i.6631-x8",
    "mode": "dds",
    "active_channels": [0, 1],
    "sample_rate_hz": 1_250_000_000,
    "reference_clock_hz": 125_000_000,
    "output_range_mv": 500,
    "dds_routing": "16/5",
    "dds_trigger_latency_ns": 560.0,  # 示意时序声明，必须独立测量
}
"""
    )
    md("""
    A/B仍沿用原版的500/520 samples延迟声明，即400/416 ns。C使用独立的560 ns示例声明，
    不能复用AWG的延迟。下面把共同触发提前量设为1200 ns，所以前缀分别是：
    A 800 ns、B 784 ns、C 640 ns。这些不同的前缀补偿用于对齐同一个逻辑入口。
    A/B的前缀为零样本，C的前缀为**非零初始hold**。

    16/5 是硬件路由，X使用CH0/core0、Y使用CH1/core8，不能任意选10/10。
    """)
    md("""
    ## 2. descriptor：A的公共场、B的IZ覆盖、C的AOD轴

    保留原版 `nuclear_rf` 的 `sharing="global"`。本例只登记q0，这不代表RF变为局域场。
    B 的clock/rydberg声明覆盖iz0，由当时位置推导targets；C的轴不是qubit drive，
    不需要伪造 `q0.aod`、也不关联spin或clock frame。

    A/B/C属于**同一个同步触发组**。每个shot的门序列开始前，ARTIQ在同一RTIO时刻
    向三条TTL各发一次触发；之后A/B按AWG步表、C按DDS内部timer自主执行。
    模式不同不妨碍共享时间轴，但触发到输出的延迟必须分别校准，并使用共同参考时钟。
    C首次建立hold使用的本机初始化触发，不是每shot额外发一次外部TTL。
    """)
    code(
        original('"""Signal roles and physical wiring,')
        + """

descriptor["name"] = "clock_ramsey_transport_teaching_lab"
descriptor["instruments"]["C"] = {"kind": "spectrum", "device": "C"}
descriptor["channels"]["clock"]["coverage"] = {"zones": ["iz0"]}
descriptor["channels"]["rydberg"]["coverage"] = {"zones": ["iz0"]}
descriptor["channels"].update({
    "aod_x": {"instrument": "C", "channel": 0},
    "aod_y": {"instrument": "C", "channel": 1},
    "dds_trigger_c": {"instrument": "core", "device": "dds_trigger_c"},
})
descriptor["sync_groups"]["qubit_awg"].update({
    "cards": ["A", "B", "C"],
    "triggers": {"A": "awg_trigger_a", "B": "awg_trigger_b", "C": "dds_trigger_c"},
    "timing": {"trigger_lead_mu": 1200},
})
# qubit_awg沿用原同步组名字；实际模式由每张卡的mode决定。
setup = DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
display(setup.connections())
display(setup.trigger_connections())
"""
    )
    md("""
    ## 3. LabConfig、q0 与搬运标定

    `LabConfig` 组合上面的 setup 与下面的 qubit/transport，作为编译和连接入口。先写出 q0 与标定，再创建 lab，保持每项参数可直接检查。
    q0的RF/clock参数沿用原版，只给它补上初始位置。IZ仍是site pair：本例只使用iz0[0]，
    iz0[1]是预留空位，不为了示例额外登记第二个原子。`SiteRef`保存站点身份及坐标；
    `to=q0.position`明确表示回到初始位置。
    """)
    source = original("q0 = Qubit(")
    if "position=None" not in source:
        raise ValueError("Base q0 position changed; review the transport derivation")
    code(
        source.replace(
            "position=None",
            'position=Position(0 * um, 0 * um, 0 * um, "tweezer_plane")',
        )
    )
    code("""
    iz0 = IZ("iz0", sites=(
        Position(30 * um, 10 * um, 0 * um, "tweezer_plane"),
        Position(36 * um, 10 * um, 0 * um, "tweezer_plane"),
    ))
    transport = TransportConfig(
        card="C",
        x_calibration=((0, 70e6), (40e-6, 90e6)),
        y_calibration=((0, 70e6), (15e-6, 85e6)),
        bindings={"q0": {"x_core": 0, "y_core": 8}},
        initial_amplitudes={0: 0.08, 8: 0.10},
        zones=(iz0,),
        position_tolerance=1e-9,
        frequency_tolerance_hz=1.0,
        min_separation=1e-6,
    )
    lab = LabConfig(setup=setup, qubits=[q0], transport=transport, emulation=True)
    initial_position = q0.position
    display([
        {"状态": "home", "x_um": 0, "y_um": 0, "X_MHz": 70, "Y_MHz": 70},
        {"状态": "iz0[0]", "x_um": 30, "y_um": 10, "X_MHz": 85, "Y_MHz": 80},
    ])
    """)
    md("""
    本例标定是线性的：`fX=70 MHz + x×0.5 MHz/μm`，`fY=70 MHz + y×1 MHz/μm`。
    因而外搬时X从70到85 MHz、Y从70到80 MHz；回搬反向。实际指令会量化，后面直接查看编译值。
    X/Y幅度分别保持0.08/0.10，不启用位置相关幅度补偿。它们是归一化tone幅度，不是已校准阱深。

    `LabConfig.transport`会保存几何、标定和core绑定快照。若换成两原子共享Y tone，
    必须像原三卡示例那样批量move全部受影响原子；不能遗漏spectator。
    原三卡示例的q0/q1分别用X core0/1、共用Y core8；两次批量move按列表顺序匹配终点。
    """)
    md("""
    ## 4. 保留原版的原生准备与读出

    以下登记原版的同一ARTIQ类与调用边界，
    不在notebook里执行kernel。`prepare_MOT()`的前段时长不作为固定墙钟时间；
    固定尾段15 ms+16 ns和读出100.48 ms保留在完整pulse sheet里。
    C在第一次运行建立hold后，应贯穿后续准备、读出和host等待持续输出。
    这里保留 `NativeKernelPackage.from_package`：fixture初始化包含 `core.reset()`，
    且 `finalize_method` 每shot执行。新建独立组件推荐 `NativeModule.from_package` +
    `NativeProgram`，由会话统一reset，组件 `finalize` 只在会话结束时执行，不能直接替换。
    详见 [原生组件指南](../native_components.md) 和 [实验室扩展示例](../../examples/lab_extension/README.md)。
    相机TTL不自动生成图像；扩展真实相机见 [读出adapter指南](../readout_adapters.md)。
    """)
    code(original("package = NativeKernelPackage.from_package("))
    md("""
    ## 5. 先命名 pulse 和搬运曲线，再写实验

    仍读取q0的pi2时长/幅度及clock的pi时长/幅度。不重复创建两份RF pulse。
    `MoveProfile.minimum_jerk(segments=32)`指定请求轨迹，duration在每次move中必填。
    内部采样点近似归一化曲线 `s(u)=10u³−15u⁴+6u⁵`，硬件执行有限段线性频率斜坡。
    100 μs、10.24 μs和200 μs都对齐6.4 ns；内部节点量化误差另行报告。
    """)
    code(
        original("# 修改Qubit的幅度或时长后，重新执行本单元格，再构建实验。")
        + """

transport_profile = MoveProfile.minimum_jerk(segments=32)
t_move = 100 * us
t_settle = 10.24 * us
"""
    )
    md("""
    ## 6. 完整clock_ramsey正文：外搬 → clock → 回搬

    顺序为 **RF π/2 → move到IZ → settle → clock 2π → move回home → settle → RF π/2**。
    两次move只传终点，不需要写from；整个块串行。`settle`包含在move的总占时内。
    继续只扫描第二RF附加相位，prepare/readout/shots是明确函数参数，函数最后返回Experiment。
    """)
    code("""
    def clock_ramsey_transport(q0, detuning, clock_pulse, pi2_pulse, phase, *,
                              interaction_site, move_duration, move_profile, settle,
                              prepare, readout, shots=2, preparation_to_gate=10 * us,
                              group="qubit_awg"):
        frequency = q0.parameters.rf.frequency * Hz + detuning
        exp = Experiment("clock_ramsey_transport", [q0], metadata={
            "hardware_verified": False,
            "transport_positions": "planned_not_camera_observed",
            "clock_2pi_assumption": "constant resonant Rabi rate; demo only",
        })
        with exp.average(shots):
            with exp.sweep(phase):
                with exp.realtime():
                    prepare()
                    delay(preparation_to_gate)
                    with exp.triggered_sequence(group=group):
                        reset_phase(q0.phase_frame("spin"))
                        reset_phase(q0.phase_frame("clock_control"))
                        play(q0.rf, pi2_pulse, frequency=frequency)
                        move(q0, to=interaction_site, duration=move_duration,
                             profile=move_profile, settle=settle)
                        play(q0.clock, clock_pulse)
                        move(q0, to=q0.position, duration=move_duration,
                             profile=move_profile, settle=settle)
                        play(q0.rf, pi2_pulse, frequency=frequency, phase=phase)
                    readout()
        return exp
    """)
    md("""
    与原版相比，两RF之间的间隔从200 μs增为420.48 μs，第二RF从300 μs推迟到520.48 μs。
    RF仍为30 kHz+1 kHz=31 kHz，并沿实际时间演化；不能为了让图像和旧版相同而重置载波或补一个任意相位。
    0°扫描点的第二RF起始载波相位因此为 `(31 kHz × 520.48 μs × 360°) mod 360° = 48.5568°`，
    其余点再加请求相位。clock独立的控制frame不自动推断光学转移对核自旋的物理相位影响。
    这些是输出相位与时序检查，不是包含搬运退相干的量子态仿真。
    """)
    code("""
    second_phases = SweepParameter("second_phase", [0, 45, 90, 135, 180], "deg")
    clock_exp = clock_ramsey_transport(
        q0=q0, detuning=1 * kHz, clock_pulse=clock_pulse, pi2_pulse=pi2_pulse,
        phase=second_phases, interaction_site=iz0[0],
        move_duration=t_move, move_profile=transport_profile, settle=t_settle,
        prepare=prepare_MOT, readout=readout, shots=2,
    )
    clock_compiled = lab.compile(clock_exp)
    assert q0.position == initial_position
    print("扫描点:", len(clock_compiled.points), "shots/point:", clock_compiled.shots)
    display(clock_compiled.axes)

    """)
    md("""
    编译后用 `clock_compiled.export_report(...)` 查看完整时序与波形。下面几行只把当前 notebook 源码附到报告，便于后来核对“这个报告来自哪个实验”；不参与实验定义，也不是实机执行记录。编写新实验时通常只需直接调用 `export_report`，无需重复写这段报告存档代码。
    """)
    code("""
    # 报告附带可审阅的原始代码；不修改已编译时序。

    saved_notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    source_snapshot = OUTPUT / "notebook_code_snapshot.py"
    source_snapshot.write_text("\\n\\n".join(
        "# Notebook cell " + str(i) + "\\n" + "".join(c["source"])
        for i, c in enumerate(saved_notebook["cells"]) if c["cell_type"] == "code"
    ), encoding="utf-8")
    clock_report = clock_compiled.export_report(OUTPUT / "report", source_path=source_snapshot)
    display(Markdown(f"[完整编译报告]({clock_report['index'].resolve().as_uri()})"))
    """)
    md("""
    ## 7. 两次move怎样知道起点：查看实际编译结果

    builder只记录请求。每个扫描点的编译先建立home位置和初始tone状态，然后沿实际执行路径推进：

    1. 第一次move读取内部位置home；排好搬运和settle后，把内部q0位置更新为iz0[0]。
    2. `play(q0.clock, ...)`此时看到q0位于IZ，所以推导targets=[q0]，B只发一次公共光场。
    3. 第二次move的请求终点是原始 `q0.position`；起点从内部状态读取为iz0[0]。
    4. 内部位置回到home，shot闭合检查通过。下个扫描点/shot仍以home为初态。

    外部q0.position始终不变。以下表格来自logical events，不是手写一份位置账本。
    """)
    code("""
    point = clock_compiled.points[0]
    region = next(e for e in point.logical_events if e.kind == "spectrum_region")
    tick = clock_compiled.setup.tick_seconds
    moves = [e for e in point.logical_events if e.kind == "move"]
    display([{
        "move": i + 1,
        "start_us": (e.start_mu - region.start_mu) * tick * 1e6,
        "motion_end_us": (e.start_mu - region.start_mu + e.params["motion_duration_mu"]) * tick * 1e6,
        "settle_end_us": (e.end_mu - region.start_mu) * tick * 1e6,
        "from_um": [e.params["initial_layout"]["q0"][a] * 1e6 for a in ("x", "y")],
        "to_um": [e.params["final_layout"]["q0"][a] * 1e6 for a in ("x", "y")],
        "max_knot_time_error_ns": e.params["max_timing_quantization_error_s"] * 1e9,
    } for i, e in enumerate(moves)])
    assert moves[0].params["final_layout"] == moves[1].params["initial_layout"]
    assert moves[1].params["final_layout"] == moves[0].params["initial_layout"]
    assert q0.position == initial_position
    clock_event = next(e for e in point.logical_events if e.kind == "play" and e.signal == q0.clock)
    assert clock_event.params["operation_qubits"] == ["q0"]
    print("clock targets:", clock_event.params["operation_qubits"])
    """)
    md("""
    ## 8. 从move变成C卡DDS命令

    编译器先把轨迹节点映射成X/Y频率，再量化到DDS频率字和6.4 ns时间格点。
    每段使用实际量化时长计算 `frequency_slope_hz_per_s`，通过内部timer推进；
    中间节点只更新slope，避免反复固定频率引入一次更新延迟。每次搬运终点显式固定频率，
    将频率和幅度slope归零，保留非零幅度。终点相邻的下一次move还会记录其一次更新延迟补偿。
    本例有settle和clock间隔，直接看零slope的hold帧即可。

    C不经过AWG切段、填零、零尾段，也不建立软件绝对相位账本。
    以下是实际DDS程序选出的关键帧，完整JSON和CSV在报告中。时间全部换算到门序列公共入口。
    """)
    code("""
    physical = next(e for e in point.physical_events if e.kind == "spectrum_program")
    cards = physical.params["cards"]
    dds = cards["C"]["program"]
    prefix_mu = dds["prefix_mu"]
    interesting = {0, len(dds["frames"]) - 1}
    interesting.update(i for i, f in enumerate(dds["frames"]) if f["set_frequency"])
    interesting.update(i for i, f in enumerate(dds["frames"]) if
        i and any(t["frequency_slope_hz_per_s"] != 0 for t in f["tones"])
        and all(t["frequency_slope_hz_per_s"] == 0 for t in dds["frames"][i-1]["tones"]))
    display([{
        "frame": i, "time_us": (f["at_mu"] - prefix_mu) * tick * 1e6,
        "next_trigger": f["trigger_source"], "pin_frequency": f["set_frequency"],
        "tones": [{"core": t["core"], "MHz": t["frequency_hz"] / 1e6,
                   "MHz_per_us": t["frequency_slope_hz_per_s"] / 1e12,
                   "amplitude": t["amplitude"]} for t in f["tones"]],
    } for i, f in enumerate(dds["frames"]) if i in interesting])
    print("shot命令数:", dds["command_count"], "含初始hold/配置预留:", dds["resident_command_count"], "/ 4096")
    print("频率量化/斜率误差:")
    display(dds["quantization"])
    assert dds["initial_tones"] == dds["final_tones"]
    assert dds["frames"][-1]["trigger_source"] == "card"
    assert all(t["amplitude"] > 0 and t["frequency_slope_hz_per_s"] == 0
               and t["amplitude_slope_per_s"] == 0 for t in dds["frames"][-1]["tones"])
    """)
    md("""
    ## 9. 通用waveform viewer：A/B波形与C频率/幅度共用横轴

    `export_report` 自动生成的 **waveform.html** 已同时支持 AWG 和原生 DDS，
    所有包含move的实验都使用同一个入口，无需导入本例的绘图helper。
    也可以单独调用 `clock_compiled.waveform_simulator(path)` 导出相同查看器。

    图中三行分别为 A/B 的最终DAC、C 的频率MHz、C 的幅度FS；它们共用同一横轴和缩放。
    A/B长窗口显示实际int16样本的min/max，放大后切换逐样本；C直接显示量化后DDS命令曲线，
    包括pin更新延迟、分段斜率和非零hold，不模拟高频载波或绝对tone相位。

    默认按实际执行顺序显示5个point×2个shot的Spectrum窗口，点/shot边界省略未知host间隔。
    Advanced可选择单次执行或完整已知control窗口；C在已知准备/读出区间仍显示hold。
    下方第二个链接直接定位point0的门序列；此时横轴相对control T0，门入口位置会单独列出。
    相对门入口，外触发仍为−1.2 μs，A/B样本0在−0.8/−0.784 μs，C首帧在−0.64 μs。
    计划位置见第7节和完整报告，原生准备/读出时序见pulse sheet。
    """)
    code("""
    waveform_url = clock_report["waveform"].resolve().as_uri()
    gate_start_s = region.start_mu * tick
    gate_end_s = region.end_mu * tick
    display(Markdown(f"[通用waveform viewer：全部point/shot的A/B/C]({waveform_url})"))
    detail_url = (
        f"{waveform_url}?point=0&shot=0"
        f"&t0={gate_start_s-2e-6:.12f}&t1={gate_end_s+2e-6:.12f}"
    )
    display(Markdown(f"[只检查point0的完整门序列]({detail_url})"))
    print("本例门入口相对control T0:", gate_start_s * 1e6, "μs")
    display(Markdown(f"[完整pulse sheet]({clock_report['pulse_sheet'].resolve().as_uri()})"))
    """)
    md("""
    本组默认参数应满足下表；下面再用编译事件验证这些数值：

    | 相对门入口时间 / μs | A | B | C / 位置 |
    |---|---|---|---|
    | 0–100 | RF π/2 | 0 | home hold，70/70 MHz |
    | 100–200 | 0 | 0 | 外搬，X70→85、Y70→80 MHz |
    | 200–210.24 | 0 | 0 | IZ settle，85/80 MHz |
    | 210.24–410.24 | 0 | clock 2π | IZ hold，85/80 MHz |
    | 410.24–510.24 | 0 | 0 | 回搬，X85→70、Y80→70 MHz |
    | 510.24–520.48 | 0 | 0 | home settle |
    | 520.48–620.48 | RF π/2（扫描phase） | 0 | home hold |
    | 之后、读出、shot间 | AWG有限播放结束 | AWG有限播放结束 | 继续home hold直到会话关闭 |

    AWG实际零尾段可略晚于620.48 μs，图中按编译值保留，不改变任何门边界。
    """)
    code("""
    expected_starts = [0, 100, 210.24, 410.24, 520.48]
    for point in clock_compiled.points:
        region = next(e for e in point.logical_events if e.kind == "spectrum_region")
        operations = [e for e in point.logical_events if e.kind in {"play", "move"}]
        np.testing.assert_allclose([(e.start_mu-region.start_mu)*tick*1e6 for e in operations], expected_starts, rtol=0, atol=1e-9)
        np.testing.assert_allclose(region.duration_mu*tick*1e6, 620.48, rtol=0, atol=1e-9)
        for e in operations:
            if e.kind == "play" and e.signal == q0.rf:
                assert e.params["frequency"] == 31_000
        program = next(e for e in point.physical_events if e.kind == "spectrum_program").params["cards"]["C"]["program"]
        assert program["initial_tones"] == program["final_tones"]
    assert q0.position == initial_position
    print("全部5个相位点：时序、31kHz RF、往返闭合和非修改q0.position检查通过。")
    """)
    md("""
    ## 10. 搬运之后和shot之间为什么不会归零

    `duration`结束只结束斜坡，不结束DDS输出。到IZ后，C锁定85/80 MHz、保持0.08/0.10幅度；
    在settle与clock期间不关断。回到home后，C锁定70/70 MHz；shot终帧将trigger source切回CARD，
    但幅度仍非零。这里的CARD是**后续命令帧等待的触发源**，不等于输出开关。

    初次运行：configure/reset仅用于建立会话初态 → 初始hold并启动C一次 → 全卡ready → ARTIQ外触发。
    后续shot：C边保持边预装SINGLE命令 → 外触发 → 内部timer执行 → 终帧hold。
    A/B换bank、相机读出、host等待和 `session.run()` 返回都不stop C；关闭连接会话才stop。
    正常完成看本轮DDS trigger-count增量和错误状态，不能用“FIFO已经空了”判断终帧已执行。

    这份notebook没有调用上述运行器方法。mock验证证明软件调用顺序，实机连续输出仍需实验室验收。
    会话发生异常时停止卡、标记位置未知，不在原会话自动重试搬运。
    `with lab.connect(...)`一旦退出with就关闭会话，因此要在多次run之间保持同一个连接上下文。

    原教程的RF相位账本仍用于A；C不维护绝对tone相位，不与qubit frame或A/B相干契约绑定，
    搬运途中不发送reset/phase-jump。shot间hold持续时间未知，所以图不拼出假定固定的两shot墙钟间隔。
    """)
    md("""
    ## 11. 连接真实硬件：保持一个明确的会话

    先在实验室包中准备真实 device_db/spec_db、descriptor、同步校准、AOD 标定、Qubit 与 native 回调；这里的 SIM 身份、坐标和频率示例不能直接用于仪器。
    下面只展示实机调用顺序，是 Markdown 示例，不会随 Run All 执行。命名 pulse 应按 `calibrated_q0` 重建，实验函数仍沿用上面的完整正文。

    ```python
    calibrated_pi2_pulse = Square(
        duration=calibrated_q0.parameters.rf.pi2_duration,
        amplitude=calibrated_q0.parameters.rf.pi2_amplitude,
    )
    calibrated_clock_pulse = Square(
        duration=2 * calibrated_q0.parameters.clock.pi_duration,
        amplitude=calibrated_q0.parameters.clock.pi_amplitude,
    )
    real_lab = LabConfig(
        setup=real_setup, qubits=[calibrated_q0], transport=real_transport,
        emulation=False, artiq_python=artiq_python,
    )
    real_exp = clock_ramsey_transport(
        q0=calibrated_q0, detuning=1 * kHz,
        clock_pulse=calibrated_clock_pulse, pi2_pulse=calibrated_pi2_pulse,
        phase=second_phases, interaction_site=real_iz[0],
        move_duration=t_move, move_profile=transport_profile, settle=t_settle,
        prepare=real_prepare, readout=real_readout, shots=100,
    )
    real_compiled = real_lab.compile(real_exp)
    with real_lab.connect(clock_session_ids=measured_clock_session_ids) as session:
        receipt = session.run(real_compiled)
        # 后续 run 放在同一 with 内，C 卡才持续保持陷阱输出。
    ```

    `connect` 核对设备与时钟，`run` 才上传并执行；`artiq_python` 使用独立 ARTIQ 环境。实测时钟 session 标识必须对应当前连续参考时钟。关闭 `with` 会停止 DDS；异常后位置未知，不在原会话自动重做搬运。当前返回值是执行记录，拍照与数据提交需另接[读出 adapter](../readout_adapters.md)。
    """)
    md("""
    ## 12. 修改参数与验证边界

    改q0的pulse幅度/时长：重新定义命名pulse → 调用实验函数 → 编译/报告/统一视图。
    改RF频率/detuning：重新构建实验和编译；改站点或标定：重建transport/LabConfig后再构建实验。
    改曲线/搬运时长：重建profile或调用参数，再编译。新时序需对齐DDS 6.4 ns且满足完整帧命令预算；
    最后默认参数断言表也要随你的有意修改更新。

    验证层级分别是：notebook全新Python kernel执行、实际编译事件/命令与A/B packed DAC检查、
    查看器浏览器实显、真实ARTIQ离线编译、Spectrum实机输出与原子存活率。是否通过以对应本次源码的验证记录为准；Run All 不自动完成后四项。
    本例不自动寻路，不做成像反馈，也不支持将含move结构放进parallel/ASAP。

    参考：[IZ/move用法](../transport_dds.md)、[原三卡往返源码](../../examples/artiq_spectrum/transport_dds.py)、
    [Spectrum DDS官方说明](https://spectrum-instrumentation.com/download_files/application_notes/PN_Using_DDS_Mode_on_AWG.php)、
    [官方复杂ramp示例](https://github.com/SpectrumInstrumentation/spcm/blob/ddd29f3365a874f870520935ce90a66237b50f00/src/examples/03_dds/12_dds_complex_ramps.py)。
    """)
    for index, cell in enumerate(cells):
        cell.id = f"ramsey-transport-{index:03d}"
    notebook = nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3 (project)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12.15"},
            "taiyidsl": {
                "base_notebook": BASE.name,
                "base_sha256": hashlib.sha256(BASE.read_bytes()).hexdigest(),
                "generator": "scripts/build_clock_transport_tutorial.py",
                "hardware_executed": False,
            },
        },
    )
    nbformat.validate(notebook)
    nbformat.write(notebook, DESTINATION)
    print(
        json.dumps(
            {
                "notebook": str(DESTINATION),
                "cells": len(cells),
                "code_cells": sum(c.cell_type == "code" for c in cells),
            },
            ensure_ascii=False,
        )
    )
    return DESTINATION


if __name__ == "__main__":
    build()
