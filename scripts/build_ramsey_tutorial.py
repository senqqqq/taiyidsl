"""Build a short Chinese Ramsey lesson with editable, fully inline declarations."""

# Chinese prose and tables are intentionally not wrapped inside string literals.
# ruff: noqa: E501
import subprocess
import sys
import textwrap
from copy import deepcopy
from pathlib import Path

import nbformat

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "examples/artiq_spectrum/ramsey_lab"
cells = []


def md(source):
    cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(source).strip()))


def code(source):
    cells.append(nbformat.v4.new_code_cell(textwrap.dedent(source).strip()))


md("""
# Ramsey 与 clock_ramsey：从接线到连续实验波形

**操作版入口：`ramsey_clock_walkthrough.ipynb`；第一次使用可先读 [plain版](ramsey_clock_plain.ipynb)。** 本 notebook 包含两个完整实验：

| 实验 | gate sequence | 扫描 |
|---|---|---|
| Ramsey | RF π/2 → dark time → RF π/2，第二脉冲附加相位0° | dark time = 1 / 10 / 100 μs |
| clock_ramsey | RF100 μs → clock200 μs → RF100 μs | 第二RF附加相位 = 0 / 45 / 90 / 135 / 180° |

clock 波形载波为200 kHz，π时长100 μs、2π时长200 μs。两个实验的RF载波均为31 kHz（校准30 kHz加1 kHz detuning）。
本文件取代旧入门链接；旧 `ramsey_from_scratch.ipynb` 保留，不覆盖其中可能尚未整理的编辑。

这是一条入门路径：**device_db / spec_db → descriptor → LabConfig 与 Qubit → native ARTIQ → 实验定义 → compile 与可视化 → 真实硬件连接**。
下面的数据库、descriptor、Qubit 和原生入口都先完整展示，允许直接修改。最后才介绍如何分文件保存。

默认 Run All **只离线编译**，不连接仪器。GreenMOT 使用现有 TTL kernel 演示包；它不制备原子。
频率、幅度、脉宽和触发延迟均为示例，不代表物理校准。读出函数触发相机，不自动取得图像。

环境：项目根目录执行 `uv sync --locked --group notebooks`，然后 `uv run --locked --group notebooks jupyter lab`。
选择项目 CPython 3.12 kernel。只有真实 ARTIQ 编译/运行才需要独立的官方工具链。
本教程不需要先学习 RB、磁盘编译、bank、运行数据库或 Spectrum SDK。
""")
code("""
from pathlib import Path
from IPython.display import Markdown, display

from taiyidsl import (
    DeviceSetup, Experiment, LabConfig, NativeKernelPackage, Qubit,
    QubitOperations, Square, SweepParameter, Hz, deg, delay, kHz, ms, ns, play,
    qubit_operation, reset_phase, us, experiment, average, sweep, realtime, triggered_sequence,
)

ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src/taiyidsl").is_dir())
OUTPUT = ROOT / "output/simple_ramsey"
OUTPUT.mkdir(parents=True, exist_ok=True)

""")
md("""
## 1. 两个设备库：设备是什么，在哪里

`device_db` 就是 ARTIQ 原有的设备字典。下面为了从零教学而给出一份最小示例；它不能代替已经控制 MOT 的完整 `device_db.py`。
`core.arguments.host` 是通过网络连接 core 的地址；这里用文档示例地址 `192.0.2.10`。TTL 的 `channel` 是 gateware 的 RTIO 编号，不是网络地址。
**离线与否由 `emulation=True` 和是否显式 connect 决定，不由有没有填地址决定。** 两个 AWG trigger 与 MOT/camera 的 TTL 是不同设备。

下面的字典就是 `device_db.py` 的完整示例内容；不会 import ARTIQ 驱动。
""")
code((LAB / "device_db.py").read_text(encoding="utf-8"))
md("""
### 已有 MOT device_db.py 时怎么办

继续以实验室原文件为唯一来源，保留其中的 core、DDS、Zotino、TTL 和控制器条目。不要用上面的最小字典覆盖它，也不要把两份数据库机械合并。

1. 在真实接线中选出两路未被 MOT/读出占用的 TTL，并确认它们已经存在于原 device_db。
2. descriptor 的 `triggers` 可以直接引用原名字，例如 `{"A": "ttl8", "B": "ttl9"}`；这样无需新增设备条目。
3. 如果希望统一实验室命名，可在原 `device_db` 字典中添加别名：`"awg_trigger_a": "ttl8"`、`"awg_trigger_b": "ttl9"`。这是给已有线路另起名字，不是创建新 RTIO 通道。
4. 只有实际增加硬件且 gateware 已支持时，才按真实设备信息增加条目。别名和 channel 编号不能猜测。

日常使用直接传路径：
```python
setup = DeviceSetup.from_descriptor(
    descriptor,
    device_db="C:/my_lab/device_db.py",  # 原 MOT 设备库
    spec_db="C:/my_lab/spec_db.py",
)
```

加载器静态读取声明式 `.py`；如果原文件用循环、函数或 import 动态生成设备，应在你信任的配置环境中先求值，再把得到的 `device_db` 字典传入。DSL 不为了查看接线而偷偷执行原文件。
原生包与 DSL 的生成程序最后共用这份数据库和同一个设备管理器。
[ARTIQ 官方设备库与别名说明](https://m-labs.hk/artiq/manual/environment.html#the-device-database)
""")
md("""
`spec_db` 是 TaiyiDSL 的卡配置格式，内部通过 Spectrum 官方 `spcm` 驱动执行。
本例 A 卡启用一个输出，B 卡启用两个输出。UID `A/B` 用于接线；`serial_number` 才用于核对真实卡身份。
下面直接写出 `address`：`/dev/spcm0`、`/dev/spcm1` 是本机 PCIe 卡的驱动标识，不是 IP。序列号 `SIM_A/B` 仍是离线占位，接实机时必须替换。
官方 SDK 也接受远程资源标识，例如 `TCPIP::192.168.1.10::inst0::INSTR`；只有通过实际配置的远程服务访问时才使用它，不给本机 PCIe 卡虚构 IP。
`output_range_mv` 是模拟输出量程，不能把它与门的归一化 `pi_amplitude` 混为一谈。
[Spectrum 官方连接标识示例](https://github.com/SpectrumInstrumentation/spcm/blob/master/src/examples/README.md)
""")
code((LAB / "spec_db.py").read_text(encoding="utf-8"))
md("""
## 2. 完整 descriptor：谁连接谁

这是可直接编辑的接线定义，没有隐藏在辅助函数中：

- `instruments` 引用上述数据库，不再次填写地址和时钟。
- `channels` 是实验室命名的物理线路；`nuclear_rf` 在 A 卡输出 0，clock/rydberg 在 B 卡，`awg_trigger_a/b` 也完整列在这里。
- `sync_groups` 引用 channels 中的两路触发线，定义它们分别接 A/B 卡。旧程序直接引用 device_db 设备名仍兼容。
- `signals` 按 qubit 分组：`q0: {rf: ...}` 在内部展开为 `q0.rf`。相机等公共实验信号可直接放在同层。旧扁平写法仍兼容。

第一个 Ramsey 只用 RF，B 卡仍同步播放零输出；第二个 clock_ramsey 使用 B 卡 clock。触发 TTL 是硬件控制线路，不必再人为添加到 qubit 的 signals 中。
""")
wiring_source = (
    (LAB / "wiring.py")
    .read_text(encoding="utf-8")
    .replace(
        '"nuclear_rf": {"instrument": "A", "channel": 0},',
        '"nuclear_rf": {"instrument": "A", "channel": 0, "sharing": "global"},',
    )
)
code(wiring_source)
code("""
setup = DeviceSetup.from_descriptor(descriptor, device_db=device_db, spec_db=spec_db)
display(setup.connections())
display(setup.trigger_connections())
""")
md("""
### DeviceSetup 的实际结构与连接地址

DeviceSetup 是 Python 配置对象，不是驱动连接。它保存设备和 Spectrum 分组的编译配置，同时保留 descriptor、设备库来源及派生映射。
下面 `describe()` 展示这份对象的可读快照，包括解析后的 core host、卡 address/序列号、通道、同步组和信号；修改返回的字典不会修改 setup。
地址在设备库中只写一处，descriptor 通过设备 UID 引用它，所以 descriptor 不再复制地址。
""")
code("""
display(setup.describe())
""")
md("""
### 单个q0的Ramsey与公共RF场

本教程只定义、控制并观察q0，不构建双比特实验。nuclear_rf仍声明sharing="global"，说明它是公共核自旋驱动场，不代表独立寻址。
其他原子可能受到这个场，但不在本例中建模或传入实验函数。以后若在同一descriptor登记其它原子的RF映射，后台会按接线确定覆盖范围，无需在每次play重复targets。
""")
md("""
配置稳定后，可以把两个数据库分别保存为 `.py` 文件，再传入路径。加载器静态读取赋值和数字表达式，不执行配置代码；动态生成的设备库可显式传入已求值字典。
这一步只读取配置，没有打开驱动或连接仪器。`SignalMap` 和 Spectrum 分组由 setup 内部生成。
""")
code("""
config_dir = OUTPUT / "lab_config"
config_dir.mkdir(exist_ok=True)
for name, value in (("device_db", device_db), ("spec_db", spec_db)):
    (config_dir / f"{name}.py").write_text(name + " = " + pprint.pformat(value, sort_dicts=False) + "\\n", encoding="utf-8")
setup = DeviceSetup.from_descriptor(
    descriptor,
    device_db=config_dir / "device_db.py",
    spec_db=config_dir / "spec_db.py",
)
""")
md("""
## 3. LabConfig 与 Qubit：接线和校准各自管理

`LabConfig` 把 setup 和登记的 qubit 组合为实验室入口，`lab.compile(exp)` 使用这份配置。
先说明两者的职责，再定义 q0，随后用一行 `LabConfig(setup=setup, qubits=[q0], emulation=True)` 完成组合。
创建 lab 不连接仪器；`qubits` 也不替代接线。

### Qubit：信号与校准参数

`signals={"rf": "q0.rf"}` 中，key `rf` 是Qubit内部角色，对应q0.rf属性及parameters.rf分组；value `q0.rf` 是实验接线使用的逻辑信号名，q0.rf属性返回的就是这个字符串。
完整关系为 `rf角色 → q0.rf逻辑信号 → nuclear_rf实验室通道 → A卡/output 0`。两者相似只是“qubit名＋角色名”的命名约定，不是重复声明两个物理输出。
value可改为其他逻辑名称，但须同步修改descriptor；修改uid不会自动重写显式signals值。

`q0.rf` 是连接公共RF输出的逻辑信号；`q0.parameters.rf` 是本例选择的波形校准来源。每个驱动采用相同层级；不再把 RF 放一处、clock 放另一处。
`frequency` 是波形驱动频率，`resonance_frequency` 描述物理跃迁。对于 AOM/混频链路，两者不一定相同，不能自动替代。
π 和 π/2 的幅度、时间独立给出，不根据半幅度或半时长推测已校准。
clock 提供一套明确的 demo 值：波形频率 200 kHz、π 时长 100 μs、π/2 时长 50 μs、归一化幅度 0.2；独立的 clock_control frame 不自动传播核自旋相位。
本次 2π demo 使用同幅度的 200 μs 方波。它基于恒定 Rabi 频率下的面积关系，不是经过物理校准的保证，也不会把 200 kHz 误写成光学共振频率。
rydberg 此处仍为空，表示尚未提供门参数；调用相关门会明确报缺参。
""")
qubit_source = (LAB / "qubits.py").read_text(encoding="utf-8")
# Imports are shown once in the opening cell; keep the complete editable object.
code(qubit_source[qubit_source.index("q0 = Qubit(") :])
code("""
print("RF 信号:", q0.rf)
print("RF π/2 幅度:", q0.parameters.rf.pi2_amplitude)
print("RF π/2 时间 [s]:", q0.parameters.rf.pi2_duration)
print("clock 信号:", q0.clock)
lab = LabConfig(setup=setup, qubits=[q0], emulation=True)
""")
md("""
### 3.1 直接修改 Qubit 参数

这里的 q0 是可以编辑的实验对象，直接赋值即可，不需要 QubitStore：
`q0.parameters.rf.pi2_amplitude = 0.22`。赋值时仍检查单位和数值范围；拼错标准字段会报错。
下面演示修改和增加自定义参数，最后恢复RF幅度，保持两组演示波形仍为0.2。
""")
code("""
original_amplitude = q0.parameters.rf.pi2_amplitude
q0.parameters.rf.pi2_amplitude = 0.22
print("修改后的π/2幅度:", q0.parameters.rf.pi2_amplitude)

# 实验室自定义参数使用普通字典，不混入库已定义的驱动字段。
q0.parameters.extensions["lab"] = {"clock_detuning_hz": 500.0}
q0.parameters.extensions["lab"]["clock_detuning_hz"] = 750.0
print("新增参数:", q0.parameters.extensions["lab"])

# 此项只用于演示；后面的clock仍使用自己的frequency，不会自动加上这个值。
q0.parameters.rf.pi2_amplitude = original_amplitude
assert q0.parameters.rf.pi2_amplitude == 0.2
""")
md("""
已有的标准字段可以直接赋值，例如 `q0.parameters.clock.pi_duration = 100 * us`。
自定义字段放入 `extensions["lab"]`，使用普通JSON数据并明确单位；读取它的实验才会采用它。
新增真正的信号角色则还要配置 signals 和 descriptor，不能只增加一个参数代替接线。

“快照”就是**把当时的参数复制并保存下来**。现在只在建立Experiment、保存校准记录和编译产物时自动保留这份副本。
你继续修改q0，不会倒过来改变已经建立的exp。修改pulse保存的幅度或时长后，先重建命名pulse，再调用实验函数；只改频率则直接重新调用函数。
LabConfig负责硬件连接和接线，不再要求因为幅度或频率更新就重新创建lab。

直接赋值会更新revision和fingerprint；用户日常不必手动管理它们。QubitStore是保存校准历史/应用拟合proposal的可选进阶接口，不是赋值入口。
需要简单保存时可使用下面的JSON方式；读取结果仍可编辑，不需要保存流程才能运行实验。
""")
code("""
qubit_file = OUTPUT / "qubit_parameters.json"
qubit_file.write_text(json.dumps(q0.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
restored_q0 = Qubit.from_dict(json.loads(qubit_file.read_text(encoding="utf-8")))
assert restored_q0.to_dict() == q0.to_dict()
""")
md("""
### 3.2 Qubit → 命名pulse与函数参数 → 实验 → 编译

Ramsey的两次RF脉冲物理上都是π/2脉冲，因此在定义命名pulse时明确读取 `q0.parameters.rf.pi2_amplitude` 和 `pi2_duration`。
不另外定义一个通用 `parameters.rf.amplitude`，也不让play猜测门的含义。

```python
# 在实验上下文之外定义；这里保存的是当时的数值，不是动态引用。
pi2_pulse = Square(
    duration=q0.parameters.rf.pi2_duration,
    amplitude=q0.parameters.rf.pi2_amplitude,
)
# 实验函数中计算实际RF频率，第二次play才覆盖phase。
frequency = q0.parameters.rf.frequency * Hz + detuning
play(q0.rf, pi2_pulse, frequency=frequency)
play(q0.rf, pi2_pulse, frequency=frequency, phase=phase)
```

| 内容 | 由谁提供 | 何时固定 |
|---|---|---|
| 波形形状和时长 | 提前命名的pi2_pulse / clock_pulse | 创建pulse时 |
| RF π/2幅度、clock幅度 | 命名pulse读取pi2_amplitude / clock.pi_amplitude | 创建pulse时 |
| RF实际频率 | 函数开头显式计算Qubit频率加detuning | 建立exp时 |
| clock频率 | 后台读取Experiment保存的Qubit参数 | 编译时 |
| detuning、tau、phase | 函数参数；Ramsey只扫描tau，clock_ramsey只扫描phase | 标量在建exp时；唯一扫描量在编译各点时 |
| 实际卡、通道、触发与采样网格 | DeviceSetup / LabConfig | 编译时解析 |

```text
可编辑Qubit → 命名pulse保存时长/幅度；实验函数读取频率并加detuning
命名pulse ＋ 函数参数
      ↓ ramsey(...) / clock_ramsey(...)
Experiment保存参数副本；RF play采用pulse的时长/幅度，记录频率和相位覆盖
      ↓ lab.compile(exp)
绑定扫描值 → 排程与相位计算 → 逻辑IR → 卡/通道 → 最终DAC样本
```

RF频率在函数开头显式相加，便于直接看见detuning如何进入波形。
clock没有失谐，frequency也不用写；phase默认零，只有第二个RF脉冲需要传phase。
两次play复用**同一个pi2_pulse对象**；不需要第二个pulse或replace。
play内部记录该次调用的独立值，不改变原pulse。单次覆盖也不会回写Qubit。
若修改幅度、时长或形状，先重新创建命名pulse，再调用实验函数；只改频率则不必重建pulse。随后都要重新编译，旧exp和旧报告不会自动变化。
参数优先级是play覆盖值 → pulse中已保存的值 → Qubit默认值（仅当pulse对应字段为None）。play不会自动把π/2幅度换成Qubit的新值。
本教程只使用方波。将来替换包络时，应同时选择对应校准和构造参数，不能仅换名字就假定门已校准。
""")
md("""
### LabConfig 是把这些对象组合起来

```text
LabConfig
├─ backend：artiq_spectrum
├─ emulation：True
├─ setup：DeviceSetup（设备事实、接线、同步组；补充 Qubit frame 关联）
├─ qubits：登记的Qubit与接线；本次校准采用Experiment中保存的值
├─ signal_map：从 descriptor 派生，用户不再维护第二份映射
└─ artiq_python：真实执行时使用的独立 ARTIQ Python 环境
```

下面查看除了已展示的 setup 外其余实际字段。完整版本是 `lab.describe()`；它是可读配置快照，不是仪器已连接的证明。
旧 `lab.to_dict()` 仍用于旧编译配置格式，不能把它误当成包含 emulation、连接来源和所有 Qubit 的完整会话配置。
""")
code("""
lab_view = lab.describe()
display(lab_view)
""")
md("""
Qubit参数可以直接修改；实验构建后保留副本。本例只关心q0，以q0的参数作为RF校准和控制相位参考。
""")
md("""
## 4. 原生 ARTIQ 包：登记一次，正文调用

这里复用已有 `GreenMOTFixture` 类。库读取它的源码和静态依赖，不在 notebook 中 import 或执行 ARTIQ kernel。
`.kernel(...)` 只是创建一个“将来在生成 kernel 中调用此方法”的 DSL 代理。

准备过程分为可变前段和有明确结束边界的固定尾段；两段合成 `prepare_MOT()`。
固定尾段的返回游标会检查，不能通过暗加 delay 掩盖不符。`finalize_method` 是旧接口的每 shot 清理，不是会话结束清理。
""")
code("""
package = NativeKernelPackage.from_package(
    root=ROOT / "examples/artiq_spectrum/greenmot_native",
    entry="greenmot_fixture:GreenMOTFixture",
    resources=("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"),
    initialize_method="initialize_native",
    finalize_method="finalize_native",
)
prepare_prefix = package.kernel("prepare_MOT", timing="prefix")
prepare_tail = package.kernel(
    "finish_preparation", timing="timed", duration=15 * ms + 16 * ns,
    end_boundary="Fixture TTL falling edge; no analog preparation model",
)
readout = package.kernel("execute_readout", timing="suffix", duration=100.48 * ms)

def prepare_MOT():
    prepare_prefix()
    prepare_tail()
""")
md("""
这里直接调用 `prepare_MOT()`、`readout()`，实验正文能看见准备、控制和读出的顺序。
现有 fixture 的初始化包含 `core.reset()`，清理每 shot 执行，因此保留单包入口。
新开发的独立冷却/拍照组件使用 `NativeModule.from_package` 和 `NativeProgram`；其 initialize/finalize 每会话一次，after_shot 每 shot，不能直接换类名。
组件开发见 [原生组件指南](../native_components.md) 和 [实验室扩展示例](../../examples/lab_extension/README.md)。
保存到 `my_lab/native.py` 后，直接 `from my_lab import native` 就自然得到模块形式的 `native.prepare_MOT()`，无需另包一次。

接真实 GreenMOT 时，可以使用现有 `GreenMOTAdapter(greenmot_source=实际源文件路径)`，将其 `prepare_MOT`、`readout` 作为下面的两个回调。
适配器保留原准备前段、15 ms＋16 ns 固定尾段及每 shot 清理，并检查拆分未改变原操作顺序；不要把未知时长的整个原程序随意标为 fixed duration。
真实尾段结束点是 Zotino 的数字 LDAC 边沿，不等于模拟磁场已经稳定；`preparation_to_gate` 需要实验室确定。

其他原生类按同样方式登记 `root`、`entry`、资源和方法。若方法包含 reset/break/wait，不能直接声明成严格 timed/suffix。
原生方法放在 realtime 的准备或读出位置，不放进 AWG gate Section。基础 Ramsey 不需要 NativeProgram 或 compile_stream。
""")
md("""
### 原生读出与measure的区别

plain版仍保留prepare_MOT/readout：它们是原生kernel的DSL调用代理，不是qubit_operation。
`measure(q0)`/`acquire(...)` 需要相应读出协议、信号映射和结果适配器。当前示例只有相机TTL动作，没有图像读取，因此不把readout替换成一个看似能产生测量数据的measure，也不重复触发相机。

## 5. 操作与 Section：只用一种可复用容器

所有pulse先实例化并命名，然后把对象传入实验函数。一个RF pi2_pulse就足以表达Ramsey的前后两次公共脉冲，每次对q0进行一次π/2操作。
波形对象从Qubit读取并保存时长和幅度；RF频率由实验函数显式计算，只在第二次RF播放时额外覆盖相位。不重复创建前后两个pulse。
""")
code("""
# 修改Qubit的幅度或时长后，重新执行本单元格，再构建实验。
pi2_pulse = Square(
    duration=q0.parameters.rf.pi2_duration,
    amplitude=q0.parameters.rf.pi2_amplitude,
)
clock_pulse = Square(
    duration=2 * q0.parameters.clock.pi_duration,
    amplitude=q0.parameters.clock.pi_amplitude,
)
print("RF π/2 pulse:", pi2_pulse)
print("Clock 2π pulse:", clock_pulse)
""")
md("""
### 修改Qubit后，重新执行哪些单元格？

在已有q0上直接赋值，例如 `q0.parameters.rf.pi2_amplitude = 0.25`。
**不要为了更新参数重新运行最初创建q0的单元格**，否则会把参数恢复为示例值。
上面的pulse保存创建时的数值，不会跟随q0自动变化。

| 修改内容 | 需要重跑的单元格，按执行顺序 |
|---|---|
| RF的pi2_amplitude / pi2_duration，或clock的pi_amplitude / pi_duration | ① 本节定义pi2_pulse和clock_pulse的单元格 → ② 构建所需实验的调用单元格 → ③ 编译、报告与绘图单元格 |
| RF或clock的frequency | ① 构建实验的调用单元格 → ② 编译、报告与绘图；pulse没有保存frequency，不需重建 |
| detuning、tau、phase、shots等实验参数 | 修改对应调用参数，再执行该调用及后面的编译、报告与绘图 |

对应的位置是：第6节的 `exp = ramsey(...)` 与 `compiled = lab.compile(exp)`；
第6.1节的装饰器定义/调用/编译单元格，以及紧随其后的等价检查；
第8节的 `clock_exp = clock_ramsey(...)` 调用/编译/报告单元格。
第7节的Ramsey报告导出与绘图也要重跑，已有HTML不会自动更新。

普通函数和@experiment遵守同一条取值规则。只修改同一个q0的参数时，不需要重新定义函数、门操作或lab；
本教程装饰器定义与调用放在一个单元格，所以使用装饰器版时重跑该单元格即可。
如果要运行普通/装饰器等价检查，先更新普通版compiled，再更新compiled_decorated；不要比较一新一旧两个实验。
若将q0替换为另一个对象，则必须重新执行@experiment定义，使其绑定新对象；这与修改参数不同。

只编译旧exp不会重新读取q0或新pulse。Qubit参数、命名pulse、已构建实验和编译结果是不同阶段的对象。
本教程第3.1节的赋值演示会恢复示例幅度；实际调参时在其后单独赋值即可，不必重跑演示单元格。
""")
md("""
下面的 `global_rf_pi2` 只接收校准来源q0和命名pulse；幅度/时长已经存入pulse，RF频率由实验函数按q0计算，frame以q0为参考。受影响对象由编译器按接线推导。
显式传入计算好的frequency和第二脉冲phase；每次调用只播放一次RF。
它不创建pulse，也不改写Qubit。装饰器执行函数体、收集节点并返回Section；在活动上下文里自动插入一次。
因此它是可复用的门体，而下一节的ramsey是返回整个Experiment的实验函数，两者职责不同。
""")
code("""
@qubit_operation
def global_rf_pi2(q0, pulse, *, frequency, phase=0 * deg):
    play(q0.rf, pulse, frequency=frequency, phase=phase)

gate = global_rf_pi2(q0, pi2_pulse, frequency=q0.parameters.rf.frequency * Hz)
print("门操作返回:", type(gate).__name__)
# 在上下文外生成的gate可用exp.add(gate)复制插入；本例正文直接调用global_rf_pi2。
""")
md("""
默认串行。需要多个独立输出对齐时，用 `section(..., schedule="parallel", align="start"/"end", qubits=(...))`。
直接子操作是完整并行分支，复合分支在内部串行；同一公共输出不能作为两条独立分支。
`layer()` 只保留旧接口包装。`barrier()` 是 ASAP 中的同步点，没有子内容，也不负责右对齐；本例不需要它。

本例只写q0的时序；公共RF不能假装成多个原子的独立并行输出。
Section 与 segment 都不自动清 frame；Ramsey 的 reset_phase 在门体入口明确写出。
对于下面本来就串行的两个脉冲，额外套 `with section("ramsey")` 只是命名分组，**并非必需**，所以完整正文中省掉。
需要保存复用时，`@qubit_operation` 已经会返回 Section，不必再手写一层同名 Section。
""")
md("""
## 6. 完整 Ramsey 实验正文

先看完整正文，不隐藏准备、扫描和 gate sequence 边界。`tau` 是第一脉冲结束到第二脉冲开始的时间。
**实际RF频率 = q0.parameters.rf.frequency + detuning = 30 kHz + 1 kHz = 31 kHz。**
实验函数开头显式写 `frequency = q0.parameters.rf.frequency * Hz + detuning`，两次RF都传 `frequency=frequency`。后台直接使用这个最终频率计算载波相位和DAC样本，不再另加一次失谐。
两个脉冲都使用校准频率加 detuning；第二脉冲只额外加 second_phase。载波沿实际时间轴继续演化，不能再重复加一遍 `2π·detuning·tau`。
本版 second_phase 为 0°，tau 扫描 1、10、100 μs；保留 1 kHz detuning，所以 RF 输出为 31 kHz。
相位连续意味着第二脉冲接续同一个随时间演化的载波，而不是让它重新从第一个脉冲的起始相位开始。暗时间中实际 DAC 为零，但载波参考仍继续演化。
""")
code("""
def ramsey(q0, detuning, tau, pi2_pulse, phase=0 * deg, *,
           prepare, readout, shots=2, preparation_to_gate=10 * us,
           group="qubit_awg"):
    # 明确的真实驱动频率，两个RF脉冲共用。
    frequency = q0.parameters.rf.frequency * Hz + detuning
    # 仅扫描tau；detuning、phase等控制参数是标量。
    exp = Experiment("greenmot_ramsey", [q0], metadata={"hardware_verified": False})
    with exp.average(shots):
        with exp.sweep(tau):
            with exp.realtime():
                prepare()
                delay(preparation_to_gate)
                with exp.triggered_sequence(group=group):
                    reset_phase(q0.phase_frame("spin"))
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency)
                    delay(q0.rf, tau)
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)
                readout()
    return exp
""")
md("""
Ramsey只扫描tau：传入一个SweepParameter；detuning、phase和其它控制参数使用单个数值。
准备、读出、shots和同步组由函数参数传入。函数只构建实验，最后return exp；编译放在函数外。
""")
code("""
delays = SweepParameter("tau", [1, 10, 100], "us")
exp = ramsey(
    q0=q0, detuning=1 * kHz, tau=delays, pi2_pulse=pi2_pulse,
    phase=0 * deg, prepare=prepare_MOT, readout=readout, shots=2,
)
""")
md("""
修改幅度/时长后先重建pi2_pulse，再调用ramsey；只改频率或detuning时直接重新调用ramsey即可。不需要重建lab。旧exp保存构建时的值，不会随着后续赋值变化。
前后脉冲复用pi2_pulse；本次相位由play调用独立记录，不修改该pulse。
""")
code("""
compiled = lab.compile(exp)
print("扫描点:", len(compiled.points), "；shots/point:", compiled.shots)
display(compiled.axes)
rf_events = [e for e in compiled.points[0].logical_events if e.kind == "play"]
display([{"pulse": i + 1, "base_frequency_Hz": q0.parameters.rf.frequency,
        "detuning_Hz": e.params["frequency"] - q0.parameters.rf.frequency,
        "compiled_frequency_Hz": e.params["frequency"]}
       for i, e in enumerate(rf_events)])
assert rf_events[0].params["frequency"] == rf_events[1].params["frequency"]
""")
md("""
### 6.1 同一个Ramsey也可以用 @experiment 定义

这是已有的另一种实验构建入口。普通函数显式创建exp并return；装饰器在每次调用时创建exp，函数体只写DSL，不写return exp。
两者都不执行仪器，也不改变扫描和相位语义。@qubit_operation返回Section，@experiment返回整个Experiment。

这里 `@experiment(qubits=[q0])` 绑定校准对象q0，参数值在调用时保存；本例只观察q0，不传入其它原子。
装饰器版使用不带exp前缀的average/sweep/realtime/triggered_sequence，它们写入当前正在建立的Experiment。
此写法绑定的是这里的q0对象；更换成另一个对象时重定义装饰器，或采用上面的普通参数化函数。
""")
code("""
@experiment(qubits=[q0], name="greenmot_ramsey", metadata={"hardware_verified": False})
def ramsey_decorated(detuning, tau, pi2_pulse, phase=0 * deg, *,
                     prepare, readout, shots=2, preparation_to_gate=10 * us,
                     group="qubit_awg"):
    frequency = q0.parameters.rf.frequency * Hz + detuning
    with average(shots):
        with sweep(tau):
            with realtime():
                prepare()
                delay(preparation_to_gate)
                with triggered_sequence(group=group):
                    reset_phase(q0.phase_frame("spin"))
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency)
                    delay(q0.rf, tau)
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)
                readout()

exp_decorated = ramsey_decorated(
    detuning=1 * kHz, tau=delays, pi2_pulse=pi2_pulse, phase=0 * deg,
    prepare=prepare_MOT, readout=readout, shots=2,
)
compiled_decorated = lab.compile(exp_decorated)
print("装饰器返回:", type(exp_decorated).__name__)
print("编译记录的作用对象:", list(compiled_decorated.qubits))
""")
md("""
下面只作一次等价核对，不另外导出重复报告。检查每点的时序、π/2参数和最终DAC段hash/播放步表；源码位置可以不同。
""")
code("""
def waveform_signature(result):
    signature = []
    for point in result.points:
        timing = [(e.kind, e.signal, e.start_mu, e.duration_mu) for e in point.logical_events]
        rf = [(e.params["amplitude"], e.params["frequency"], e.params["phase"])
              for e in point.logical_events if e.kind == "play"]
        programs = []
        for event in point.physical_events:
            if event.kind == "spectrum_program":
                for card, item in event.params["cards"].items():
                    program = item["program"]
                    programs.append((card, [(s["sample_count"], s["sha256"]) for s in program["segments"]], program["steps"]))
        signature.append((timing, rf, programs))
    return signature

assert waveform_signature(compiled_decorated) == waveform_signature(compiled)
assert set(exp.qubits) == {"q0"}  # 编译不会改写原来的构建对象。
assert set(compiled.qubits) == {"q0"}
print("普通函数与@experiment的时序、相位和DAC样本一致。")
""")
md("""
## 7. 检查实际编译结果：时间、相位和最终 DAC

下面的报告源于编译产物，波形查看器解码最终 packed DAC 样本。
它不是把理想门图当成硬件输出，也不是测量数据。
注意：ARTIQ 原生 kernel 的波形没有自动模拟；报告保留其调用、时长和源码。
""")
code("""
report = compiled.export_report(OUTPUT / "notebook_report")
display(Markdown(f"[打开本次实验报告]({report['index'].resolve().as_uri()})"))
print("报告路径:", report["index"].resolve())
""")
md("""
### 32 点约束属于 segment，不属于每一个门

当前 1.25 GS/s 的采样间隔是 0.8 ns。segment 每通道长度必须是 32 的整数倍；启用一个通道时最少 384 点，两个通道时最少 192 点。
编译器先建立完整门时间轴，再切分 segment。不能为了凑 32 点在两个 Ramsey 脉冲之间加零或拉长 tau。
末尾保留零输出并补齐，**逻辑门结束**与**物理播放结束**分别显示；相机读出可使用独立资源，重新触发或上传则必须等待卡完成。
""")
code("""
point_index = 2  # tau=100 us；在 HTML 中可切换 1 / 10 / 100 us 三个点。
point = compiled.points[point_index]
group_event = next(e for e in point.physical_events if e.kind == "spectrum_program")
rows = []
for card, item in group_event.params["cards"].items():
    program = item["program"]
    rows.append({
        "card": card,
        "active_channels": item["contract"]["active_channels"],
        "segment_lengths": [s["sample_count"] for s in program["segments"]],
        "tail_samples": program["tail_samples"],
        "tail_ns": program["tail_samples"] * 0.8,
    })
    assert all(s["sample_count"] % 32 == 0 for s in program["segments"])
display(rows)
""")
md("""
### 各卡共用同一横轴

下面显示所选扫描点一个 shot 的**完整门序列**：第一个 π/2、100 μs 暗时间、第二个 π/2 以及末尾零样本。横轴相对于 gate sequence 入口，而不是每卡 sample 0；触发补偿前缀已计入。
本例 B 卡未使用，所以应为零。多卡同时有门时，也按同一公共时间轴分配，不能将各卡空闲区各自删掉。
分段数量不同不会破坏逻辑对齐。软件排程一致不等于实测模拟输出同步，仍需共同参考时钟和测得的通道延迟/相位校准。

图片直接嵌入下面的 cell 输出，也保存为 `output/simple_ramsey/ramsey_dac.png`，不依赖弹出窗口。
上面的静态图用于检查一个 shot。交互 waveform simulator 则默认按执行顺序连续展示多个 point/shot，并同时画出所有卡、所有通道；点击图例可隐藏或恢复某条输出。放大后再读取精确样本，选择单个 pulse 是可选细节视图。概览的 min/max 压缩来自最终 DAC，不是重新生成的理想 pulse。
""")
code("""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from IPython.display import Image

from taiyidsl.backends.artiq_spectrum.awg import reconstruct_window

def plot_dac_sequence(compilation, point_index, image_path, title):
    # Decode both cards against the gate entry, including all idle intervals.
    point = compilation.points[point_index]
    event = next(e for e in point.physical_events if e.kind == "spectrum_program")
    region = next(e for e in point.logical_events if e.kind == "spectrum_region")
    tick = compilation.setup.tick_seconds
    gate_start = region.start_mu * tick
    fig, ax = plt.subplots(figsize=(11, 3.8))
    for card, item in event.params["cards"].items():
        contract, program = item["contract"], item["program"]
        rate = contract["sample_rate_hz"]
        sample_zero = event.params["trigger_relative_mu"] * event.params["trigger_time_unit_s"] + contract["trigger_latency_samples"] / rate
        count = program["physical_duration_samples"]
        # These short examples have at most ~500k samples/card; no downsampling.
        dac = reconstruct_window(program, 0, count)
        t_us = (sample_zero + np.arange(count) / rate - gate_start) * 1e6
        for column, channel in enumerate(contract["active_channels"]):
            ax.plot(t_us, dac[:, column] / 32767, label=f"{card}: output {channel}", linewidth=0.9)
    pulses = [e for e in point.logical_events if e.kind == "play"]
    for index, pulse in enumerate(pulses):
        start_us = (pulse.start_mu * tick - gate_start) * 1e6
        end_us = ((pulse.start_mu + pulse.duration_mu) * tick - gate_start) * 1e6
        ax.axvspan(start_us, end_us, color="C0", alpha=0.05)
        ax.text((start_us + end_us) / 2, 0.23, f"pulse {index + 1}", ha="center")
    ax.set(xlabel="Time from gate sequence start [us]", ylabel="Normalized DAC", title=title + " — compiled samples, not measured", ylim=(-0.25, 0.29))
    ax.legend(loc="lower center", ncol=3)
    fig.tight_layout()
    fig.savefig(image_path, dpi=130)
    plt.close(fig)
    display(Image(filename=str(image_path)))

plot_dac_sequence(compiled, point_index, OUTPUT / "ramsey_dac.png", "Ramsey: RF 100 us / dark 100 us / RF 100 us")
plot_dac_sequence(clock_compiled, 0, OUTPUT / "clock_ramsey_dac.png", "Clock Ramsey: RF 100 us / clock 200 us / RF 100 us; second phase 0 deg")
""")
md("""
### spec_db 最后怎样变成官方 SDK 调用

```text
spec_db（address、serial_number、采样率、参考时钟、通道、量程）
    → DeviceSetup.from_descriptor：解析为内部 SpectrumCard 配置及连接地址
    → LabConfig.compile：离线生成波形、segment/step 和逐卡硬件契约
    → lab.connect：独立 ARTIQ worker 打开设备、查询身份与已有时钟
    → session.run：全卡预检 → 配置输出/触发 → 上传 → 全卡 ready → ARTIQ 同步触发
```

实际程序就在库中：`src/taiyidsl/runtime/worker.py` 的 `Worker.connect()` 调用 `SpcmAdapter`；
`src/taiyidsl/runtime/spectrum/awg.py` 的 `SpcmAdapter` 使用官方 `spcm.Card/Clock/Channels/Trigger/Sequence`。
`configure()` 设置启用输出、量程和触发，`upload()` 写入 segment/step。实验作者不需要再写一套 Spectrum 初始化程序。

参考时钟和采样率是特别的：普通 connect/run 检查现有状态，不在实验中暗中重锁时钟。首次建站或修改时钟使用显式的 `configure_for_calibration()`，然后测量并保存适用的时间/相位校准；不能只改 spec_db 就认为原校准仍然有效。
本教程不会调用这些硬件方法。
""")
md("""
## 8. clock_ramsey：在两次 RF 脉冲之间插入 B 卡 clock

复用前面完整展示的设备库、descriptor、Qubit、准备与读出，不再创建另一套硬件配置。
本例把暗时间替换成一次 clock 2π 脉冲：**A 卡 RF π/2 100 μs → B 卡 clock 2π 200 μs → A 卡 RF π/2 100 μs**。
三段紧接，没有额外 delay；总逻辑长度 400 μs。B 卡从公共入口的 100 μs 才开始输出，不能把自己的空白前段删掉。

前面已经实例化并命名了clock_pulse，现在只封装如何使用它。这里专门使用恒幅方波：clock π 时长为 100 μs，因此同幅度的 2π 演示脉冲为 200 μs。
200 kHz 是 DAC 载波频率，不是 Rabi 频率；2π 指旋转角，不是只播放一个载波周期。更复杂的包络/实际钟跃迁必须使用实验室验证的门体。
`qubit_operation` 返回 Section，调用时自动插入，所以正文不再套一层 Section。
""")
code("""
@qubit_operation
def clock_2pi(q, pulse):
    play(q.clock, pulse)


def clock_ramsey(q0, detuning, clock_pulse, pi2_pulse, phase, *,
                 prepare, readout, shots=2, preparation_to_gate=10 * us,
                 group="qubit_awg"):
    frequency = q0.parameters.rf.frequency * Hz + detuning
    # 仅扫描第二个RF脉冲的phase；两RF之间的间隔就是clock_pulse时长。
    exp = Experiment("clock_ramsey", [q0], metadata={
        "hardware_verified": False,
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
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency)
                    clock_2pi(q0, clock_pulse)
                    global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)
                readout()
    return exp
""")

