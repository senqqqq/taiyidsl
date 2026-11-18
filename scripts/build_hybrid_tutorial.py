"""Build the Chinese, offline-first notebook from readable lesson cells.

Run with the optional notebooks dependency group. This script only writes the
source notebook; execute_hybrid_tutorial.py validates it in a fresh kernel.
"""

# Lesson prose and Markdown tables deliberately keep paragraphs on one line.
# ruff: noqa: E501

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "docs/tutorials/hybrid_from_scratch.ipynb"
cells = []


def md(source):
    cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))


def code(source):
    cells.append(nbformat.v4.new_code_cell(textwrap.dedent(source).strip()))


md(r"""
# 从零编写混合量子实验：从 GreenMOT Ramsey 到双比特 RB

这是一份可以按顺序 **Restart Kernel → Run All** 的实验编写教程。我们从一个空的实验对象开始，自己定义 qubit、接线、门、可复用实验段和四个原生 ARTIQ 组件，再组合 Ramsey 与完整双比特 Clifford RB／CZ interleaved RB，最后编译、分块、查看 DAC 样本、保存与分析数据。

**本 notebook 默认全程离线。** 所有数值、串号、TTL 与 CZ 门体都是教学配置；生成的合成衰减数据不是原子实验，也不是演示波形的物理仿真。Run All 不连接仪器，不执行 ARTIQ kernel。末尾说明如何把每一部分替换为实验室配置。

学习路线：

1. 环境与文件 → 2. Qubit 参数与版本 → 3. 硬件映射 → 4. pulse、operation 与 Section → 5. layer、barrier 与相位 → 6. 原生组件 → 7. Ramsey 与现有 GreenMOT → 8. 双比特 RB → 9. 自动分块与磁盘产物 → 10. 联合读出、分析与恢复 → 11. 实验室接入与后续改进。

本教程不要求事先理解编译器源码。第一次完整运行会生成 Clifford 表、编译若干小实验和报告，耗时通常比后续单独重跑一节长。全部产物保存在项目 `output/tutorial_from_scratch/runs/` 下，每次从头运行创建独立目录。
""")

md(r"""
## 1. 准备环境，认识四种不同的“运行”

在项目根目录用 PowerShell 执行一次：

```powershell
cd C:/AIproject/taiyidsl
uv sync --locked --group notebooks
uv run --locked --group notebooks jupyter lab docs/tutorials/hybrid_from_scratch.ipynb
```

项目使用 uv 管理的 CPython 3.12；在 VS Code 中也可以打开本文件并选择本项目 `.venv`。`notebooks` 是可选依赖组，不增加仪器运行环境的依赖。不需要在 notebook 中 `pip install`，也不需要在普通 Python 环境安装 ARTIQ。

| 动作 | 实际发生什么 |
|---|---|
| 执行 `with ...`、`ops.x90(q)` | Python 构建节点树；此刻不输出脉冲 |
| `lab.compile(exp)` / `compile_stream(...)` | 解析参数、调度、计算相位，生成离线产物 |
| 真实 ARTIQ 离线编译 | 用 ARTIQ 编译器生成 ELF；仍不执行 kernel |
| 实机执行 | 完成预检、上传、ready，运行一个 shot 的顶层 kernel；需要另外配置仪器与探测器 |

`exp.realtime()` 是 DSL 的结构上下文，不是给任意 Python 加上 `@kernel`。Python 的 `for` 会在构建期展开节点；每 shot 真正执行的是后端生成的程序。

环境命令参考：[uv 依赖组](https://docs.astral.sh/uv/concepts/dependencies/)、[uv sync](https://docs.astral.sh/uv/concepts/projects/sync/)。
""")
code(r"""
from pathlib import Path
from datetime import datetime
from uuid import uuid4
import sys
import os
import json
import math
import html
import numpy as np
from IPython.display import display, Markdown, HTML, Image

ROOT = next(
    (p for p in (Path.cwd().resolve(), *Path.cwd().resolve().parents)
     if (p / "src/taiyidsl").is_dir() and (p / "pyproject.toml").is_file()),
    None,
)
assert ROOT is not None, "请从项目根目录或 docs/tutorials 启动 notebook"
assert sys.version_info[:2] == (3, 12), "请选择项目的 uv CPython 3.12 环境"
os.chdir(ROOT)
SESSION = ROOT / "output/tutorial_from_scratch/runs" / (
    datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
)
SESSION.mkdir(parents=True)

def table(rows):
    if not rows:
        return display(Markdown("（空表）"))
    keys = list(rows[0])
    head = "".join(f"<th>{html.escape(str(k))}</th>" for k in keys)
    body = "".join("<tr>" + "".join(
        f"<td>{html.escape(str(row.get(k, '')))}</td>" for k in keys
    ) + "</tr>" for row in rows)
    display(HTML(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"))

print("Python:", sys.executable)
print("本次输出:", SESSION)
""")
code(r"""
from taiyidsl import (
    Qubit, QubitParameters, DriveParameters, TransitionParameters, Position,
    QubitStore, CalibrationProposal, PairCalibration, QubitOperations,
    LabConfig, Device, DeviceSetup, SignalMap,
    SpectrumCard, SpectrumOutput, SpectrumGroup, SpectrumConfig,
    Experiment, SweepParameter, Square, Section, section, add, layer, barrier,
    qubit_operation, play, delay, rz, shift_phase, reset_frame, reset_phase,
    parallel, acquire, us, ns, MHz, um,
    generate_rb_sequences, clifford_catalog, sequence_tableau, rb_sequence_section,
    ResultSet, joint_rb_counts, analyze_rb_counts,
)
""")

