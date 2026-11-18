# 后端接入与实验室配置

前端构建实验意图，后端负责把意图落实到特定硬件的时间表和离线程序。当前内置
`artiq`（ARTIQ 9 / AD9910 / TTL）及 `artiq_spectrum`（ARTIQ 与 Spectrum AWG/DDS
混合控制）。另有显式 session、Spectrum adapter 和持久执行入口；编译与导出仍
保持离线，不因创建后端而连接仪器。运行入口和实机验收是不同层次。

| 能力 | 当前实现与边界 |
| --- | --- |
| AD9910 / TTL | `artiq`；各 Square/RAM 路径有独立的时序和协议约束 |
| 多卡 AWG | `artiq_spectrum`；同步组触发与静态 bank，实际同步需测量 |
| 交叉 AOD 搬运 | 混合后端的 Spectrum DDS transport；普通 play 不映射到 DDS |
| 原生准备和读出 | 固定边界的 NativeKernelPackage 或 NativeProgram，非门间任意交错 |
| host 运行 | 显式配置 session/runner；相机 SDK 与设备配置由实验室集成 |
| 实时反馈与跨 shot 相干 | 当前不支持，不能由能力声明自动获得 |

公共入口见[API 索引](public_api.md)，合作扩展示例见[开发指南](development.md)。

## 使用实验室配置

```python
from taiyidsl import Qubit, rabi, reference_lab, us

q0 = Qubit("q0")
lab = reference_lab([q0])
experiment = rabi(q0, [0 * us, 4 * us, 8 * us], shots=100)
compiled = lab.compile(experiment)
compiled.visualize("output/rabi.html")
compiled.export("output/rabi_artiq")
```

`LabConfig` 集中保存编译配置与显式连接所需的入口：

| 字段 | 职责 |
|---|---|
| `setup` | 设备、时钟、协议与共享标定 |
| `signal_map` | 逻辑信号到设备输出的绑定与变换 |
| `backend` | 后端实例或已注册名称，默认 `"artiq"` |
| `qubits` | 登记逻辑信号身份及共享输出参与者 |
| `emulation`、`artiq_python` | 显式连接模式和独立 ARTIQ 解释器；不影响普通编译保持离线 |
| `transport` | AOD 几何、站点与设备标定/绑定 |

`reference_lab(qubits)` 提供每 qubit 一块示例板卡的配置；`initialization_lab(qubit)` 提供抽运、push-out 和成像触发的示例配置。二者位于 `src/taiyidsl/lab.py`，每次调用都返回独立配置。实验室应维护自己的配置工厂，使用自己的设备名、时序预算、协议和标定。

编译时复制 setup、mapping 和后端策略。修改 lab 只影响后续编译；此前生成的计划保留原快照。`lab.compile(..., allow_quantization=True)` 等选项转交编译器；后端选择只放在 `lab.backend`，不再通过 `lab.compile` 的关键字覆盖。

原接口继续有效：

```python
from taiyidsl import compile_experiment, reference_setup

setup, mapping = reference_setup([q0])
compiled = compile_experiment(experiment, setup, mapping)
compiled.export_artiq("output/rabi_artiq")
```

`reference_setup` 是返回二元组的兼容入口。`compiled.export(...)` 使用编译该计划的后端；`compiled.export_artiq(...)` 只用于 ARTIQ 计划，不能把其他后端的物理事件表转换成 ARTIQ。

## 各层的职责

```text
DSL / Node 源结构
    ↓
compiler：实验结构、sweep 点、采集索引
    ↓
Backend.schedule：逻辑时间表
    ↓
Backend.lower：目标物理时间表 / PhysicalPlan
    ↓
Backend.export：离线目标产物
```

默认 `Backend.schedule` 调用 `scheduling.Scheduler`，处理串并行、静态 repeat/case、软件 frame 和协议展开；时间由 `setup.tick_seconds` 表示。`Backend.signal_grid(signal, setup, mapping)` 默认使用该信号映射设备的网格公倍数。新目标可以只覆盖网格策略，也可以覆盖整个 `schedule`。

`ir.py` 中的 `ScheduledEvent`、`CompiledPoint`、`PhysicalPlan` 是共享容器，并不意味着里面的时间和设备操作跨目标通用：

| 表示 | 含义 | 换硬件时 |
|---|---|---|
| DSL `Node` 树 | 源实验结构与参数表达 | 保留并重新编译 |
| `compiled.ir` | 源树的可审查 JSON 记录 | 当前无公开加载/编译入口 |
| `logical_events` | 使用逻辑信号、已落到目标 tick/grid 的时间表 | 不能默认复用 |
| `physical_events` | 目标设备调用、物理资源与准备动作 | 重新生成 |