md("""
clock_pulse的持续时间就是两次RF之间的间隔，因此函数不重复传入tau，也不插入额外delay。
这次只扫描第二个 RF 脉冲的附加相位 `[0°, 45°, 90°, 135°, 180°]`；不再扫描 tau。
RF 仍用前例的 1 kHz detuning，即输出 31 kHz。`second_phase=0°` 表示 RF 载波沿公共时间轴连续演化，不是第二脉冲重新归零。
入口分别 reset RF 的 spin 与 clock_control frame；中间不 reset。clock 使用独立的显式控制相位，不自动推断多能级相干转移后的核自旋补偿。
""")
code("""
second_phases = SweepParameter("second_phase", [0, 45, 90, 135, 180], "deg")
clock_exp = clock_ramsey(
    q0=q0, detuning=1 * kHz, clock_pulse=clock_pulse, pi2_pulse=pi2_pulse,
    phase=second_phases, prepare=prepare_MOT, readout=readout, shots=2,
)

clock_compiled = lab.compile(clock_exp)
clock_report = clock_compiled.export_report(OUTPUT / "clock_ramsey_report")
display(Markdown(f"[打开 clock_ramsey 报告]({clock_report['index'].resolve().as_uri()})"))
print("相位扫描点:", len(clock_compiled.points), "；shots/point:", clock_compiled.shots)
display(clock_compiled.axes)

""")
md("""
图中蓝色 A:output 0 是两段 RF，橙色 B:output 0 是中间 clock，B:output 1 保持零。
两个卡的 segment 边界可以不同，但 100/300 μs 的交接必须保持；末尾 segment 补零不挪动逻辑脉冲。

### 用交互 HTML 查看这两个实验

报告默认直接展示完整信息，不需要先逐个选择卡和通道：

- Pulse sheet 将 shots 按真实执行顺序沿横轴排列，同一信号保持同一泳道。悬停只看简短摘要，点击矩形再看完整参数和源码。
- Output simulator 同时叠加全部卡/通道的最终 DAC 波形，颜色和可点击图例用于辨认/显隐。横轴默认依次拼接多次执行的完整 AWG 窗口。
- 这两个实验采用 cyclic 平均：先按扫描点顺序执行第0次 shot，再按同样顺序执行第1次 shot。因此分别有6次和10次执行。
- 参数筛选、单位和精确窗口放在“高级”中；日常查看只需要拖动缩放、恢复视野及点击图例。

连续轴是**已知时间窗口的累计展示**。门内的 pulse/delay、跨卡偏移和播放尾段都保留；未知主机等待、上传及原生准备前段用边界说明，不把它们编造成确定时长。
Pulse sheet 的控制窗口与 waveform 的 AWG 窗口范围不同，均标明范围。后者不会把原生准备/读出误画成已模拟的 DAC 波形，也不会在拼接时重新计算每个 shot 的相位。

| 实验 | 选择/操作 | 应看到的结果 |
|---|---|---|
| Ramsey | 默认全部执行，查看 A/output 0 | 每次两段 100 μs RF，暗时间依次为1/10/100 μs，再重复一遍 |
| Ramsey | 点击 B 的图例显示/隐藏 | 同一横轴上的零输出 |
| clock_ramsey | 默认同时显示 A/output 0 与 B/output 0 | 每次局部0–100和300–400 μs为RF，100–300 μs为clock |
| clock_ramsey | B/output 1 图例 | 始终为零，可点图例隐藏 |
| clock_ramsey | 查看连续5个扫描点，再看重复shot | 只改变第二个RF附加相位，clock和各shot内部边界不变 |
| 两者 | 缩放至脉冲交界，再恢复全序列 | 精确样本和概览使用同一公共时间轴，不遗漏后续脉冲 |

本地 HTML 可以在 Edge 中按 Ctrl+O 选文件打开。是否完成浏览器实际操作验收，见本次交付的验证记录；静态图和 notebook 执行通过不等同于交互验收。
""")
md("""
## 9. 接实验室硬件：需要修改的只有有物理含义的配置

| 内容 | 修改位置 | 实验室需要提供什么 |
|---|---|---|
| ARTIQ 地址和实际 TTL/DDS/SPI 拓扑 | device_db | 当前设备库，与 gateware 一致 |
| 卡身份、通道、时钟、量程 | spec_db | 真实序列号、地址及硬件设置；不能继续用 SIM_A/B |
| 接线与同步组 | descriptor | 实际 TTL→卡以及逻辑信号→输出线路 |
| 量子门校准 | Qubit parameters | π/π2 幅度、时间、驱动频率、波形、frame |
| 输出时间与相位校准 | sync group 的 calibration | 测得的延迟、增益、相位及适用频率/硬件版本 |
| 准备与相机动作 | native 声明及原 ARTIQ 包 | 真实固定尾段、每 shot 清理、图像读取方式 |

修改后先重新 compile，再检查报告。`emulation=False` 不会使 compile 访问仪器；必须显式 connect/run。
下面只展示实机写法，不在 Run All 中执行：

```python
# 使用真实接线、校准和原生入口，预先定义命名pulse，再调用实验函数。
q0 = calibrated_q0
pi2_pulse = Square(
    duration=q0.parameters.rf.pi2_duration,
    amplitude=q0.parameters.rf.pi2_amplitude,
)
lab = LabConfig(
    setup=real_setup, qubits=[q0], emulation=False, artiq_python=artiq_python,
)
exp = ramsey(q0, 1 * kHz, delays, pi2_pulse, phase=0 * deg,
             prepare=real_prepare_MOT, readout=real_readout, shots=100)
compiled = lab.compile(exp)
with lab.connect(clock_session_ids=measured_clock_session_ids) as session:
    receipt = session.run(compiled)
```

`artiq_python` 是实验室独立 ARTIQ 环境的 Python 可执行文件路径，见 [工具链安装](../artiq_toolchain.md)。
clock session 标识由实验室管理，只有参考时钟连续保持、未重锁/改线时才可沿用；不能从校准文件复制一个标识来假装有效。
connect 核对身份与时钟，run 才上传、初始化和执行。缺少真实身份、校准或工具链时明确拒绝，不能静默回退 emulation。
返回的 receipt 是执行记录，不是相机图像。当前接入的读出 kernel 只负责触发；真正的探测数据须接入实验室数据读取适配器。
""")
md("""
## 10. 保存为自己的实验包

当上面的配置已经看懂，才把它们保存到独立文件，实验正文保持短小：

```text
my_lab/
    device_db.py     # ARTIQ 设备事实
    spec_db.py       # Spectrum 卡配置
    wiring.py        # 本 notebook 完整展示的 descriptor
    qubits.py        # 可编辑Qubit参数
    native.py        # 原生类只登记一次
ramsey.py            # 接收参数、返回Experiment的函数
```

将本notebook中的ramsey/clock_ramsey函数保存到自己的实验模块；后续只改调用参数即可。
仓库还保留此前的兼容分文件示例 `examples/artiq_spectrum/simple_ramsey.py` 和 `clock_ramsey.py`，对应声明在 `ramsey_lab/`，其包装接口与本notebook展示的简化函数不同。
`ramsey_lab/experiments.py` 保存 clock_2pi 与可复用的 clock_ramsey_experiment 构造函数；它们仍由普通 DSL 组成。
在项目根运行 `uv run --locked python -m examples.artiq_spectrum.simple_ramsey` 或 `uv run --locked python -m examples.artiq_spectrum.clock_ramsey`，只编译和导出报告。

需要大实验时再看自动分块和 compile_stream：前者决定 AWG 分批驻留，后者控制电脑编译工作集；两者不是导入原生包的前置条件。
旧的大型 [综合教程](hybrid_from_scratch.ipynb) 保留为进阶资料。

软件验证、真实 ARTIQ 离线编译、仪器实测是三种不同证据。本 notebook 只展示前者，且不模拟原子动力学或生成假测量数据。
""")


