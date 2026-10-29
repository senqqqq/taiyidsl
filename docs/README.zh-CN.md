# taiyidsl — 中性原子 pulse-level 实验框架

面向研究人员的 Python DSL：定义 qubit pulse、时序区块、扫描和重复采样，离线编译为可检查的执行计划，生成交互时序图及 ARTIQ 9 / AD9910 / TTL 参考代码。

**当前状态：离线首版。** 没有连接仪器。真实 ARTIQ 编译、RTIO 吞吐、板卡同步及原子处光场时序尚未验证；导出报告分别记录这些状态。参考 setup 的准备/读出协议是示例 TTL 触发，不能代替实验室的加载、光抽运和探测配置。

## 安装与运行

项目使用 uv 管理的 CPython 3.12.15，解释器固定在 `.python-version`，依赖固定在 `uv.lock`。以下命令从项目根目录运行。在 PowerShell 中：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
uv sync --locked
uv run --locked python examples/tuneup_workflow.py
uv run --locked python examples/state_initialization.py
uv run --locked pytest --basetemp=.test-tmp-pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build --no-sources
```

`examples/tuneup_workflow.py` 完成参数加载、Rabi 编译与合成数据、拟合建议、显式应用校准、新版本 Ramsey、RB、可视化与 ARTIQ 导出。全部输出位于 `output/tuneup_workflow/`，包含 `verification_report.json`，以及各实验的 `compiled.json`、`timeline.html`、结果文件、默认参数化导出的 `artiq/` 和调试展开模式的 `artiq_unrolled/`。每个实验的 `export_comparison.json` 记录两种源码的行数、kernel 数量和逐点事件等价性检查。重复运行此演示会重新生成该输出目录内的演示参数和结果。

## 最短示例

```python
from taiyidsl import Qubit, rabi, reference_setup, compile_experiment, us

q0 = Qubit("q0")
setup, mapping = reference_setup([q0])
exp = rabi(q0, [i * us for i in range(41)], shots=100, mode="cyclic")
compiled = compile_experiment(exp, setup, mapping)

compiled.visualize("output/rabi.html")
export = compiled.export_artiq("output/rabi_artiq")
print(export["verification"])  # 独立 mock 检查，不是真实 ARTIQ 编译
```

导出的 HTML 自带 Plotly，不需要 CDN 或网络；支持逻辑/物理视图、扫描点与 shot 选择、缩放、来源信息和事件表。shot 选择用于查看索引和执行次序，不意味着固定 shot 周期。

## 实验 DSL

```python
from taiyidsl import (
    Experiment,
    SweepParameter,
    Square,
    prepare,
    play,
    delay,
    measure,
    reset_frame,
    us,
)

exp = Experiment("ramsey", qubits=[q0])
q = exp.qubits["q0"]  # 实验构建时冻结的 Qubit 快照
tau = SweepParameter("tau", [0, 4, 8, 12], unit="us")

with exp.average(shots=100, mode="sequential"):
    with exp.sweep(tau):
        prepare(q, protocol="standard")
        with exp.realtime():
            reset_frame(q.frame)
            pulse = Square(
                q.params.raman.pi2_duration, amplitude=q.params.raman.amplitude
            )
            play(q.raman, pulse)
            delay(q.raman, tau)
            play(q.raman, pulse)
            measure(q, protocol="state_readout", handle="population")