md(r"""
## 2. Qubit：保存的是参数快照，不是硬件驱动

一个 `Qubit` 包含身份和不可变的 `QubitParameters`。`q.rf` / `q.signal("clock")` 是逻辑信号；连接哪个输出由下一节的 LabConfig 决定。

我们先建两个 qubit。RF 与 clock 的数字波形频率独立保存；原子跃迁共振频率属于 `TransitionParameters.resonance_frequency`，不能把光学共振频率直接填进 AWG 的 `frequency`。下面未填写真实共振频率。

`pi_duration/pi2_duration` 就是实验中常说的 π/π2 pulse length。两种旋转分别有 `pi_amplitude/pi2_amplitude`；未指定才回退 `amplitude`，库不会替你假定“半时长”或“半幅度”。所有值都需要对应你的脉冲形状与校准。

位置以 SI 米保存并带坐标系名，仅是元数据，不自动控制 AOD、搬运或寻址。自定义参数放进 `extensions` 的命名空间。
""")
code(r"""
def teaching_qubit(index):
    return Qubit(
        f"q{index}",
        QubitParameters(
            rf=DriveParameters(
                frequency=(10 + 2 * index) * MHz,
                amplitude=0.30,
                pi_amplitude=0.30, pi2_amplitude=0.17,
                pi_duration=800 * ns, pi2_duration=400 * ns,
                waveform="square",
            ),
            drives={"clock": DriveParameters(
                frequency=(80 + 10 * index) * MHz,
                amplitude=0.12,
                pi_duration=800 * ns, pi2_duration=400 * ns,
            )},
            transitions={
                "spin": TransitionParameters("g_down", "g_up"),
                "optical": TransitionParameters("g_up", "e_up"),
            },
            drive_transitions={"rf": "spin", "clock": "optical"},
            drive_frames={"clock": "clock_control"},
            position=Position(index * 5 * um, 0 * um, 0 * um, "tweezer_plane"),
            extensions={"teaching": {"calibrated": False, "purpose": "offline"}},
        ),
    )

q0, q1 = teaching_qubit(0), teaching_qubit(1)
qubits = (q0, q1)
table([{
    "qubit": q.uid, "RF Hz": q.drive("rf").frequency,
    "π/2 s": q.drive("rf").pi2_duration,
    "π/2 amplitude": q.drive("rf").rotation_amplitude("pi2"),
    "RF signal": q.rf, "clock signal": q.signal("clock"),
    "spin frame": q.frame, "clock frame": q.phase_frame("clock_control"),
} for q in qubits])
""")
md(r"""
### 参数更新必须产生新版本

`QubitStore` 负责保存和更新参数。已经拿到的 `q0` 和已经构建的 Section 不会随着文件变化而偷偷改变。应用新校准之后，应重新取出 qubit、重新构建门和实验；双比特校准也要检查依赖版本。

下面的 proposal **只是模拟一次校准更新**，并不声称测得了 0.18 的正确幅度。第一次 apply 成功，第二次因为基准 revision 已过期而拒绝。后面的教程继续使用开头的旧快照，以便把这个区别看清楚。
""")
code(r"""
store = QubitStore(qubits, path=SESSION / "qubits.json")
store.save()
proposal = CalibrationProposal(
    qubit_id=q0.uid, base_revision=q0.revision, base_fingerprint=q0.fingerprint,
    updates={"rf.pi2_amplitude": 0.18}, units={"rf.pi2_amplitude": "1"},
    quality={"example_only": 1.0}, source_experiment="synthetic_tutorial_update",
)
updated = store.apply(proposal)
assert updated.revision == q0.revision + 1
assert q0.drive("rf").pi2_amplitude == 0.17
assert QubitStore.load(SESSION / "qubits.json")["q0"].fingerprint == updated.fingerprint
try:
    store.apply(proposal)
except ValueError as error:
    print("预期的版本保护:", error)
else:
    raise AssertionError("旧 proposal 不应被再次应用")
print("本教程实验使用 q0 revision", q0.revision,
      "；store 当前 revision", updated.revision)
""")

md(r"""
## 3. 从零声明双卡双 TTL：两种映射不要混淆

第一种是 **TTL → 卡的 EXT0**：`trigger_devices={"A": "awg_trigger_a", ...}`。一个同步组启动时，ARTIQ 给这些 TTL 安排相同 RTIO 起止时刻。每张卡播放整段门序列，不是每个 `play()` 发一次 TTL。

第二种是 **qubit 逻辑信号 → 物理输出**：`q0.rf → q0_rf → A/channel 0`。一个 TTL 不对应一个 RF 门或某个单独 channel。

下面手动建立 LabConfig，没有调用预设实验 builder。`SIM_*` 串号、触发延迟与 calibration 标记都是离线占位。当前后端合同限定 M4i.6631-x8、1.25 GS/s、125 MHz 参考，不代表任意 Spectrum 型号或采样率都可用。输出幅度是归一化量，实际电压还取决于 output range、终端和模拟链路。
""")
code(r"""
group = SpectrumGroup(
    cards={"A": SpectrumCard("SIM_TUTORIAL_A"),
           "B": SpectrumCard("SIM_TUTORIAL_B")},
    outputs={
        "q0_rf": SpectrumOutput("A", 0), "q0_clock": SpectrumOutput("A", 1),
        "q1_rf": SpectrumOutput("B", 0), "q1_clock": SpectrumOutput("B", 1),
    },
    trigger_devices={"A": "awg_trigger_a", "B": "awg_trigger_b"},
    calibration={"status": "unmeasured", "revision": "tutorial-only"},
)
setup = DeviceSetup(
    {
        **{name: Device(name, "spectrum") for name in group.outputs},
        **{name: Device(name, "ttl", grid_mu=1)
           for name in ("awg_trigger_a", "awg_trigger_b", "camera_ttl")},
    },
    spectrum=SpectrumConfig({"qubit_awg": group}),
)
signals = SignalMap({
    "q0.rf": "q0_rf", "q0.clock": "q0_clock",
    "q1.rf": "q1_rf", "q1.clock": "q1_clock",
    "camera.trigger": "camera_ttl",
})
lab = LabConfig(setup, signals, backend="artiq_spectrum")
profile_path = SESSION / "lab_teaching.json"
profile_path.write_text(json.dumps(lab.to_dict(), indent=2), encoding="utf-8")
assert LabConfig.load(profile_path).to_dict() == lab.to_dict()
table([{"card": card, "EXT0 source": ttl,
        "serial": group.cards[card].serial_number}
       for card, ttl in group.card_trigger_devices.items()])
table([{"logical": logical, "device": device,
        "output": f"{group.outputs[device].card}/{group.outputs[device].channel}"
        if device in group.outputs else "ARTIQ TTL"}
       for logical, device in signals.items()])
""")
md(r"""
扩到三／四张卡时，向 `cards`、`outputs`、`trigger_devices` 增加对应项，并在 DeviceSetup 与 SignalMap 声明输出和 TTL。不是修改一个“最大两卡”的开关。所有卡必须预检并 ready 后才能触发，提交容量仍受 RTIO 硬件约束。相同 RTIO 时间戳不等于已经测得模拟输出同步。

实验室需要修改：卡串号、输出通道、输出量程、device_db 里的 TTL 设备与 RTIO 通道、共用参考时钟、触发线缆／延迟、固件与测量所得 calibration。`LabConfig` **不替代 ARTIQ device_db**。不要只把 `status` 改成 verified 来绕过测量。
""")

