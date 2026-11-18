# LabConfig、OpticsConfig 与 Experiment：参数、操作和实验流程

更新：2026-10-30。以下接口用于初始化和 Raman sideband cooling 两个离线示例。本文路径沿用旧版名称，方便已有链接继续访问。

## 1. 三个输入角色

| 对象 | 保存内容 | 使用方式 |
|---|---|---|
| `LabConfig` | 设备、共享资源、逻辑信号映射、触发序列、设备标定、backend | 在编译时绑定硬件 |
| `OpticsConfig` | 共享光学工作点、默认脉宽和等待时间 | 轻量参数表，可从字典或 JSON 构建 |
| `Experiment` | Qubit 与参数快照、扫描、平均和具体时序 | 组合普通 Python 操作函数 |

逻辑信号名连接实验与 lab。例如参数表描述 `optics.pump_556` 的频率、幅值，lab 将同名信号映射到 DDS。参数表不含 Urukul 通道号，也不选择实验执行流程。

```python
from taiyidsl import LabConfig, OpticsConfig, Qubit
from examples.state_initialization import build_initialization_scan

q0 = Qubit("q0")
lab = LabConfig.load("examples/config/labs/initialization.json", qubit_id=q0.uid)
optics = OpticsConfig.load("examples/config/optics/initialization.json")
exp = build_initialization_scan(q0, optics, shots=100, averaging="cyclic")
compiled = lab.compile(exp)
```

同一份 lab 可以编译多种实验，只要映射与设备能力满足要求。两个离线示例保留原接线：初始化的 push-out 使用 `urukul0_ch1`，RSC 使用 `urukul1_ch1`。它们不是已确认的同一个实际实验室配置。

## 2. OpticsConfig 是轻量参数表

一个只有抽运参数的配置就能独立构建，无需补齐 cooling 和 readout：

```python
optics = OpticsConfig.from_dict(
    {
        "schema_version": 2,
        "revision": "illustrative-v1",
        "pump": {
            "signal": "optics.pump_556",
            "frequency_hz": 80_000_000,
            "amplitude": 0.1,
            "duration_s": 20e-6,
            "setup_wait_s": 20e-6,
            "settle_s": 20e-6,
        },
    }
)
data = optics.to_dict()  # 独立普通字典，可用 json.dumps(data) 保存
```

schema 2 使用少量浅层分组：`pump`、`pushout`、`beams`、`raman`、`imaging`、`camera`。只提供本实验需要的组。顶层还有版本标签和可选 `conditions`；数字使用 Hz、s、rad，`amplitude` 是 0–1 的 RF 输出幅值比例。

少量实验室自定义数据可放入 `extensions` 命名空间。`OpticsConfig` 中的全部值必须能够直接 JSON 往返，因此时长使用以 `_s` 命名的 SI 数值；`50 * us` 等单位量用于 Python 操作调用。普通 `Experiment(parameters=dict)` 也支持单位量，快照会复制并检查其值。

`pump` 和 `pushout` 直接保存 signal、频率、幅值、默认时长和明确等待窗口，没有 `kind / parameters / beam` 包装。RSC 的 `beams` 按名字保存光束工作点；`raman["x"]` 的 `a`、`b` 引用这些名字。公共 Raman 光束和抽运参数各维护一份，不在每个轴中复制。

频率仍是本例 RF 控制信号的频率，不自动等于原子处光学频率或边带失谐。版本标签不构成自动校准数据库；`conditions` 是实验前置条件说明，不会操作磁场或检查激光锁定。

## 3. 单次调用怎样覆盖一个参数

操作函数使用显式关键字覆盖这一次调用的值：

```python
from taiyidsl import Experiment, us
from taiyidsl.cooling import optical_pump

exp = Experiment("override_demo", qubits=[q0], parameters=optics)
p = exp.parameters

with exp.average(shots=1):
    with exp.realtime():
        optical_pump(exp, p["pump"], amplitude=0.12)
        optical_pump(exp, p["pump"], duration=50 * us, amplitude=0.08)
        optical_pump(exp, p["pump"])  # 重新使用快照默认值
```

第一句只覆盖幅值；第二句覆盖时长和幅值；频率、相位和等待窗口继续使用参数表。三个调用不会修改 `p`、原始 `optics` 或其他实验。显式 `amplitude=0` 也是覆盖，不会被当作“未提供”。`duration=0 * us` 保留零抽运点，无输出毛刺。

`optical_pump` 支持 `duration`、`amplitude`、`frequency`、`phase`、`setup_wait`、`settle` 关键字；单位量或 sweep 必须符合相应维度。拼错关键字和非法值会报错。生效参数记录在操作 IR 中；编译后的 pulse 记录每点实际值。基准快照保留默认值，因此既能追溯来源，也能看到单次覆盖。

参数改变可能需要 DDS 重新配置。显式覆盖不会自动插入准备窗口；不够用时编译报错，用户应在实验中安排合法窗口。硬件配置预算仍由 Lab/adapter 给出。

同一次构建覆盖不同参数组，可使用普通 Python 字典合并，无需新增配置类型：