```

裸时间值使用秒、频率值使用 Hz、相位值使用 rad。`ns/us/ms/s`、`Hz/kHz/MHz`、`rad/turn` 支持带量纲构建并检查维度。幅值是归一化全量程值 0..1，不等同于光功率或 Rabi 频率。

`SweepParameter.unit` 用来解释裸扫描值；内部坐标统一保存 SI 数值，结果轴同时记录输入单位和 `values_unit`。带单位的扫描值不会重复换算。

### 两种平均模式

| `mode` | A、B、C 各两次的顺序 |
|---|---|
| `cyclic`（默认） | A₀ B₀ C₀ A₁ B₁ C₁ |
| `sequential` | A₀ A₁ B₀ B₁ C₀ C₁ |

模式只改变执行顺序；两者都保存 `[point, shot, qubit]` 原始数据。均值由结果分析独立计算。嵌套 sweep 是笛卡尔积，最内层轴变化最快；cyclic 每轮遍历整个笛卡尔积。

每个实验一个顶层 `average`，其内是单条嵌套 sweep 链，之后是 shot 程序。`repeat(count)` 重复实时区块，不表示重复采样或自动平均。普通 Python `for` 在构建时展开。`exp.case(parameter, value)` 仅在编译时选择静态扫描条件，例如不同 RB 序列，不是实时条件分支。

### 时序规则

- 默认顺序执行；`exp.parallel(align='start'|'end')` 显式并行，各分支通常使用 `exp.serial()`。
- 并行块后等待最长分支；结束对齐要求各分支持续时间可确定。
- `exact_duration` 是严格相等约束，不是自动 padding。
- `delay(signal, duration)` 在该分支保留 idle 区间，振荡器 frame 不会自动复位。
- `reserve(signal_or_resource, duration)` 声明资源占用；未知名称或非法重叠会报错。
- `shift_phase(q.frame, angle)` 与 `reset_frame(q.frame)` 是软件参考系操作；不执行 DDS 寄存器读回。
- 同一 frame 的写入和另一个并行分支对它的访问被拒绝，避免分支执行次序改变相位含义。
- 零时长 pulse 保留逻辑边界但不产生物理输出；非零但小于设备最小时长会失败。
- 不支持固定 host repetition period、跨 shot 相位连续、实时反馈、AWG 或运动时明确拒绝。

每个实时区都保留用户声明的时间关系。相邻同参数、相位语义兼容的 pulse 可以合并为连续物理输出，原逻辑边界仍可见。相位/频率/幅值发生变化而没有合法配置窗口时，编译失败，不会插入隐藏等待。RB 的 `inter_gate_delay` 必须显式传入。

默认时间必须能精确落在映射设备的栅格上。拟合参数常不是整数 tick；可以显式使用 `allow_quantization=True`，按该信号所用设备栅格的公倍数舍入 duration/delay，并将请求值、实际值和误差纳入诊断。不会通过改变先后关系或插入 gap 修复不满足的对齐约束。非零操作不能被舍入为零。

## Qubit 参数与校准

```python
from taiyidsl import (
    Qubit,
    QubitStore,
    QubitParameters,
    DriveParameters,
    analyze_rabi,
    synthesize,
    CalibrationPolicy,
)

store = QubitStore([Qubit("q0")], path="output/qubits.json")
store.save()
q0 = store.update("q0", {"raman.amplitude": 0.4}, persist=True)

exp = rabi(q0, [i * us for i in range(61)], shots=500)
compiled = compile_experiment(exp, *reference_setup([q0]))
results = synthesize(compiled, seed=1, model_parameters={"pi_duration": 12e-6})
proposal = analyze_rabi(results, experiment=exp)

# 用户/工作流决定应用，拟合本身不会修改参数。
q1 = store.apply(proposal, persist=True)
```

`Qubit`、嵌套参数和扩展字段是不可变值对象。修改通过 `store.update(...)` 或 `store.apply(...)` 创建新 revision。必须重新取 `store['q0']` 或使用更新返回值；已有变量、旧实验和旧编译结果保持原版本。

参数字段包括 `params.raman/rf.frequency`、`amplitude`、`pi_duration`、`pi2_duration`、`params.readout.duration`。自定义配置放入 `params.extensions[namespace]`。设备共享的时钟、延迟、相位偏移和幅值换算放入 `DeviceSetup.calibration`，不复制进每个 Qubit。

`CalibrationProposal` 记录基准 revision/fingerprint、更新字段及单位、拟合质量和来源。来源过期、单位错误、非法参数或磁盘保存失败都不会修改当前 store。JSON 保存采用原子替换，并保存前后快照及历史。

自动应用必须显式调用，并提供质量策略。例如：

```python
policy = CalibrationPolicy(min_r_squared=0.98, max_relative_uncertainty=0.05)
# 这是另一种应用路径；不要再次应用上面已经应用过的 proposal。
# store.apply_auto(fresh_proposal, policy=policy, persist=True)
```

阈值是工作流选择，不证明物理校准正确。Rabi 校准器只处理与源 Qubit profile 一致的方波时长扫描；不同幅值、频率、额外驱动或不匹配的实验来源不能覆盖该 profile。Ramsey 拟合给出频差绝对值和 T2*，不推断频率修正符号。RB 保留随机种子、Clifford ID、recovery 和实际 gate words；理想 recovery 使用精确整数 SO(3) 检验。

## 设备映射与导出边界

`SignalMap` 可以将逻辑信号映射为设备名、`ControlBinding` 或多输出的 `ControlRecipe`。后者可以展开一条逻辑 Raman 控制为多个同步物理输出。每个 binding 可带频率/相位/幅值换算；不假设它自动代表某个真实双光子跃迁。

`DeviceSetup` 声明设备、输出栅格、DDS 配置栅格、配置预算、总线和更新组。编译器处理共享资源和配置副作用；参考后端保守地禁止在同一更新组的其他 DDS 输出期间更新参数。准备调用还需满足单个 kernel 的提交顺序，因此能力可能保守于硬件可实现的最优排程。

首版 AD9910 参考契约使用 4 mu 输出栅格、8 mu 配置栅格、1000 mu 示例配置预算，以及独立 shot 的 tracking phase。**这些不是实验室设备的实测参数。** 默认 `tick_seconds=1e-9`，可按目标配置。光路延迟和相位偏移需要独立标定。

编译产物保存 IR、配置快照、hash、每点逻辑/物理事件、平均顺序、来源及诊断。修改编译产物后调用导出、可视化、结果生成或保存会触发完整性检查，应从实验重新编译。

默认 `mode='auto'` 导出 **host averaging/sweep 循环 + 参数表 + 按事件结构复用的参数化 kernel**。这适用于所有自定义实验，不依赖实验名或是否调用内置 Rabi/Ramsey 模板。相同的有序设备操作结构共享程序，时间戳、DDS tuning words 等数值从参数行传入；短结构能够匹配长结构的有序子序列时，使用可选事件槽，例如 Rabi 的零时长点、Ramsey 的零延迟点。无法共享结构的点分组生成多个 kernel，不强行合并。

```python
# 默认：参数化导出，host 端仍按 cyclic/sequential 次序调用。
compact = compiled.export_artiq("output/artiq", mode="auto")