md(r"""
## 4. pulse → qubit_operation → Section → Experiment

当前结构可以这样理解：

```text
Experiment                         扫描轴、平均、qubit 快照、元数据
└─ average / sweep                 主机执行计划
   └─ realtime                     一个 shot 的实时控制区
      ├─ 原生准备调用               单独声明的 NativeProgram 节点
      ├─ delay
      ├─ triggered_sequence        Spectrum 整段启动与载波参考入口
      │  ├─ play / delay / rz      基本 DSL 节点
      │  ├─ Section               可复用 Node 子树，可嵌套和混排
      │  │  └─ layer              有明确起止边界的并行 Section
      │  └─ ...
      ├─ acquire                  声明测量句柄与触发窗口
      └─ 原生读出调用
```

普通 `with serial()/parallel()/case()` 收集的是 Node 子树；并非每一种 with 都是可独立复用的 Section。`section()` 和 `layer()` 才返回可保存的 Section。内部 `_builder()` 只是找到当前收集器，不是公开的实验组件工厂。

`@qubit_operation` 执行函数体、收集节点，并由装饰器返回完成的 Section。函数体本身应返回 `None`。在活动上下文调用，结果会**自动插入一次**；在外部调用得到脱离实验的 Section，以后用 `add(saved)` 插入一个副本。不要在活动上下文写 `add(ops.x90(q))`，这样会插两次。
""")
code(r"""
drive = q0.drive("rf")
with section(name="pulse_level_x90") as pulse_level_x90:
    play(q0.rf, Square(
        drive.pi2_duration,
        amplitude=drive.rotation_amplitude("pi2"),
        frequency=drive.frequency,
        phase=0.0,
    ))
print("最基本的门体仍然是 play + pulse:", pulse_level_x90.to_node().children[0].kind)
""")
md(r"""
上面手动创建一个 pulse-level Section。接下来 `ops.x90(q0)` 自动完成同样的参数读取，并记录标准操作名称、参数及快照依赖。我们再把两次 X90、等待与 virtual Z 包成更高层的 Ramsey operation；这就是从 pulse level 逐层构建门库的方式。
""")
code(r"""
ops = QubitOperations(drive="rf")

@qubit_operation
def ramsey_gates(q, dark_time, virtual_angle=0.0, final_phase=0.0):
    ops.x90(q)
    delay(q.rf, dark_time)
    rz(q, virtual_angle)
    ops.x90(q, phase=final_phase)
    # 无 return：装饰器负责返回 Section。

saved_ramsey = ramsey_gates(q0, 2 * us, virtual_angle=math.pi / 2)
assert isinstance(saved_ramsey, Section) and saved_ramsey.finalized
tree_copy = saved_ramsey.to_node()
original_count = len(tree_copy.children)
tree_copy.children.clear()
assert len(saved_ramsey.to_node().children) == original_count
print("返回:", type(saved_ramsey).__name__,
      "；内部:", type(saved_ramsey.to_node()).__name__,
      "；绑定 qubits:", saved_ramsey.qubits)
print("记录的有效实参:", saved_ramsey.parameters)
""")
code(r"""
with section(name="two_ramsey_blocks") as reusable_pair:
    add(saved_ramsey)
    delay(q0.rf, 1 * us)
    add(saved_ramsey)

simple_exp = Experiment("first_authored_experiment", qubits)
with simple_exp.average(1), simple_exp.realtime():
    with simple_exp.triggered_sequence(group="qubit_awg"):
        reset_frame(q0.frame)
        add(reusable_pair)
        ops.y90(q1)  # 自动插入；普通指令和 Section 可以混排。
simple_compiled = lab.compile(simple_exp)
print("扫描点:", len(simple_compiled.points),
      "；逻辑事件:", len(simple_compiled.points[0].logical_events))
""")
md(r"""
Section 保存的是未调度的结构和参数依赖，不是已编译波形；插入时复制，完成后只读。它仍绑定原来的 qubit 与参数快照，不能把 `saved_ramsey` 任意重绑到 q1；应再次调用 `ramsey_gates(q1, ...)`。

标准 `QubitOperations` 提供 X/Y 的正负 π/2、π，virtual Z90/Z180，以及 `clock(q, rotation="pi"|"pi2", phase=...)`。时长、幅度、频率和波形从快照读取，也可以明确覆盖单次操作。它不会把任意角度自动换算成一个“已校准”的 pulse。可以用自定义 operation 表达更复杂的相干转移与补偿。
""")

md(r"""
## 5. 串行、layer、targeted barrier 与相位

默认仍是串行。即使 q0/q1 输出不同，顺序写的门也按顺序执行；需要同时开始时显式使用 `layer(q0, q1, align="start")`，需要同时结束时用 `align="end"`。一个分支可以是内含多条串行指令的 operation。不同分支不能争用同一 qubit 或冲突的物理输出。即使只做一个 shot，也要保留一个顶层 `average(1)`，这是当前 Experiment 的结构要求。

`section(schedule="asap")` 是显式启用按资源尽早排程。`barrier(q0)` 只同步 q0 及共享资源；`barrier()` 同步当前 Section 全部。**layer 是完整层边界**，即使父 Section 使用 ASAP，相邻层也不重叠。`exact_duration` 是长度断言，不是填充。

下面用最终调度后的逻辑时间对比，不靠源码顺序猜测。
""")
code(r"""
def scheduling_example(mode):
    exp = Experiment(f"schedule_{mode}", qubits)
    with exp.average(1), exp.realtime(), exp.triggered_sequence(group="qubit_awg"):
        if mode == "serial":
            ops.x180(q0)
            ops.y90(q1)
        else:
            with layer(q0, q1, align=mode):
                ops.x180(q0)
                ops.y90(q1)
    return lab.compile(exp)

def pulse_rows(compiled, label="", point=0):
    tick = compiled.setup.tick_seconds
    return [{"case": label, "signal": e.signal,
             "start ns": round(e.start_mu * tick * 1e9, 3),
             "end ns": round(e.end_mu * tick * 1e9, 3)}
            for e in compiled.points[point].logical_events if e.kind in ("play", "trigplay")]

schedules = {mode: scheduling_example(mode) for mode in ("serial", "start", "end")}
table([row for mode, compiled in schedules.items() for row in pulse_rows(compiled, mode)])
""")
code(r"""
aligned_exp = Experiment("asap_with_layer_boundaries", qubits)
with aligned_exp.average(1), aligned_exp.realtime(), aligned_exp.triggered_sequence(group="qubit_awg"):
    with section(schedule="asap"):
        ops.x180(q0)
        ops.y90(q1)
        barrier(q0)
        ops.x90(q0)
        with layer(q0, q1):
            ops.x90(q0)
            ops.x180(q1)
        with layer(q0, q1, align="end"):
            ops.y90(q0)
            ops.y180(q1)
aligned = lab.compile(aligned_exp)
table(pulse_rows(aligned, "asap + barrier + 2 layers"))
""")
md(r"""
### 相位：每个控制 frame 内只有一个持久偏移 F

当前表达式是

\[
\theta(t)=\Phi_{\mathrm{osc}}(t)+s[F(t)+\phi_{\mathrm{pulse}}(u)].
\]

普通直驱通常 `s=+1`；涉及频率变换或相位方向的配置按具体控制链定义，不能一概硬写 +1。

| 指令／边界 | 当前含义 |
|---|---|
| `shift_phase(frame, δ)` | F 加 δ |
| `rz(q, θ)` / `ops.z90(q)` | 门级 virtual Z；当前约定 F 减 θ |
| `reset_frame(frame)` | 清整个 F，包括 virtual Z 的贡献 |
| `reset_phase(frame)` | 清 F、继承的调制偏移，并重新建立载波参考 |
| `triggered_sequence` 默认入口 | AWG 预计算使用该序列入口作为载波时间参考；不会暗中清除先前显式 F |
| Section / layer / barrier / segment / chunk | 本身不重置相位 |

前面的 MOT、冷却和等待不会进入默认 AWG 首门载波原点。sequence 内的 delay 和频率变化仍影响后续载波相位；脉冲相位调制和配置的继承规则也要保留，不能把每个 segment 都从零相位独立绘制。`reset_phase` 是控制波形重新定相，不是把量子态制备到 |0⟩。

RF 默认与 Raman 关联核自旋 frame；我们显式给 clock 声明独立的 `clock_control`。这表示两条驱动的控制相位独立记账，并不自动推导多能级态矢量的相位传播。相干转移后需要的补偿应写进对应门定义。
""")
code(r"""
phase_exp = Experiment("explicit_phase_frames", qubits)
with phase_exp.average(1), phase_exp.realtime(), phase_exp.triggered_sequence(group="qubit_awg"):
    reset_phase(q0.frame)
    reset_frame(q0.phase_frame("clock_control"))
    ops.x90(q0)
    rz(q0, math.pi / 2)
    ops.clock(q0, rotation="pi2", phase=0.2)
    shift_phase(q0.phase_frame("clock_control"), 0.1)
    ops.clock(q0, rotation="pi", phase=0.0)
phase_compiled = lab.compile(phase_exp)
frames = phase_compiled.frame_phases()
assert math.isclose(frames[q0.frame], -math.pi / 2)
assert math.isclose(frames[q0.phase_frame("clock_control")], 0.1)
print("最终 F (rad):", frames)
""")

