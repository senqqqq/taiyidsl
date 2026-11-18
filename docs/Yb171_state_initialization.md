# ¹⁷¹Yb 核自旋基态制备：物理方案与 taiyidsl 编程

更新：2026-10-30。本文整理初始化实验的设计与现有离线示例，项目现名为 `taiyidsl`。

本文的“可运行”指能够构建实验、离线编译、生成时序图并导出 ARTIQ 源码。示例中的频率、幅值和时间是演示参数，不是已完成标定的实验室配方；真实 ARTIQ 编译和实机输出仍未验证。

## 1. 从冷原子到指定核自旋态

选定计算基为

\[
|0\rangle=|{}^1S_0,F=1/2,m_F=+1/2\rangle,\qquad
|1\rangle=|{}^1S_0,F=1/2,m_F=-1/2\rangle.
\]

目标是将原子制备到 \(|0\rangle\)。一个可行方案是使用 556 nm 光，经 \({}^3P_1,F'=1/2\) 支路进行 σ⁺ 光抽运：

\[
|1\rangle\xrightarrow{\sigma^+}
|{}^3P_1,F'=1/2,m_F'=+1/2\rangle
\xrightarrow{\mathrm{spontaneous\ emission}}
|0\rangle\ \text{或}\ |1\rangle.
\]

反复散射使粒子积累到目标态。在理想偏振和这条选定支路下，目标态没有可由 σ⁺ 激发的 \(m_F'=+3/2\) 能级，成为暗态。σ⁺ 的定义相对于偏置磁场给出的量子化轴；光传播方向与偏振必须一并考虑。其他超精细支路的离共振激发、偏振杂质和场方向误差会使实际暗态不完美。该初始化方案及自旋选择性移除读出已用于 ¹⁷¹Yb tweezer 核自旋实验。[Jenkins 等，Phys. Rev. X 12, 021027（2022），附录 D](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

如果 Raman sideband cooling（RSC）已经完成，需先检查其最后一步：RSC 通常包含光抽运，冷却结束时可能已经具有明显的自旋极化。额外初始化扫描应保留零抽运时间点，测量“新增步骤”带来的改善，不能预设初始态一定为等概率混合态。该论文的 RSC 就包含核自旋光抽运。[同文，第 VII 节](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

## 2. 一个 shot 的边界

每次采样按以下顺序组织：

```text
提供已装载、已冷却的原子
→ 确保阱、偏置场和光路达到本次制备条件
→ 光抽运 t_pump
→ 等待激发态衰减及光开关稳定
→ 自旋选择性检测或存活率对照
→ 成像与数据传输
```

这里“已完成冷却”意味着具备可重复调用的冷却流程。若 push-out 或成像是破坏性的，每个 shot 都要重新提供合适的原子；不能只在整个扫描开始时冷却一次。

当前示例把装载、冷却和稳定场条件作为每个 shot 的前置条件。在生成代码的 host `result_adapter.before_shot(point, shot)` 中可以接入实验室已有流程，等待它确实完成，再启动初始化 kernel。`after_shot` 负责获取图像并记录装载、丢失、失败等状态。这些实验室接口尚未由示例实现。

host 边界不保证固定等待时间。若 RSC 结束到初始化开始的延迟必须确定，应把 RSC 与初始化展开到同一个实时区。软件默认不会把“等待一段时间”解释为“外部冷却已经成功”。

## 3. 统一的 lab、参数表和 experiment 接口

完整离线例子在 [examples/state_initialization.py](../examples/state_initialization.py)。它保留原有“push-out／对照 + 相机触发”的读出行为，不新增成像照明。

```python
from taiyidsl import LabConfig, OpticsConfig, Qubit
from examples.state_initialization import build_initialization_scan

q0 = Qubit("q0")
lab = LabConfig.load("examples/config/labs/initialization.json", qubit_id=q0.uid)
parameters = OpticsConfig.load("examples/config/optics/initialization.json")
exp = build_initialization_scan(
    q0,
    parameters,
    durations_us=(0, 10, 20, 50, 100, 200),
    shots=100,
    averaging="cyclic",
)
compiled = lab.compile(exp)
```

lab 描述设备和信号映射，OpticsConfig 保存共享工作点，exp 描述本次 sweep、平均和操作顺序。构建实验不操作仪器，也不根据实验类型重新分配硬件。builder 从参数快照读取：

```python
p = exp.parameters
with exp.average(shots=shots, mode=averaging):
    with exp.sweep(pump_time):
        with exp.sweep(readout_mode):
            with exp.realtime():
                optical_pump(exp, p["pump"], duration=pump_time)
                for mode in (0, 1):
                    with exp.case(readout_mode, mode):
                        trigger_readout(exp, q, p, pushout=bool(mode))
```

`case` 是扫描点的静态选择，不是测量反馈。无 push-out 条件保留等长 idle；两个条件的相机触发时间一致。逻辑信号 `optics.pump_556` 可以代表全阵列共享光束，无需伪装成 qubit Raman gate。

## 4. 参数、接线和快照的归属

| 内容 | 权威来源 | 作用 |
|---|---|---|
| Qubit 基态定义、局部参数 | `Qubit`／`QubitStore` | 实验创建时独立快照 |
| 设备、时钟、总线、映射、触发序列 | [initialization lab profile](../examples/config/labs/initialization.json) | `LabConfig.load()` 加载 |
| 抽运、push-out、读出等待参数 | [initialization optics](../examples/config/optics/initialization.json) | 平直的 pump、pushout 和 camera 参数组 |
| 本次实验参数 | `exp.parameters` | `Experiment(parameters=...)` 复制并冻结 |
| 编译所用输入 | `CompiledExperiment` | 保存参数和 lab 快照及内容摘要 |

初始化参数表只需 `pump`、`pushout` 和 `camera`，无需提供 RSC 参数。读出方式由代码中的 `trigger_readout()` 选择。RSC 抽运不必与初始化采用相同失谐或功率；不同物理条件应保存为明确的工作点。

`frequency_hz` 是 RF 控制频率，不是 556 nm 光学频率。需要按激光载频、EOM、AOM 阶次与单双通标定原子处的失谐。归一化 `amplitude` 也不是 mW 或 Rabi frequency。

`metadata` 保存说明、标签和实验方案；操作读取 `exp.parameters`。输入可以是 `OpticsConfig` 或相同结构的普通映射，创建实验时统一复制并冻结。`OpticsConfig.from_dict(data)` 与 `to_dict()` 负责校验和普通字典往返，没有强制构造 BeamConfig 或 PulseRecipe。

单次调整某个参数直接用关键字，例如在 realtime 区内：

```python
from taiyidsl import us
from taiyidsl.cooling import optical_pump

optical_pump(exp, p["pump"], amplitude=0.12)
optical_pump(exp, p["pump"], duration=50 * us, amplitude=0.08)
optical_pump(exp, p["pump"])  # 仍采用快照中的默认值
```

只覆盖本次调用的给定字段，不改变参数表；显式零值同样有效。单位和范围继续检查，DDS 准备窗口不足仍报错。每次调用的生效值保存在操作 IR 中，扫描绑定后的值保存在 compiled events 中。

参数表采用 schema 2，带版本标签，但不是自动校准数据库。新参数用于新建实验，拟合不自动写回。架构见 [参数、操作与 experiment](lab_recipes_experiments.md)；RSC 见 [Raman sideband cooling](Yb171_raman_sideband_cooling.md)。

## 5. 自旋读出与存活率对照

普通原子存在性成像不能直接给出两个核自旋态的比例。本例选择性移除目标态 \(|0\rangle\)，随后测量剩余原子：移除光使用 \({}^3P_1,F'=3/2,m_F'=+3/2\) 支路。读出必须另行标定选择性、偏置场条件和阱条件；这里没有照搬文献中的磁场、功率或时间数值。[读出物理方案参考：同文图 10](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=933949)

完整例子包含第二个 sweep 轴：

| `readout_mode` | 操作 | 作用 |
|---|---|---|
| `0` | 用等长 delay 替代 push-out，再成像 | 测量同一抽运条件下的存活率 |
| `1` | push-out 后成像 | 测量未被移除的自旋分量 |

`exp.case()` 根据静态扫描坐标展开对应分支，不是测量反馈。两个模式的 push-out 窗口长度相同，避免将额外等待损失直接混入对比。`measure(q, protocol="image", handle="survivors")` 在两个模式中使用相同 handle，结果由扫描坐标区分。

记相同装载筛选下、相同抽运时间的存活概率为 \(S_\mathrm{reference}\) 和 \(S_\mathrm{pushout}\)。在目标态完全移除、非目标态不受影响、两组其余损失和成像响应可比的近似下，

\[
P_0\bigm|_\mathrm{surviving}\approx
1-\frac{S_\mathrm{pushout}}{S_\mathrm{reference}}.
\]

分母必须非零且有足够有效样本。这个量是存活原子中的条件态比例，不是每个装载尝试都获得目标态的无条件成功率。后者还依赖装载和存活。

实际读出需要纳入移除不完全、非目标态误损失、成像阈值误判以及各条件有效样本数。缺失或失败图像不能填零计入统计。“没有看到原子”既可能是目标态被成功移除，也可能是先前丢失或读出失败。未经读出标定，实验测到的是制备与读出的联合表现。

## 6. 建议的调试顺序

1. 确认量子化轴、偏振、光学寻址关系与原子处共振；记录阱和磁场工作点。
2. 验证成像和 push-out 的选择性，建立参考数据与状态标签。
3. 在固定功率下扫描抽运时间，保留零点，观察目标态比例的增长或饱和。
4. 同时检查无 push-out 的存活率；再扫描功率和失谐，寻找制备效率与损失之间的合适工作点。
5. 用独立的运动诊断检查附加抽运是否损害冷却效果。不能只依据自旋极化改善就认定最终原子状态更好。

拟合或优化得到的设置应生成可审阅的校准建议，记录来源实验、读出处理和适用条件。现有初始化示例没有实现专用初始化保真度拟合器，也不会自动覆盖持久化配置。

## 7. 运行、产物与验证边界

在 `taiyidsl` 项目根目录运行：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
uv run --locked python examples/state_initialization.py
uv run --locked python examples/verify_optics.py --experiment initialization
```

生成位置为 `output/state_initialization/`：

- `compiled.json`：包含参数坐标、逻辑与物理时序以及配置快照。
- `timeline.html`：离线逻辑／物理时序图。
- `artiq/experiment.py`：默认 host 循环与参数化 kernel。
- `artiq/manifest.json`：设备要求及导出检查信息。
- `verification.json`：由独立 `verify_optics.py` 脚本生成，检查默认与展开模式的 mock 执行等价性；实验定义本身只负责构建、编译和导出。

默认扫描为 6 个抽运时间乘以 2 个读出条件，共 12 点。编译器可以将这些点组织为共享参数化 kernel；零点和不执行 push-out 的条件通过可选事件处理，实际分组以生成报告为准。

需要分别看待三种验证：

| 验证 | 本文对应状态 |
|---|---|
| DSL 构建、离线排程和 mock 导出一致性 | 由示例及 `verification.json` 检查 |
| 真实 ARTIQ 编译 | 未验证 |
| 实机 RTIO、DDS 相位、光学输出及制备性能 | 未验证 |

时序图表示逻辑信号和数字设备输出模型。未进行光路延迟标定时，不能将数字边沿对齐解释为原子处光场已经对齐。

当前相机协议只产生 TTL 触发，成像照明、曝光过程、数据传输，以及需要的场和阱变化仍需实验室适配。新增协议也必须展开为可检查的操作，并明确这些条件，不能用一个函数名掩盖尚未实现的物理流程。