# 调试：每个扫描点生成独立 point_N kernel。
expanded = compiled.export_artiq("output/artiq_unrolled", mode="unrolled")

from taiyidsl.artiq import verify_equivalence

comparison = verify_equivalence(compiled)
```

两种模式只改变生成代码的表示，不改变已经编译的逐点时间表。`auto` 并不把 sweep 或平均循环移入 kernel；host 读出适配器边界保持原样。单个 shot 内的静态 `repeat` 当前仍在排程时展开，参数化导出不等于完整保留所有 DSL 循环，也不增加实时分支能力。

导出还包括设备初始化、DDS 寄存器量化表、目标配置和独立 mock 验证报告。生成代码需要用户接入 `result_adapter` 的 `before_shot(point, shot)` / `after_shot(point, shot)` / `finish()`；它不假设相机数据传输已实现。运行入口检查 core/DDS 时钟，DDS 设置调用超出声明的配置预算时失败。参数化可以缩短源码，但并不自动保证只编译一次或提高实机吞吐量。

`verify_export` 在独立 fake ARTIQ 模块中执行生成的代码，再比较重建事件与执行计划，能发现代码生成错误。`verify_equivalence` 分别执行默认与展开代码，逐点检查事件次序、时间、DDS 参数、phase reference 和结束时间一致，并检查 host 平均次序。它们不等价于运行真实 `artiq_compile`。实际硬件接入的步骤与限制见 `docs/architecture.md`。

## 数据与验证

`ResultSet` 按 handle 保存数值数组、status、labels、外部图像引用及执行序号。`unexecuted/missing/failed/atom_lost/loading_failed` 与有效的 0 区分；均值仅归约有效数值，并返回有效样本数。支持显式 postselection。

结果以 JSON + NPZ 保存，加载禁用 pickle。`synthesize` 是带固定种子的合成数据发生器，用于测试统计和校准闭环，不是依据完整编译波形的原子动力学仿真。相同坐标和 shot 的合成观测不因平均顺序改变。

测试包括快照和原子保存、两种平均模式、多轴扫描、时序/资源失败、frame 累积、DDS 栅格、严格边界、生成代码变异检查、结果缺失处理、拟合来源校验和完整校准闭环。当前测试与示例结果参见[验证记录](validation.md)。开发和协作流程参见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

初始化示例扫描六个抽运时长和两个读出条件，共 12 个扫描点，共用一个参数化 shot kernel。每个 shot 的冷却、偏置场和光路状态由实验室集成提供；示例成像协议只生成 TTL 触发，不含照明、相机传输或已标定的自旋判别。示例输出位于 `output/state_initialization/`，所有数值均用于离线演示。

HTML 内嵌脚本语法测试需要 Node.js；CI 显式配置 Node.js 22，避免遗漏此项检查。主 [README](../README.md) 为英文，中文说明保留在本文件。