md(r"""
## 6. 从自己的 ARTIQ 类开始接入：四个组件、一个 shot kernel

现在自己写 MOT、cooling、state preparation、readout 四个类。下个 cell 只把 **源码字符串保存成文件**，不会 import ARTIQ。类继承 `HasEnvironment`；`build` 绑定设备，`@kernel` 方法保留原生写法。我们只用 TTL 占位演示控制流，不声称实现了这些物理过程。

真实模块需要以文件形式提供，便于静态审计、固定依赖源码与 hash；只在 notebook 临时定义一个类无法得到同等的可追溯导入边界。包的顶层代码、build 和显式 host_prepare 在真实 ARTIQ 构造阶段会执行，因此不应藏入启动实验的副作用。
""")

# The companion is checked in, so notebook regeneration does not depend on output/.
native_text = (ROOT / "docs/tutorials/native_components_walkthrough.md").read_text(
    encoding="utf-8"
)
native_cells = re.findall(r"```python\n(.*?)\n```", native_text, flags=re.S)
assert len(native_cells) == 2, (
    "Native walkthrough must contain exactly two Python cells"
)
code(native_cells[0])
md(r"""
### 声明与源码分开：NativeModule / NativeStage / NativeProgram

`NativeModule` 记录类、源码、资源、静态构建参数及生命周期；`NativeStage` 为方法指定公开名字、阶段、精确时长和结束边界；`NativeProgram` 组合命名组件。调用 `native.GM_cooling(...)` 时只是加入一个未来调用节点。

同一个源码文件可以定义多个组件，各自实例独立，所以同名属性不会互相覆盖。共用设备必须声明 shared_resources，并指定唯一初始化责任方；本例 MOT 初始化准备 TTL，cooling/state 只顺序使用它。原生资源不能与 DSL 的输出或 AWG trigger 偷偷复用。
""")
code(native_cells[1])
md(r"""
生命周期顺序是：根程序管理 reset／设备初始化 → 会话 initialize 一次 → 每 shot 的 prefix、timed、DSL、suffix → 每 shot after_shot → 会话 finalize 一次。换 chunk 保持同一个 root 和原生实例，不重新初始化设备。未显式声明的 `run/prepare/analyze` 不会自动调用；需要主机准备时用 `host_prepare="method"`。

当前支持 **`prefix* → timed* → DSL → suffix*`**，原生调用须为 realtime 直接子节点，不能塞进 Section/layer/triggered_sequence，也不能任意夹在两个门之间。prefix 可以包含可变准备；严格 timed/suffix 在实际 kernel 返回时检查游标是否恰好等于声明时长，过长过短都会报错，不补等待。time audit 与离线编译不能证明模拟装置已稳定。

`duration=20*us` 不会从实参自动推断；本例必须传入匹配的 `20*us`。严格段禁止 reset、break_realtime、阻塞等待和 RPC。前级结束后还要留出容纳 AWG 提前触发的显式 gap；编译器不会把触发倒排进不透明原生段。

`source_root` 只是我们的路径变量，不是 NativeModule 的参数。构建/导出只读源码、审计和 hash 固定；真正加载时才 import，需要 ARTIQ worker 能导入这个包且解析到被固定的路径。改源码或移动路径后须重建声明和产物。
""")

md(r"""
## 7. 组合一个完整的 Ramsey，再对照现有 GreenMOT

这里用两个 dark_time 扫描点，把准备、门段和读出接起来。我们显式声明 `acquire`，为结果建立 `bits` 句柄；它的 64 ns 是触发窗口，**不是**相机曝光、取图或分类时长。随后的 native readout 使用另一个 TTL 演示照明阶段。

请把这段当作以后创建新实验的主要模板：所有变化都在参数、门函数和 NativeProgram 中，实验正文只表达顺序。
""")
code(r"""
dark = SweepParameter("dark_time", [1, 2], "us")
ramsey_exp = Experiment("tutorial_ramsey", qubits, metadata={
    "offline_only": True, "hardware_verified": False,
    "physical_preparation_verified": False,
})
with ramsey_exp.average(2, mode="cyclic", scope="chunk"):
    with ramsey_exp.sweep(dark, chunks="auto"), ramsey_exp.realtime():
        native.prepare_MOT()
        native.GM_cooling(20 * us)
        native.state_prep(8 * us)
        delay(10 * us)
        with ramsey_exp.triggered_sequence(group="qubit_awg"):
            reset_frame(q0.frame)
            ramsey_gates(q0, dark, virtual_angle=math.pi / 2)
        acquire("camera.trigger", duration=64 * ns, handle="bits", qubits=qubits,
                metadata={"classification_required": True})
        native.readout(10 * us)

ramsey_compiled = lab.compile(ramsey_exp)
assert ramsey_compiled.metadata["native_artiq"]["source_audit"]["status"] == "passed"
assert "tutorial_native.stages" not in sys.modules
assert "artiq" not in sys.modules
ramsey_export = ramsey_compiled.export(SESSION / "ramsey_export")
table(ramsey_compiled.acquisition_plan(0))
print("原生源码审计通过；导出:", ramsey_export)
""")
md(r"""
### 现有 GreenMOT 示例目前保留兼容入口

`examples/artiq_spectrum/greenmot_ramsey.py` 使用 `GreenMOTAdapter`，内部仍是单组件的 `NativeKernelPackage`；它没有被暗中改成四组件 NativeProgram。默认是 fixture。传入真实 `_GreenMOTbase.py` 后才固定、审计外部源码。

真实 GreenMOT 准备本来就含冷却。适配器把可变 prefix 与最后固定尾段拆开，并检查组合与原函数的 AST 一致；严格尾段声明到最后 Zotino LDAC 数字上升沿，共 `15 ms + 16 ns`。默认门前 gap 10 µs 仍需实际模拟稳定时间支持。不要把教学里的 `GM_cooling` 再额外当成真实 GreenMOT 必需的一次冷却。

旧例的 `execute_readout()` 是原生读出调用，没有 DSL acquire 句柄，不会自动产生相机数据或 ResultSet。新实验若要持久恢复，需要把触发、取图、分类、保存和结果提交分别接好。旧 `finalize_method` 仍按每 shot 收尾；新 NativeProgram 的 finalize 才是会话结束。
""")
code(r"""
from examples.artiq_spectrum import greenmot_ramsey

green_exp = greenmot_ramsey.build_experiment(delays_ms=(0.01,), shots=2)
green_lab = LabConfig.load(ROOT / "examples/artiq_spectrum/lab_simulated.json")
green_compiled = green_lab.compile(green_exp)
assert green_exp.metadata["native_source"] == "native_fixture"
assert green_compiled.acquisition_plan() == []
print("实际 GreenMOT 示例来源:", green_exp.metadata["native_source"])
print("对应卡触发映射:", green_lab.setup.spectrum.groups["qubit_awg"].card_trigger_devices)
print("原生 readout 不自动创建 acquisition handle:", green_compiled.acquisition_plan())
""")
md(r"""
接入真实 GreenMOT 时，显式向 `build_experiment(greenmot_source=Path(...))` 提供本地原文件，并使用本实验室 LabConfig/device_db。原文件来源的字节身份、设备初始化、固定尾段与模拟稳定时间需要验收。真实外部路径不在 Run All 中猜测或加载。

新建多组件实验优先采用上面的 NativeProgram。当前 `compile_stream` 的原生路径要求 NativeProgram，不能直接拿旧 GreenMOT NativeKernelPackage 走流式路径。要迁移真实 GreenMOT，应保留经过核对的准备分段语义，再实现符合新入口约定的组件，而不只是改类名。
""")

