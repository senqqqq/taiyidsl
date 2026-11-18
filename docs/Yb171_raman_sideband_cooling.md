# ¹⁷¹Yb Raman sideband cooling：共享光学配置、协议与完整 DSL 序列

更新：2026-10-30。项目：`taiyidsl`。本文给出脉冲式三轴 RSC 的可运行离线实现，并区分“时序完整”和“实验室物理条件已具备”。前置读物：[核自旋初始化](Yb171_state_initialization.md)。

## 1. 当前能够完整表达什么

本次实现覆盖：每 shot 的初始自旋抽运 → 多轮、逐轴 Raman 红边带脉冲 → 每次脉冲后的耗散抽运复位 → 可选边带谱探测 → 选择性移除／存活率对照 → 成像光与冷却光、相机触发 → host 获取数据。

这些步骤均展开成普通 DSL 的 `serial`、`parallel`、`repeat`、`play`、`delay`、`measure` 节点，进入同一个实时 shot。函数不会运行仪器代码，也没有隐藏的 Python 回调。

这不是从空真空腔开始的完整实验室控制程序。每 shot 开始时，host `before_shot` 仍须提供装载、预冷却后的原子，以及稳定的阱、偏置场、光学锁定、偏振和通路。例子没有伪造 MOT 装载、磁场 ramp 或阱深 ramp。若这些操作已经具备受支持的硬件适配且需要确定性衔接，应把它们也纳入同一个实时 shot。

## 2. 物理循环和频率约定

沿用 \(|0\rangle=|{}^1S_0,m_F=+1/2\rangle\)、\(|1\rangle=|{}^1S_0,m_F=-1/2\rangle\)。每个方向的理想冷却循环为：

\[
|0,n\rangle\xrightarrow{\text{Raman red sideband}}|1,n-1\rangle
\xrightarrow{\text{optical pumping}}|0,n-1\rangle.
\]