def introductory_route(lesson):
    """Keep configuration and experiment cells first; inspection details are optional."""
    main = list(lesson)
    appendices = []

    def extract(first, following):
        start = next(i for i, cell in enumerate(main) if cell.source.startswith(first))
        stop = next(
            i for i, cell in enumerate(main) if cell.source.startswith(following)
        )
        appendices.extend(main[start:stop])
        del main[start:stop]

    extract("### DeviceSetup 的实际结构", "### 单个q0")
    extract("配置稳定后，可以把两个数据库", "## 3. LabConfig")
    extract("### 6.1 同一个Ramsey", "## 7. 检查")
    extract("### 3.1 直接修改", "## 4. 原生")
    extract("### 32 点约束", "## 8. clock_ramsey")
    intro = nbformat.v4.new_markdown_cell(
        "## 附录：按需查看配置、等价检查与静态 DAC 图\n\n"
        "第一次阅读到这里已经完成实验定义、编译报告和实机入口说明。"
        "以下 Python 文件保存、数据遍历和绘图仅供检查；不需要复制进实验函数。"
    )
    imports = nbformat.v4.new_code_cell("import json\nimport pprint")
    return main + [intro, imports] + appendices


def plain_variant(original):
    """Keep the same lesson and experiment signatures, expand only gate wrappers."""
    result = deepcopy(original)
    for cell in result:
        source = cell.source.replace("output/simple_ramsey", "output/plain_ramsey")
        if cell.cell_type == "code":
            source = source.replace("QubitOperations, ", "").replace(
                "qubit_operation, ", ""
            )
            if "public_gate = QubitOperations" in source:
                source = source[: source.index("public_gate =")]
                source += 'print("公共场接线仅供说明；plain实验仍用单q0的setup。")'
            elif source.startswith("@qubit_operation\ndef global_rf_pi2"):
                source = (
                    'print("Plain版的实验函数直接play命名pulse，不经过门操作包装。")'
                )
            elif source.startswith("@qubit_operation\ndef clock_2pi"):
                source = source[source.index("def clock_ramsey(") :]
            source = (
                source.replace(
                    "global_rf_pi2(q0, pi2_pulse, frequency=frequency)",
                    "play(q0.rf, pi2_pulse, frequency=frequency)",
                )
                .replace(
                    "global_rf_pi2(q0, pi2_pulse, frequency=frequency, phase=phase)",
                    "play(q0.rf, pi2_pulse, frequency=frequency, phase=phase)",
                )
                .replace(
                    "clock_2pi(q0, clock_pulse)",
                    "play(q0.clock, clock_pulse)",
                )
            )
        else:
            source = source.replace(
                "## 5. 操作与 Section：只用一种可复用容器",
                "## 5. Plain DSL：先命名pulse，再传入实验函数",
            )
            if source.startswith("下面的 `global_rf_pi2`"):
                source = "本plain版省去qubit_operation门包装。后面的ramsey/clock_ramsey函数直接play同一个命名pulse，采用pulse保存的时长/幅度，只覆盖基础频率加detuning的结果与第二脉冲相位。函数返回Experiment，可以反复用不同参数调用。"
            source = source.replace(
                "此时使用 `ops.global_x90(q0, q1, calibration=q0)`：",
                "操作库版可用 `ops.global_x90(q0, q1, calibration=q0)`：",
            )
            source = source.replace(
                "`qubit_operation` 返回 Section，调用时自动插入，所以正文不再套一层 Section。",
                "下面的函数直接使用play；无需门包装或额外Section。",
            )
        cell.source = source
    result[0].source = (
        result[0]
        .source.replace(
            "# Ramsey 与 clock_ramsey：", "# Plain DSL · Ramsey 与 clock_ramsey：", 1
        )
        .replace("`ramsey_clock_walkthrough.ipynb`", "`ramsey_clock_plain.ipynb`")
        .replace("操作版入口", "Plain 版入口")
        .replace(
            "第一次使用可先读 [plain版](ramsey_clock_plain.ipynb)",
            "可对照 [操作版](ramsey_clock_walkthrough.ipynb) 学习复用",
        )
    )
    result[
        0
    ].source += "\n\n本版与[操作版](ramsey_clock_walkthrough.ipynb)使用相同设备、参数、原生准备/读出、函数接口和可视化，实验正文展开为原始DSL。输出单独保存在output/plain_ramsey。"
    return result


for stem, lesson in (
    ("ramsey_clock_walkthrough", introductory_route(cells)),
    ("ramsey_clock_plain", introductory_route(plain_variant(cells))),
):
    for index, cell in enumerate(lesson):
        cell.id = f"ramsey-lesson-{index:03d}"
    notebook = nbformat.v4.new_notebook(cells=lesson)
    notebook.metadata.kernelspec = {
        "display_name": "Python 3 (project)",
        "language": "python",
        "name": "python3",
    }
    notebook.metadata.language_info = {"name": "python", "version": "3.12"}
    nbformat.validate(notebook)
    destination = ROOT / f"docs/tutorials/{stem}.ipynb"
    nbformat.write(notebook, destination)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "I,F401",
            "--fix",
            str(destination),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", str(destination)], check=True
    )
    print(destination)