md(r"""
## 8. 完整双比特 RB 与 CZ interleaved RB：先固定电路，再编译波形

我们先生成完整 11,520 元素双比特 Clifford 群上的均匀随机序列。`length` 是随机 Clifford 数，不包含 recovery，也不是 pulse 数。reference 与 interleaved 共享同一组随机 Clifford；后者在每个随机 Clifford 后插入 CZ，并单独求精确 recovery。

这里做两种独立检查：理想 tableau 的总变换回到 identity；配对随机 Clifford 完全一致。这些检查证明逻辑代数，不证明下面的 pulse 实现了 CZ。固定 seed 后，改变 shot 数、平均和 chunk 不应重新抽电路。
""")
code(r"""
LENGTHS = (0, 1, 4, 12)
SEQUENCE_COUNT = 3
SEED = 101
VARIANTS = {0: "reference", 1: "interleaved"}
rb_sequences = generate_rb_sequences(
    LENGTHS, sequence_count=SEQUENCE_COUNT, seed=SEED,
    num_qubits=2, interleaved=True,
)
identity = sequence_tableau((), num_qubits=2)
assert len(clifford_catalog(1)) == 24
assert len(clifford_catalog(2)) == 11520
assert all(sequence_tableau(seq.gates, num_qubits=2) == identity for seq in rb_sequences)
descriptors = {(s.length, s.sequence, s.variant): s for s in rb_sequences}
for length in LENGTHS:
    for number in range(SEQUENCE_COUNT):
        assert (descriptors[length, number, "reference"].clifford_indices
                == descriptors[length, number, "interleaved"].clifford_indices)
table([{"length": s.length, "sequence": s.sequence, "variant": s.variant,
        "primitive gates incl. recovery": len(s.gates), "recovery": s.recovery_index}
       for s in rb_sequences[:8]])
""")
md(r"""
### 可替换的 CZ 门体与 PairCalibration

下面两路 clock pulse **只是编译占位，不是物理 CZ，也不能建立纠缠**。真实门必须替换整个门体，加入实际相互作用、脉冲参数和测得的局部相位补偿。`PairCalibration` 保存端点角色、实现参数、版本依赖及补偿；它不会自动把这些数字变成正确门体。

`QubitOperations.cz()` 检查所选 pair 与 qubit 快照，再调用给定实现。补偿应在实现里明确施加；不要假设写入 phase_corrections 就会自动执行。
""")
code(r"""
pair = PairCalibration(
    (q0.uid, q1.uid), "tutorial_placeholder_cz",
    status="demonstration",
    parameters={"physical_cz_verified": False, "clock_rotation": "pi"},
    qubit_revisions={q.uid: q.revision for q in qubits},
    qubit_fingerprints={q.uid: q.fingerprint for q in qubits},
    phase_corrections={q.uid: 0.0 for q in qubits},
    source="offline teaching only; replace with a calibrated interaction",
)

@qubit_operation
def placeholder_cz(control, target):
    pair.validate(control, target)
    with layer(control, target):
        ops.clock(control, rotation="pi", phase=0.0)
        ops.clock(target, rotation="pi", phase=0.0)
    # 真实实现才有实验测得的补偿；此处零值不代表已校准。
    for q in (control, target):
        shift_phase(q.frame, pair.phase_corrections[q.uid])

rb_ops = QubitOperations(drive="rf", cz_impl=placeholder_cz, pair_calibrations=(pair,))
pair.save(SESSION / "pair_calibration_demo.json")
try:
    pair.validate(updated, q1)
except ValueError as error:
    print("预期：q0 更新后旧 pair 不能沿用:", error)
else:
    raise AssertionError("旧 pair 应拒绝新 qubit revision")
""")
md(r"""
### 把 length × sequence × variant 写成实验

循环里的 `case` 是构建期选择结构：每个扫描点只编译匹配的门体，不会把所有随机序列串起来执行。`rb_sequence_section` 把门词映射到 operation 与有边界的 layer，我们不需要在实验里手写 Clifford 分解器。

非时间的 sweep 必须显式写 `"dimensionless"`；默认时间单位不适用于 length/sequence。下面函数接收已经冻结的 descriptors，因此 chunk 不会改变随机电路。
""")
code(r"""
def make_rb_experiment(*, chunks="auto", shots=256):
    exp = Experiment("tutorial_2q_rb_irb", qubits, metadata={
        "experiment_type": "rb", "num_qubits": 2,
        "seed": SEED, "variant_labels": VARIANTS,
        "rb_sequences": [s.to_dict() for s in rb_sequences],
        "offline_only": True, "hardware_verified": False,
        "physical_cz_verified": False, "native_fixture": True,
        "calibrated_rotations": False,
    })
    length_axis = SweepParameter("length", LENGTHS, "dimensionless")
    sequence_axis = SweepParameter("sequence", range(SEQUENCE_COUNT), "dimensionless")
    variant_axis = SweepParameter("variant", tuple(VARIANTS), "dimensionless")
    with exp.average(shots, mode="cyclic", scope="chunk"):
        with (exp.sweep(length_axis, chunks=chunks), exp.sweep(sequence_axis),
              exp.sweep(variant_axis), exp.realtime()):
            native.prepare_MOT()
            native.GM_cooling(20 * us)
            native.state_prep(8 * us)
            delay(10 * us)
            with exp.triggered_sequence(group="qubit_awg"):
                for q in qubits:
                    reset_frame(q.frame)
                    reset_frame(q.phase_frame("clock_control"))
                for length in LENGTHS:
                    with exp.case(length_axis, length):
                        for number in range(SEQUENCE_COUNT):
                            with exp.case(sequence_axis, number):
                                for variant, label in VARIANTS.items():
                                    with exp.case(variant_axis, variant):
                                        rb_sequence_section(
                                            qubits, descriptors[length, number, label], rb_ops
                                        )
            acquire("camera.trigger", duration=64 * ns, handle="bits", qubits=qubits,
                    metadata={"joint_readout": True, "classification_required": True})
            native.readout(10 * us)
    return exp

rb_exp = make_rb_experiment()
rb_compiled = lab.compile(rb_exp)
assert len(rb_compiled.points) == len(LENGTHS) * SEQUENCE_COUNT * len(VARIANTS)
print("全局点数:", len(rb_compiled.points), "shots/point:", rb_compiled.shots)
print("首末坐标:", rb_compiled.points[0].coordinates, rb_compiled.points[-1].coordinates)
""")
md(r"""
现有 `examples/artiq_spectrum/randomized_benchmarking.py` 做的也是这几步。熟悉后可直接用其 `build_experiment(qubits=..., operations=..., native_program=..., chunks="auto")`，避免重复搭扫描结构；本教程保留完整正文是为了说明每一层如何配合。单比特入口使用同一模块的 `num_qubits=1, interleaved=False` 和完整 24 Clifford，不需要 CZ。

clock 与核自旋相干组合时，门体决定其物理补偿关系。本教程只分别清除两个控制 frame，不能据此得出真实相干转移后的量子相位已经正确。
""")