后一箭头表示冷却原理中的近似：真实自发散射有反冲，可能改变运动量子数。对 \(n=0\) 没有更低的一阶红边带，因此理想情况下不再被该边带驱动。三维冷却要求各 Raman 光束对的有效波矢差对相应运动方向有非零投影。¹⁷¹Yb 的实验实现包含 Raman 转移和光抽运，且光抽运的失谐与损失／加热有关，不能把基态初始化的工作点直接当作最优 RSC 参数。[Jenkins 等，PRX 12, 021027，第 VII 节及图 5](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

明确约定吸收 A 光子、受激发射 B 光子，内部态变化为 \(|0\rangle\to|1\rangle\)。则理想一阶红边带满足

\[
\omega_A-\omega_B=(E_1-E_0)/\hbar-\omega_{\mathrm{trap},i}.
\]

这里是**原子处的光学角频率差**；实际共振还需包含光移、场和阱条件。程序配置中的 `frequency_hz` 是控制信号的 RF 频率（Hz），不是光频。激光载频、EOM、AOM 阶次及单双通共同决定它与上式的关系。改变 B 路 DDS 频率后，光学差频是增大还是减小必须由实际光路确定。

示例 `raman` 参数按 x/y/z 轴组织；代码不会仅凭轴名或用途判断给定 RF 数值已满足红边带共振。所有示例参数均为离线占位值。

## 3. optics 参数现在放在哪里

两个光学示例统一采用同一套加载和快照机制：

| 内容 | 位置 | 用途 |
|---|---|---|
| 设备、资源、接线和触发 | [labs/rsc.json](../examples/config/labs/rsc.json) | `LabConfig.load()` |
| 共享光学参数 | [optics/rsc.json](../examples/config/optics/rsc.json) | `OpticsConfig.load()` |
| 参数校验和字典序列化 | [optics.py](../src/taiyidsl/config/optics.py) | 轻量 schema 2 参数表 |
| 组合操作 | [cooling.py](../src/taiyidsl/experiments/cooling.py) | Python 函数展开为 DSL |
| 实验 builder | [raman_sideband_cooling.py](../examples/raman_sideband_cooling.py) | 轴顺序、循环次数、读出方式、扫描和平均 |

```python
from taiyidsl import LabConfig, OpticsConfig

lab = LabConfig.load("examples/config/labs/rsc.json", qubit_id="q0")
parameters = OpticsConfig.load("examples/config/optics/rsc.json")
data = parameters.to_dict()
parameters = OpticsConfig.from_dict(data)
```

`pump`、`pushout` 是平直的脉冲工作点；`beams` 保存各束频率、幅值和逻辑信号；`raman["x"]` 保存对 `beams` 的 a/b 名称引用及默认脉宽；`imaging` 和 `camera` 保存曝光和等待数值。公共 Raman 光束、OP 参数各保存一次。没有 `kind/parameters` 类型包装，也没有 `stages/rounds/cycles` 执行方案。

Qubit 保存局部量子比特参数；lab 保存拓扑和设备／路径标定；OpticsConfig 保存共享工作点与显式时间窗口。`Experiment(parameters=...)` 从 OpticsConfig 或普通映射生成只读快照，builder 从 `exp.parameters` 读取。`conditions` 只是前置条件声明，不会控制磁场或查询锁定状态。

数字字段采用 SI：Hz、s、rad，幅值 0–1。模型拒绝非法数值、未知键及不支持的 schema。工作点可更新后用于新实验，不会改变旧快照。旧版 RecipeSet 和聚合式 OpticsConfig 需迁移到当前 schema 2，不能只换版本号。更多说明见 [参数、操作与实验](lab_recipes_experiments.md)。

## 4. 可复用操作与单次参数覆盖

这些函数只展开确定性的 DSL 节点，不执行仪器调用：

| 操作 | 展开内容 |
|---|---|
| `optical_pump(exp, p["pump"], ...)` | 显式设置窗口 → OP pulse → 显式等待 |
| `sideband_cool(exp, p, axes=..., cycles_per_axis=..., rounds=...)` | 初始 OP → 按调用给定的顺序和次数执行双束 Raman + OP |
| `illuminated_readout(exp, q, p, illumination=..., pushout=...)` | push-out 或等长 idle → 等待 → 并行照明与相机触发 → 等待 |

另有 `raman_pulse()`，确保两个逻辑光束同时开始、同时结束；它可用于冷却，也可用于频率扫描探测。初始化例子明确调用 `trigger_readout()`，保留其原来的相机触发式读出。

每次操作产生带 `operation_name` 和 `effective_parameters` 的 serial IR 区块。基准参数保存在实验快照，实际覆盖值保存在操作 IR，绑定后的值保存在每点 pulse 中。触发规格仍为 `TriggerSequence/TriggerStep`，它们与参数表独立。

例如只调整这一次 OP 的幅值或时长：

```python
from taiyidsl import us
from taiyidsl.cooling import optical_pump

optical_pump(exp, p["pump"], amplitude=0.12)
optical_pump(exp, p["pump"], duration=50 * us, amplitude=0.08)
```

其余字段继续使用工作点值，原配置和快照均不修改，显式 `amplitude=0` 同样有效。DDS 参数改变仍需满足配置时间约束，函数不会隐式加 gap。

## 5. 最小完整实时序列

以下代码从项目根目录运行：

```python
from taiyidsl import Experiment, LabConfig, OpticsConfig, Qubit
from taiyidsl.cooling import illuminated_readout, sideband_cool

q0 = Qubit("q0")
lab = LabConfig.load("examples/config/labs/rsc.json", qubit_id=q0.uid)
parameters = OpticsConfig.load("examples/config/optics/rsc.json")
exp = Experiment("rsc_single_sequence", qubits=[q0], parameters=parameters)
q = exp.qubits["q0"]
p = exp.parameters

with exp.average(shots=100, mode="cyclic"):
    with exp.realtime():
        sideband_cool(exp, p, axes=("x", "y", "z"), cycles_per_axis=2, rounds=2)
        illuminated_readout(
            exp, q, p, illumination=("image", "image_cooling"), pushout=True
        )

compiled = lab.compile(exp)
compiled.save("output/rsc_single_sequence/compiled.json")
compiled.visualize("output/rsc_single_sequence/timeline.html")
compiled.export_artiq("output/rsc_single_sequence/artiq")
```

`sideband_cool()` 的关键循环相当于下面的 Python 结构；真实 helper 还做参数检查并处理零轮数：

```python
optical_pump(exp, p["pump"])
with exp.repeat(rounds):
    for axis in axes:
        setting = p["raman"][axis]
        pair = {role: p["beams"][setting[role]] for role in ("a", "b")}
        with exp.repeat(cycles_per_axis):
            raman_pulse(
                exp,
                pair,
                duration_s=setting["duration_s"],
                setup_wait_s=setting["setup_wait_s"],
            )
            optical_pump(exp, p["pump"])
```

`rounds=0` 保留初始自旋抽运，跳过冷却循环，作为控制组。默认顺序 x、y、z，每轴两次。顺序和次数属于函数调用，不存进共享光学参数。需要 coarse/fine cooling 时，可在 Python 中组合不同工作点和循环，而不新增配置驱动的流程语言。

每个 Raman pair 内部是 `parallel(align="start")`；OP 在并行块之后，等两个光束都结束才开始。`setup_wait_s`、`settle_s` 是明确声明的窗口，编译器检查配置动作是否放得下，不会偷偷插入 gap。

## 6. 双光束、共享总线与时间参考

参考接线使用两块四通道 Urukul，共八路 DDS，分别表示 OP、Raman common、x/y/z 支路、push-out、399 nm imaging 和 556 nm imaging cooling，另有一个相机 TTL。它只是可解释的离线映射，不要求实验室必须采用这种硬件数量。

初始化 DDS 寄存器需要时间；共享 SPI 上的配置动作不能重叠。编译器可以在合法窗口为不同通道预配置，但不能在同一通道正输出时改写成不同的 pulse 参数来伪装无间隙更新。参考配置显式提供准备窗口；同一路无间隙切换不兼容参数会报错。

两束 Raman 的数字开关对齐不等于原子处光场对齐。还需确认光路延迟、DDS 同步、RF／光学差频稳定性及实际偏振几何。JSON 的 `conditions` 记录前置要求，不是控制磁场或读取锁定状态的指令。

## 7. 调试实验一：轮数扫描

`build_cooling_scan` 生成：

- `rounds = [0, 1, 3]`；
- `readout_mode = [0, 1]`，分别为无 push-out 存活对照和移除目标自旋态；
- 默认每点 20 shots，cyclic 遍历，共 6 个逻辑点。

轮数直接通过参数化 `repeat` 绑定，不再为每个轮数建立 `for + case` 分支：

```python
count = SweepParameter("rounds", (0, 1, 3), unit="dimensionless")
readout = SweepParameter("readout_mode", (0, 1), unit="dimensionless")
with exp.average(shots=20, mode="cyclic"):
    with exp.sweep(count) as cnt:
        with exp.sweep(readout):
            with exp.realtime():
                sideband_cool(exp, p, rounds=cnt, cycles_per_axis=2)
                for mode in (0, 1):
                    with exp.case(readout, mode):
                        illuminated_readout(exp, q, p, pushout=bool(mode))
```

`cnt is count`：它是参数引用，不是构建时已经取出的普通整数。Python 只执行一次 `with` 内代码，建立包含 `repeat(cnt)` 的 IR；编译器随后在三个扫描点分别绑定为 0、1、3。直接写 `rounds=count` 完全等价。repeat 接受非负整数或无量纲整数扫描，零次循环不产生事件，也不改变 frame。`range(cnt)` 和 `if cnt` 会清楚报错；不支持一般 Python 运算或实时反馈。每点事件预算仍生效。

读出模式保留 `case`，因为它选择“实际 push-out”或“等长 idle”两种不同操作，而不是改变循环次数。未声明、定义冲突的 repeat 参数会报错，即使它位于零次循环内部。

两个读出条件使用相同的 push-out 时间窗口，成像光和触发起点一致。轮数不同的 shot 总长度有意不同；若要区分冷却损失和单纯储存寿命，应另做总时间匹配的对照，不能直接把所有差异都归因于冷却。

这个实验用于检查存活率、最终自旋极化及随轮数的变化，**不能单独证明运动基态制备**。

## 8. 调试实验二：冷却后的边带谱

`build_thermometry_scan` 默认以 `cooling_rounds=2`、每轴两次执行 RSC，然后加入较短的 Raman probe，扫描其中一路的 RF 频率，最后读取自旋翻转。示例默认 x 方向，提供六个占位频率和两个读出条件，共 12 点。y/z 可用 `axis` 参数选择，但各方向必须替换为相应实测扫描范围。

关键次序是：

```text
RSC（最后含 OP，自旋回到 |0>）
→ 双光束 sideband probe
→ push-out / 等时对照
→ 并行 imaging + imaging cooling + camera trigger
```

**probe 后不再执行 optical_pump**，否则会把探测引起的自旋转移擦除。示例不自动声称这 5 µs 占位脉冲已经满足弱激发条件，也没有从占位 RF 数值推断哪边是红／蓝边带。

在热分布、边带可分辨、相同有效探测耦合／弱激发且背景与读出已校正等适用条件下，可由一阶边带强度比估计

\[
r=A_{\mathrm{red}}/A_{\mathrm{blue}},\qquad \bar n=r/(1-r).
\]

强探测导致饱和、非热分布、脉冲动力学、阱的非谐性或不同检测条件时，不能直接套这个比值，应拟合相应模型；也不能把一个任意频点的 raw camera 值当作边带强度。单原子 tweezer 中的 RSC 和边带谱诊断参见 [Kaufman 等，PRX 2, 041014（2012）](https://doi.org/10.1103/PhysRevX.2.041014)，比值适用讨论见 [Kaufman 博士论文，式 3.20 附近](https://jila.colorado.edu/sites/default/files/2019-03/Kaufman_thesis_forGradSub_AMK_150602.pdf)。

当前提供的是采集序列，没有新增温度拟合器或虚构动力学数据。实际图像需要保留每 site、每 shot 的装载／丢失标签以及有效性，再完成背景与读出修正。

## 9. 成像协议的边界

本例明确调用 `illuminated_readout()`，输出两路成像照明并与 trigger 放在曝光区块内。初始化例子调用 `trigger_readout()`，保持只有 camera trigger 的原有读出。这里曝光区块的 exact duration 等于 `exposure_s`；若触发序列长度超出曝光时间，编译失败。

这里约定相机已经配置好曝光时长和触发模式。TTL 宽度不自动设置相机曝光；实际触发延迟必须校准。`transfer_wait_s` 仅表示声明的固定等待，不替代真实数据就绪检查；host `after_shot` 必须等待或正确标记缺失数据。本例也不生成 push-out 所需的磁场／阱变化，只有在固定条件足以支持该读出时才能直接使用，否则必须补充适配后的步骤。

## 10. 运行与验收

```powershell
$env:PYTHONIOENCODING = 'utf-8'
uv run --locked python examples/raman_sideband_cooling.py
uv run --locked pytest tests/test_cooling.py --basetemp=.test-tmp-cooling
```

生成目录：`output/rsc_round_scan/` 和 `output/rsc_thermometry_x/`，各包含 compiled JSON、离线 HTML 和 ARTIQ Python。另运行 `uv run python examples/verify_optics.py` 生成各目录的 `verification.json`，逐点检查默认和展开导出的 mock 事件；验证代码不再混入实验定义。默认导出保留 host average/sweep 遍历，兼容点共用参数化 kernel；当前 shot 内 repeat 仍在绑定后展开，所以增加大量冷却循环会增大单个 kernel，尚未实现 DMA 或实时循环压缩。

新增测试覆盖：配置校验及快照、三轴脉冲计数、双束同步、每次 Raman 后的 OP 复位、等长读出对照、无 probe 后再抽运、相机曝光约束、同一路无准备窗口的参数更新失败，以及两种 exporter 的逐点实际 mock 事件等价性。

离线检查不验证实际冷却率、\(\bar n\)、DDS 相位相干、RTIO 吞吐或真实 ARTIQ 编译。本机不连接仪器。完成实机验证后再将光学参数标为实验室标定，并记录它们适用的阱、偏置场、光路和分析版本。