```python
exp = build_cooling_scan(
    q0,
    parameters={
        **optics,
        "pump": {**optics["pump"], "duration_s": 50e-6},
        "pushout": {**optics["pushout"], "amplitude": 0.25},
    },
)
```

这会改变本次构建实验的默认值；所有读取这两组参数的调用都会使用新值。它不改变原始 optics，也不是“只改第一个 pump”。仅修改一个具体操作时，仍使用该操作的关键字参数。

## 4. 执行方案属于 Python 操作与实验

使用 RSC 参数表时，轴顺序、每轴次数和轮数由调用表达：

```python
sideband_cool(
    exp,
    p,
    axes=("x", "y", "z"),
    cycles_per_axis=2,
    rounds=3,
)
```

参数表保存各轴频率、幅值和脉宽，不再保存 `stages`、`cycles`、`rounds` 或读出类型。操作函数将执行方案展开成 `serial/repeat/parallel/play/delay`。复杂 schedule 可以先在 Python 中显式组织，没有新增一套配置驱动的流程语言。

初始化与 RSC 使用不同的读出函数，选择在实验代码中可见：

| 函数 | 输出行为 |
|---|---|
| `trigger_readout(exp, q, p, pushout=...)` | push-out／等长 idle → 相机触发 → 等待；保持初始化原有行为 |
| `illuminated_readout(exp, q, p, illumination=(...), pushout=...)` | push-out／等长 idle → 并行照明与相机触发 → 等待；保持 RSC 原有行为 |

`illumination` 指定使用哪些命名光束；读出方式不再由 Config 子类决定。相机数据传输、装载状态和自旋判别仍由实验室集成处理。

## 5. 快照与校验

`Experiment(parameters=optics)` 或 `Experiment(parameters=ordinary_dict)` 都产生独立只读快照。核心 Experiment 接受通用映射，不依赖光学模型。修改构建实验前使用的字典，或创建新版本 OpticsConfig，不改变旧实验。

光学 builder 把普通字典规范化为 OpticsConfig；Experiment 复制时保留该类型与独立只读数据。`OpticsConfig.coerce()` 重用已经校验且不可变的配置，避免组合操作逐层重复检查整张表。

传给 sideband_cool 或读出操作的整张普通参数表必须包含 `schema_version=2`，且所提供的参数组符合配置格式；旧代码若直接传入无 schema 的零散字典，需要补齐这一入口约定。单独的 optical_pump/raman_pulse 仍接受原来的 pulse/beam 子字典。

校验职责分配如下：配置加载器检查字段、静态范围及引用；`Square`、`delay` 等 DSL 构造统一检查有效数值、单位和扫描值，`repeat` 检查次数；操作函数只处理轴选择、读出方式等组合规则。独立传入原始 pulse/beam 字典的公开操作仍检查它的配置结构；内部已解析配置的时序函数不再重复校验。编译器继续检查时间栅格、共享资源、配置窗口和采集索引。`metadata` 保存说明和实验方案标签，不作为工作点参数源。

轮数扫描使用 `with exp.sweep(count) as cnt` 和 `sideband_cool(..., rounds=cnt)`。别名仍是参数引用，循环次数在每个扫描点绑定；详见 Raman sideband cooling 文档。读出行为分支仍使用 case。

`Qubit.params` 保留 qubit 专属门标定；共享光学参数留在 OpticsConfig；设备路径延迟和输出换算留在 Lab。没有共享表与 Qubit 的隐式互相覆盖。

## 6. 从上一版迁移

上一版 `RecipeSet`、`PulseRecipe` 等类型化操作容器已从当前接口移除。新的 `OpticsConfig` 沿用名称，但不再采用旧版必填 `cooling/readout` 聚合结构。配置采用 schema 2；旧结构需显式迁移，不能只修改版本号。

新的参考参数文件是 `examples/config/optics/initialization.json` 和 `examples/config/optics/rsc.json`。两个 builder 仍接受 `qubit, parameters, *, ... shots, averaging`，第二个参数可为 OpticsConfig 或符合相同分组约定的普通映射。RSC builder 额外明确接收轴顺序和循环次数。

LabConfig 的 JSON loader、TriggerSequence/TriggerStep、编译和导出接口保持。旧 Protocol/ProtocolStep 是触发规格的兼容名称，与本次移除的 recipe 容器无关。

## 7. 设备初始化与验证边界

exporter 继续按所有扫描点的实际物理事件选择使用设备。未使用设备不会因为在 lab 库存里就收到 `init()` 或 `off()`。

若所用 DDS 与未用通道共享总线或更新组，要求外部准备并明确声明 `prepared_shared_resources`。实机 profile 必须列出全部可能受共享操作影响的通道；core reset 仍要求整个 RTIO 域独占。这不证明电气隔离或支持并发实验。

本次改动前的逐点基线保存在测试 fixture 中，`output/optics_refactor_equivalence.json` 记录两种平均模式下的比较。旧的 30 点基线仍用于跨两轮重构检查。各例子的 `verification.json` 单独检查参数化与展开 exporter 的独立 mock 事件一致性。

真实 ARTIQ 编译、板卡初始化影响、RTIO 吞吐、光学同步和物理冷却性能仍未验证。