md(r"""
## 9. 自动分块：先选轴，再让编译器决定边界

本例在外层 length 轴设置 `chunks="auto"`：一个 length 值包含其全部 sequence × variant 内层组合，不能拆散。固定外层坐标后，沿所选轴连续扩块，检查每张卡的真实去重 segment、槽位、最长段和 step 限制。

| 写法 | 含义 |
|---|---|
| `chunks=2` | 手动分成两块，严格保留指定含义 |
| `chunk_size=2` | 每块最多两个该轴值，不等于两块 |
| `chunks="auto"` | 编译器据卡容量／受控工作集选择边界；小实验可以只有一块 |

如果一个 length 的全部内层组合放不下，考虑把分块轴移到 sequence；若单 shot 自身超限，则不能靠换一个外层 chunk 参数解决。目前不自动拆单 shot，也不自动选择轴或同时分多层。

`average(..., scope="chunk", mode="cyclic")` 让本块扫描点完成平均后再换 bank，避免每个平均轮次重新上传所有块；它与跨全扫描点 cyclic 的采样时间顺序不同，选择时要考虑漂移，不能认为改变执行次序没有实验影响。结果仍使用全局坐标。
""")
code(r"""
table([{"chunk": chunk["index"], "global points": chunk["points"]}
       for chunk in rb_compiled.host_execution_plan["chunks"]])

manual_compiled = lab.compile(make_rb_experiment(chunks=2, shots=2))
table([{"manual chunk": chunk["index"], "global points": chunk["points"]}
       for chunk in manual_compiled.host_execution_plan["chunks"]])
assert len(manual_compiled.host_execution_plan["chunks"]) == 2
del manual_compiled
""")
md(r"""
### 大实验用 CompilationStore，先编译完再开始执行

`lab.compile()` 返回内存中的 CompiledExperiment；`compile_stream()` 逐点落盘、逐块导出，按最终样本 hash 去重，全部完成后才提交完整索引。不会在仪器等待下一块时临时编译。目录必须是新的；当前不支持中断后续编。

预算约束编译器计量的工作缓冲，**不是进程 RSS 上限**。输入 AST、RB 门词、全局点号／文件索引、Python 对象开销和外部编译器仍占内存。不要把所有 `load_chunk()` 的结果又积在一个列表里。本教程为了对照才同时保留小规模 eager 结果；真实大实验应直接走 stream。下面特意使用较小的 8 MiB 预算演示因主机工作集提前结束自动块；这不是声明卡的实际内存只有 8 MiB。
""")
code(r"""
artifact = lab.compile_stream(
    rb_exp, directory=SESSION / "rb_artifact",
    host_memory_budget_bytes=8 * 1024**2,
)
artifact.verify()
table([{"chunk": c["index"], "global points": c["points"], "directory": c["directory"]}
       for c in artifact.manifest["chunks"]])
print("artifact SHA-256:", artifact.sha256)
print("托管工作区峰值 bytes:", artifact.manifest["managed_peak_bytes"])
print("该值不是 Python 进程 RSS。")
""")
md(r"""
### 查看最终 DAC，而不只查看 IR

逻辑/物理 IR 报告解释“为什么等待、占了什么资源”；最终输出视图解码实际将上传的 packed int16 DAC 样本，能反映量化、segment、卡间 padding 等结果。它仍不含线缆、放大器和原子响应；真实捕获要与预测另行比较。

下面只取最后一个全局点的一小段时间窗口，核对流式与普通编译的样本一致，再画两路 RF。时间是 shot 的 T0 坐标，不要把 point 编号或 chunk 内编号混用。
""")
code(r"""
global_point = len(rb_compiled.points) - 1
chunk_info = artifact.chunk_for_point(global_point)
local_point = chunk_info["points"].index(global_point)
one_chunk = artifact.load_chunk(chunk_info["index"])
assert one_chunk.points[local_point].coordinates == rb_compiled.points[global_point].coordinates
first_pulse = min(e.start_mu for e in rb_compiled.points[global_point].logical_events
                  if e.kind in ("play", "trigplay"))
start_s = first_pulse * rb_compiled.setup.tick_seconds
window = rb_compiled.simulate_output(point=global_point, start_s=start_s, duration_s=2e-6)
stream_window = one_chunk.simulate_output(point=local_point, start_s=start_s, duration_s=2e-6)
print("输出窗口字段:", list(window))
""")