ARTIQ 的 `lower` 位于 `backends/artiq/backend.py`：DDS 配置窗口、SPI/提交资源
约束、共享更新组、AD9910 FTW/POW/ASF 量化、延迟标定和相位由该后端及设备路径模块
处理。`backends/artiq/codegen*.py` 生成源码，`mock_drivers.py` 与 `verification*.py`
负责独立执行／核对，`export.py` 编排发布。旧根 `artiq.py` 是按符号转发的兼容入口。
混合实现位于 `backends/artiq_spectrum/backend.py` 与 `scheduling.py`，由同包
`awg.py/dds.py/banks.py/export.py` 处理样本、命令、bank 与导出。
新后端不需要继承 AD9910 的约束。

## 目标实现内部的依赖

- 目标展开与程序生成不依赖 mock 驱动或验证器；验证器需要默认源码时可调用生成器。
  导出入口协调生成和验证，公共契约模块保存双方需要的记录结构。
- `backends.artiq` 与 `backends.artiq_spectrum` 是按需加载的兼容命名空间。
  读取样本或命令的内部消费者直接导入所属子模块，不通过后端类装配编译流程。
- AWG 验证器 `backends.artiq_spectrum.validation_awg` 独立计算数学期望样本，
  与有限回放结果对照；它仍复用 `awg` 的样本回放工具。
- DDS 的 `validate_dds_program` 保留在 `dds`，按原意重建并核对命令、约束与哈希。
  这项检查并非独立物理输出模型，不应与 AWG 数学对照或硬件测量混称。
- 硬件连接、厂商 SDK 与实际执行归 `runtime`；报告归 `reporting`，不得因为都是
  Spectrum 功能而搬回目标编译包。共享 `spectrum_calibration` 继续服务标定分析和
  运行时解析，`spectrum_identity` 负责纯身份计算。

具体文件归属见[源码分层](source_layout.md)。以上是实现职责，不表示本次源码已经
完成真实编译或设备验收；实际验证范围须与对应记录一起审阅。

## 实现另一个后端

继承 `taiyidsl.Backend`，声明以下元数据：

- `name`：合法标识符；`artiq` 与 `artiq_spectrum` 是不能覆盖的内置名称。
- `version`：后端语义版本；改变排程、量化或导出策略时更新。
- `target`：目标平台描述。
- `phase_policy`：物理相位如何实现的明确说明。
- `capabilities`：`BackendCapabilities` 实例，声明支持的设备类型、pulse 类型、操作及 average/sweep 执行位置。

实现或按需覆盖以下方法：

| 方法 | 返回与责任 |
|---|---|
| `validate_setup(setup, mapping)` | 检查目标时钟、设备配置及连接约束；默认不做目标专属检查 |
| `signal_grid(signal, setup, mapping)` | 返回以目标 tick 为单位的正整数网格；默认取映射设备网格公倍数 |
| `schedule(experiment, body, setup, mapping, coordinates, allow_quantization, diagnostics, max_events)` | 返回 `(logical_events, duration_mu)`；默认复用公共排程器 |
| `lower(logical_events, setup, mapping, diagnostics)` | 必须实现，返回 `PhysicalPlan(events, origin_mu)` |
| `export(compiled, output_dir, **options)` | 必须实现，生成离线目标产物，不提交、不运行 |

`lower` 应承担设备绑定、复合输出变换、共享资源冲突、时序可行性、校准应用、配置预留、数值量化和相位实现。若使用预留起始时间，`origin_mu` 表示逻辑起点在物理时间轴上的偏移，返回的物理事件时间必须与这个约定一致。不能为了让硬件接受而悄悄改变实验的 pulse 间隔或忽略不支持的操作。

公共编译器检查顶层 average、嵌套 sweep 链、扫描点限制与每个 point 的采集 handle/qubit 集合。覆盖 `schedule` 的后端仍须满足这些契约，并自行保证事件预算和所声明的时序语义。

能力声明是现有 DSL 的子集。例如 `pulse_shapes={"square"}` 只接受方波。把某个未来形状或执行位置写进声明，不会让 DSL 自动获得该语法，也不会实现采集反馈。要支持这些功能，需要另行扩展语言与结果语义。