# Compare final DAC fields; native lifecycle metadata uses chunk-local indices.
code(r"""
def compare_arrays(a, b):
    if isinstance(a, dict):
        assert set(a) == set(b)
        for key in a:
            compare_arrays(a[key], b[key])
    elif isinstance(a, (list, tuple)):
        assert len(a) == len(b)
        for left, right in zip(a, b):
            compare_arrays(left, right)
    elif isinstance(a, np.ndarray):
        np.testing.assert_array_equal(a, b)
    else:
        assert a == b, (a, b)

# 全局点号与块内点号不同；对照同一坐标的时间、最终样本及 pulse 相位配方。
for field in ("coordinates", "start_s", "duration_s", "time_reference", "traces"):
    compare_arrays(window[field], stream_window[field])
assert rb_compiled.frame_phases(global_point) == one_chunk.frame_phases(local_point)
print("本窗口普通/流式最终输出一致。")
del one_chunk, stream_window
""")
code(r"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 每个通道都来自最终 DAC 解码。结构在前一 cell 中可检查。
fig, ax = plt.subplots(figsize=(10, 3))
for channel in window["traces"]:
    if channel["channel"] == 0:
        ax.plot(np.asarray(channel["time_s"]) * 1e6,
                channel["voltage_v"], label=f"card {channel['card']} / RF", lw=0.8)
ax.set(xlabel="Time from shot T0 (us)", ylabel="Predicted DAC voltage (V)",
       title="Final packed DAC prediction — no hardware measurement")
ax.legend()
fig.tight_layout()
waveform_png = SESSION / "final_dac_window.png"
fig.savefig(waveform_png, dpi=140)
plt.close(fig)
display(Image(filename=str(waveform_png)))
""")
code(r"""
report = artifact.export_report(SESSION / "rb_report")
print("目录式报告:", report)
print("请在浏览器打开报告目录中的 index.html；按 chunk/窗口加载。")
""")
md(r"""
报告按 chunk 载入元数据、按当前窗口载入 packed 样本，兼容本地目录打开；不要把大实验全部嵌入一个 HTML。当前教程会检查生成文件，但浏览器真实渲染、窗口切换与缓存释放属于单独验收。

捕获比较接口见 `docs/compiled_viewers.md`：NPZ 要声明卡／通道、时间参考、产物和校准版本。验收只使用声明参考与固定 deskew；拟合平移后“看起来重合”只能用于探索，不能宣称同步达标。
""")

md(r"""
## 10. 联合读出与科学分析：先保留 shot，再统计 P00

`acquire` 只声明需要的观测。真实相机适配器负责获取图像、分类成同一 shot 的 `(q0, q1)`，保存原图引用和 loss/invalid 状态，再写入 ResultSet / RunStore。

下面用已知的**人工去极化曲线**生成分类 bits，目的是演示数据接口与分析：`P00 = 0.70 p^m + 0.25`，reference 的 p=0.96，interleaved 的 p=0.96×0.98。我们先抽一个联合四态，再写两位，绝不用边缘均值的乘积代替 P00。它与前面占位 CZ 波形的真实演化没有联系。
""")
code(r"""
results = ResultSet.from_compiled(rb_compiled, metadata={
    "data_source": "synthetic_depolarizing_model_not_hardware",
    "alpha_reference": 0.96, "cz_ratio": 0.98,
})
rng = np.random.default_rng(20261112)
for point in rb_compiled.points:
    m = int(point.coordinates["length"])
    alpha = 0.96 * (0.98 if point.coordinates["variant"] == 1 else 1.0)
    p00 = 0.70 * alpha**m + 0.25
    states = rng.choice(4, size=rb_compiled.shots,
                        p=[p00, (1-p00)/3, (1-p00)/3, (1-p00)/3])
    for shot, state in enumerate(states):
        for bit, q in enumerate(qubits):
            results.record("bits", point.index, shot, q.uid,
                           int((state >> (1-bit)) & 1),
                           labels={"source": "synthetic"})
results.save(SESSION / "synthetic_results")
counts = joint_rb_counts(results, "bits", qubits=("q0", "q1"), variant_labels=VARIANTS)
table([c.to_dict() for c in counts[:4]])
""")
md(r"""
拟合 `A p^m + B`，SPAM 主要进入 A/B。维度 d=2 或 4 时，标准 EPC 为 `(d−1)(1−p)/d`；CZ IRB 的比值估计需要模型条件，不等于无条件的真实门误差。代码保留原始负估计，分别报告配对 sequence bootstrap 区间与理论误差界。参考 [完整双比特 RB](https://arxiv.org/abs/1210.7011) 与 [IRB 方法及误差界](https://arxiv.org/abs/1203.4550)；本项目的 CZ 分解与演示波形不是这些论文的实验实现。

本例每个 length 只有 3 组随机序列、256 shots，适合教学而不适合作为精确表征设计。随机数据的有限样本误差和拟合相关性会使结果偏离设定值；不把一个接近设定值的图称为硬件验证。
""")
code(r"""
analysis = analyze_rb_counts(counts, bootstrap_samples=40, seed=1234)
analysis["data_source"] = "synthetic_depolarizing_model_not_hardware"
(SESSION / "synthetic_rb_analysis.json").write_text(
    json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
)
table([{"variant": key, **value} for key, value in analysis["fits"].items()])
print("CZ IRB（模型条件下）:", analysis["interleaved_cz"])
print("配对 sequence bootstrap:", analysis["bootstrap"])
print("人工模型目标：reference EPC =", 0.75 * (1-0.96),
      "; CZ ratio estimate =", 0.75 * (1-0.98))
""")
code(r"""
fig, ax = plt.subplots(figsize=(8, 4))
xx = np.linspace(0, max(LENGTHS), 200)
for variant, marker in (("reference", "o"), ("interleaved", "s")):
    selected = [c for c in counts if c.variant == variant]
    ax.scatter([c.length for c in selected], [c.probability for c in selected],
               marker=marker, alpha=0.65, label=f"{variant}: synthetic sequences")
    fit = analysis["fits"][variant]
    ax.plot(xx, fit["A"] * fit["alpha"]**xx + fit["B"],
            label=f"{variant}: fit")
ax.set(xlabel="Random Clifford count (excluding recovery)", ylabel="Joint P00",
       title="Synthetic RB / CZ IRB — not waveform simulation or hardware data")
ax.legend(fontsize=8)
fig.tight_layout()
rb_png = SESSION / "synthetic_rb.png"
fig.savefig(rb_png, dpi=140)
plt.close(fig)
display(Image(filename=str(rb_png)))
""")
md(r"""
### loss 不能悄悄丢掉

原始 ResultSet 保存 `atom_lost/loading_failed` 与无效状态。默认 joint_rb_counts 遇到 loss 会拒绝，要求明确策略。`count_as_failure` 改为把 loss 计作失败，`exclude` 是有条件的后选择，会改变统计对象，不能直接沿用标准 EPC 解释。下面改的是重新加载的教学副本，已保存的原始合成结果保留。
""")
code(r"""
loss_demo = ResultSet.load(SESSION / "synthetic_results")
loss_demo.record("bits", 0, 0, "q0", status="atom_lost")
try:
    joint_rb_counts(loss_demo, "bits", qubits=("q0", "q1"), variant_labels=VARIANTS)
except ValueError as error:
    print("预期：需要显式选择 loss 策略:", error)
else:
    raise AssertionError("不应静默后选择 loss")
loss_counts = joint_rb_counts(
    loss_demo, "bits", qubits=("q0", "q1"), variant_labels=VARIANTS,
    loss_policy="count_as_failure",
)
print("原始 shot 数、loss 数、统计分母:",
      loss_counts[0].shots, loss_counts[0].lost, loss_counts[0].denominator)
""")

md(r"""
### 持久执行记录与恢复：用小 Ramsey 产物做 host mock

这里复用明确命名的 offline helper，只验证主机 bank／dispatch／SQLite／恢复控制流。卡是 mock，生成的 ARTIQ kernel 没被执行；也没有模拟 MOT 或门的物理效果。使用只有四个 shot 的 Ramsey，避免为演示日志重复整个 RB 数据集。

真实持久模式记录 execution、point、shot、attempt、bank 和已保存数据引用。硬件完成和数据提交是两个不同阶段。恢复跳过已提交项；硬件已完成但没提交数据时，只有探测器支持找回才可补取。unknown 默认停止，必须明确重试／跳过，保留原 attempt，不承诺物理执行恰好一次。
""")
code(r"""
from examples.artiq_spectrum.rb_offline_run import run_offline_journal

ramsey_artifact = lab.compile_stream(
    ramsey_exp, directory=SESSION / "ramsey_artifact",
    host_memory_budget_bytes=8 * 1024**2,
)
mock_summary = run_offline_journal(
    ramsey_artifact, SESSION / "mock_run",
    lambda coordinates, shot: {
        "source": "synthetic_mock_not_detector", "coordinates": coordinates,
        "shot": shot, "joint_bits": [0, 0],
    },
)
assert mock_summary["dispatches_on_completed_resume"] == 0
assert mock_summary["data_commits"] == 4
assert not mock_summary["hardware_executed"]
print(json.dumps(mock_summary, indent=2))
""")

md(r"""
## 11. 换成自己的实验：哪些文件、参数和方法要改

| 部分 | 你需要提供的内容 | 本教程默认值不能证明什么 |
|---|---|---|
| QubitStore | RF/Raman/clock 频率、π/π2 时长与幅度、形状、跃迁、位置、版本 | 参数存在不等于已校准 |
| 相位与门库 | 正负旋转定义、相干转移补偿、实际 CZ 相互作用与局部补偿 | 独立 frame 不自动求多能级传播；理想 recovery 不证明 pulse 实现 |
| LabConfig + device_db | 卡串号、TTL、输出、时钟、RTIO 网格与容量、SPI/共享资源 | 同时间戳不等于模拟同步 |
| NativeProgram | 真实 MOT/cooling/state/readout 类、所有依赖源码、资源、生命周期 | fixture TTL 不等于冷却或探测 |
| 准备结束与 gap | 数字结束边界、模拟稳定时间、触发提前量 | 10 µs 只是教学值 |
| Detector / ResultSet | 相机曝光与读取、同 shot 分类、loss/invalid、图像引用 | acquire 不是自动取图 |
| RunStore adapter | 持久提交、attempt 对应、重启后数据找回 | SQLite 不能消除硬件完成与数据保存间的歧义 |
| RB 设计与分析 | 随机序列数、长度、shots、采样次序、loss 模型 | 三序列教学拟合不能替代正式统计设计 |

建议把实验室文件组织为：

```text
my_lab/
  lab.json                  接线与硬件合同
  qubits.json               单比特快照
  pairs/                    双比特校准
  native/                   可导入的原生 ARTIQ 包
  native_program.py         Module / Stage / Program 声明
  operations.py             实际门体与显式相位补偿
  experiments/              Ramsey、RB 等构建函数
  detector.py               分类与持久数据适配
output/<run>/               不可混写的编译产物、执行记录、原始图像与报告
```

实验编写的通常顺序是：确认当前校准快照 → 构建门与 PairCalibration → 组合实验段 → 声明扫描/平均/分块轴 → 离线编译与查看最终 DAC → 对受影响原生调用链真实 ARTIQ 离线编译 → 小规模仪器与模拟时序验收 → 运行、持久保存、分析 → 产生下一版校准 proposal。参数更新后重建依赖，不修改已编译产物。
""")
md(r"""
### 真实 ARTIQ 离线编译是单独环境中的步骤

本 notebook 使用普通 CPython 生成源码。真实编译使用项目已有 `scripts/artiq-python.ps1` 与 `scripts/compile_native_artiq.py`，目标是断开设备库下的 `Core.compile`，不运行 kernel。原生教学包的 import root 是本次 `SESSION/native_sources`；还需为 `tutorial_prepare/tutorial_camera` 提供断开的 TTL fixture。

完整示例与参数见同目录的 [原生源码 walkthrough](native_components_walkthrough.md) 和 [原生接入说明](../native_components.md)。正式执行时应在 ARTIQ worker 正规安装实验室包，保持解析路径与源码 pins 一致，并使用真实 device_db、Spectrum adapter、探测器与经过测量的合同。不要在这个 notebook 的 Run All 中加入启动仪器的命令。
""")

md(r"""
## 12. 之前计划还剩什么？下一步做什么更有价值？

经过当前源码与计划核对，没有发现可以直接认定“已经批准的核心软件功能尚未编写”的新增遗漏，但**软件交付不等于实验室验收完成**。详见 [剩余工作与优先级](remaining_work_and_priorities.md)。

1. **尚未完成的验收**：真实浏览器中的报告操作；2/3/4 卡 TTL／模拟同步；SDK/DMA/ready/re-arm/RTIO 负载；真实准备与相机接口；物理 CZ 和局部补偿；中断后的探测数据找回。GreenMOT 上游原始字节身份也尚未完成核对。
2. **现有限制值得改善**：旧 GreenMOT 仍用 NativeKernelPackage，不能直接 stream；native 仅支持准备→DSL→读出；大实验的 AST、全部 RB 门词、点号/hash 索引和初始化汇总仍会增长。可以优先做惰性 RB 构建、磁盘/范围索引、初始化器逐块去重和按规模的内存测量。
3. **此前明确延期**：Spectrum DDS 引擎、同 qubit 跨 DDS/AWG 相干控制、任意门间原生交错/反馈、多层自动分块、单 shot 拆分、中断后续编、任意 Section 重绑定、独立 circuit 编译器、多能级相位自动推导。不要把这些当成已经可用的能力。

建议顺序：先把一个真实 Ramsey 的准备、单比特门、读出和捕获闭环做稳；再完成两卡同步与校准门库；然后接入实际 CZ 与配对 IRB；有真实规模数据后优化流式编译索引和惰性门生成。这样每一步都有可测量的收益，不必先扩展更多语法。
""")

md(r"""
## 13. 自己动手的三个练习

**练习 A：改相位。** 把 `ramsey_gates(..., virtual_angle=π/2)` 改为 0，重新构建并编译，比较第二个 RF pulse 的最终 DAC。再改成 `reset_frame`，解释与 `reset_phase` 的差别。不要就地编辑 `compiled`。

**练习 B：改并行。** 对比串行 X180(q0)/Y90(q1)、start layer 和 end layer 的时间表。把 q1 改成 q0，观察同 qubit 分支冲突为什么应当拒绝。

**练习 C：改分块。** 固定 SEED/descriptors，比较 `chunks=2` 和 auto 的 chunk 表、每点坐标与样本；再把轴移到 sequence，解释为什么平均顺序和漂移敏感性可能变化，但随机 Clifford 不应变化。

阅读顺序建议：[Qubit 与门](../qubit_parameters_and_operations.md) → [编写与触发](../authoring_and_triggers.md) → [原生组件](../native_components.md) → [流式与恢复](../streamed_execution.md) → [最终波形与捕获](../compiled_viewers.md)。历史设计保留在根目录计划文件中；当前相位规则以统一 F 与本教程说明为准。
""")
code(r"""
lesson_summary = {
    "kind": "offline_tutorial_run",
    "python": sys.version.split()[0],
    "session": str(SESSION),
    "rb_points": len(rb_compiled.points),
    "rb_artifact_sha256": artifact.sha256,
    "ramsey_source_audit": ramsey_compiled.metadata["native_artiq"]["source_audit"]["status"],
    "checks": ["qubit revision", "section copy isolation", "independent frames",
               "Clifford sizes and recovery", "paired circuits", "pair revision",
               "artifact integrity", "eager/stream DAC window", "loss policy",
               "mock committed resume"],
    "hardware_executed": False,
    "generated_artiq_kernels_executed": False,
    "synthetic_results": True,
    "browser_render_verified": False,
}
(SESSION / "lesson_summary.json").write_text(
    json.dumps(lesson_summary, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("课程产物已保存:", SESSION)
print("实验室执行仍需本教程第 11 节的实际配置与验收。")
""")


def main():
    notebook = nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3.12 (taiyidsl)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
            "taiyidsl": {
                "offline_only": True,
                "source_generator": "scripts/build_hybrid_tutorial.py",
            },
        },
    )
    # Stable IDs keep regeneration diffs readable.
    for index, cell in enumerate(notebook.cells):
        cell.id = f"hybrid-lesson-{index:03d}"
    nbformat.validate(notebook)
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, DESTINATION)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "I,F401",
            "--fix",
            str(DESTINATION),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", str(DESTINATION)], check=True
    )
    print(f"Wrote {DESTINATION}: {len(cells)} cells")


if __name__ == "__main__":
    main()