后端策略保持无状态；具体时钟、延迟、寄存器配置、采样率等放在 setup 中。公共 `Device` 保留原来的 DDS 字段以兼容旧配置；新目标可以用 frozen dataclass 子类添加配置字段。字段应可深复制和 JSON 序列化，这样可以进入 setup 快照和内容 hash。基础名称和整数网格约束在设备构建时检查，设备类型支持、DDS 频率范围和配置网格等目标规则在编译阶段检查。

可直接传入后端实例，例如：

```python
from taiyidsl import LabConfig
from taiyidsl.backends.artiq import ArtiqBackend

lab = LabConfig(setup, mapping, backend=ArtiqBackend())
compiled = lab.compile(experiment)
```

对于已经实现并实例化的 `my_backend`，可以直接赋给 `LabConfig.backend`；也可以显式注册一次，然后按名称选择：

```python
from taiyidsl import register_backend

register_backend(my_backend)
lab = LabConfig(setup, mapping, backend=my_backend.name)
```

重名注册会报错；没有自动插件发现或导入。编译产物保留当次后端快照，通用导出不会重新按名称寻找另一个实例。`tests/test_backends.py` 中的测试后端演示了独立事件类型、网格覆盖和离线导出，可作为接口使用参考，不是可连接硬件的驱动。

后端名称、版本、目标、能力和相位策略会进入 provenance 和内容 hash。版本必须由实现者维护；hash 不会自动扫描后端 Python 源码，也不构成实机正确性的证明。

## 物理连接与运行边界

以初始化示例为例：

```text
play("optics.pump_556", ...)
    → SignalMap["optics.pump_556"] == "urukul0_ch0"
    → DeviceSetup.devices["urukul0_ch0"]
    → ARTIQ 源码中的 get_device("urukul0_ch0")
    → 实验室提供的 device_db 和真实设备
```

`DeviceSetup` 保存的是编译契约，不是完整的底层设备数据库。`bus` 与 `update_group` 是资源关系标签；core 地址、完整 RTIO/驱动参数、板卡接线和光路需要实验室另行管理。设备名称必须与外部 `device_db` 对齐。生成代码会检查部分目标时钟，但不会证明实际接线或光学路径正确。

`Protocol` 当前在公共排程器中展开为带 offset/duration 的触发步骤，ARTIQ 后端要求这些步骤绑定 TTL。`measure` 记录采集意图，并不自动完成相机取数。真实加载、探测器 arm/transfer、结果关联仍由外部 `result_adapter.before_shot / after_shot / finish` 完成。

`lab.connect()` 提供显式硬件 session，SpectrumRunner 负责已配置的板卡执行，
StreamingRunner 配合 RunStore 管理分块与持久提交。它们不自动发现仪器，也没有
统一的 detector 能力：HardwareSession 当前拒绝 acquisition，普通导出和持久
执行的 result adapter 契约见[读出说明](readout_adapters.md)。ARTIQ mock 等价检查、
真实离线编译和实机时序验收仍分别记录。

## 迁移现有实验

1. 保留使用逻辑信号的实验构建函数；将设备、mapping、协议和共享标定集中到实验室配置。
2. 为新目标实现并测试后端，明确时钟、资源、相位与采集契约。
3. 使用原实验重新编译到新目标；检查时间量化、资源冲突和显式 pulse 间隔。原设备可行的间隔可能不适合新设备。
4. 对照逻辑时间表与目标物理事件，并验证扫描/shot/handle 索引。为导出程序建立目标独立的事件重建或模拟检查。
5. 离线行为确认后，另行接入部署、真实驱动和结果 adapter；真实编译与硬件验证不能由离线测试替代。


## 通用排程与目标排程

`compiler.scheduling.Scheduler` 提供共享规则；`dsl.sections` 负责 Section 声明与组合，
`compiler.section_schedule` 只负责放置。目标排程通过 `section_grid()` 提供容器内
事件的基础时间网格，并沿用 `signal_grid()` 的信号限制。Spectrum scheduler 在
triggered region 内返回 sample 网格，在 region 外返回 native 网格。
基类也保留旧规则：setup 携带 Spectrum 配置时使用其 native 网格，否则为 1 tick。
精确换算位于 `compiler.timebase`，通用排程无需导入具体后端。

资源查询通过 `signal_frame()`、`frame_owner()` 和只读 `time_origin` 完成，不从通用
算法访问具体后端的私有 region 或 frame 字典。新目标扩展这些查询时，必须覆盖实际
放置时间与最终相位，不得通过扩大间隔或自动改网格掩盖不支持的请求。
