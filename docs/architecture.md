# 实现架构与契约

当前内置 `artiq` 和 `artiq_spectrum` 两个后端，另有原生组件、Spectrum host
runtime、显式硬件 session 与持久执行入口。它们共享实验构建和编译契约，运行能力
并不完全相同。公共使用路径见[API 索引](public_api.md)，扩展步骤见[开发指南](development.md)。

源码按职责逐步分包，当前阶段及规范路径见[源码分层](source_layout.md)。报告与原生
接入分别归 `reporting/`、`native/`；执行与结果归 `runtime/`、`results/`。实验构建、配置
和编译归 `dsl/`、`config/`、`compiler/`；目标实现归 `backends/artiq/` 与
`backends/artiq_spectrum/`，可复用实验与理想序列数学归 `experiments/`。
Section 的构建模型只属于 DSL，其放置算法属于编译器。

## 编译边界

1. `dsl` 通过上下文构建 `Node` 树。失败的构建 block 回滚；无硬件调用。
2. `config.models` 提供可编辑的角色参数 Qubit、独立只读快照、JSON 版本存储和校准事务；旧式 Qubit 与 Store 记录保留只读行为。
3. `lab.LabConfig` 集中保存后端、`DeviceSetup` 和 `SignalMap`，通过 `load/from_dict` 读取统一 profile；轻量 `config.optics.OpticsConfig` 保存共享工作点。`Experiment(parameters=...)` 接受通用映射并通过独立的 `config.parameters` 模块捕获不可变快照，核心 DSL 不依赖光学模型。
4. `compiler` 检查公共结构和采集索引，解出有限 sweep 笛卡尔积，并逐点调用后端的 `schedule` 和 `lower`。
5. `Backend.schedule` 默认复用 `compiler.scheduling.Scheduler`，静态选择 `case`、展开实时区块并维护 frame；网格由后端的 `signal_grid` 决定。不同目标可以覆盖排程策略。
6. `config.hardware` 描述逻辑映射、复合控制绑定、触发序列及设备契约；`config.optics` 校验平直的参数表，`experiments.cooling` 按 Python 调用参数展开 DSL，执行顺序和次数不存入 OpticsConfig。`ir` 提供事件容器和后端返回的 `PhysicalPlan(events, origin_mu)`。
7. `backends.artiq.backend.ArtiqBackend` 展开 DDS/TTL 设备操作、资源和准备动作，检查物理可行性，并完成 AD9910 量化与 tracking-phase 参考。公共编译器不包含这些规则。
8. `backends.artiq.codegen*` 对旧 Square 路径按事件结构分组生成参数化 kernel 与 host 参数表；timed Square 当前逐点生成 kernel，RAM 长序列可用逐点事件表。`mock_drivers/verification*` 执行并重建事件，`export` 编排两者；生成器不依赖 mock 验证实现。
9. `reporting.visualization` 使用同一个执行计划生成双层时序图；公共序列化在 `serialization` 中，不依赖 ARTIQ exporter。`results` 管理数据归档、图像处理和校准建议。
10. `backends.artiq_spectrum.backend/scheduling` 编译混合时间域、同步组触发与原生准备边界；同包 `awg/dds/banks/export` 负责样本、命令、bank 与导出。报告归 `reporting`，host 执行归 `runtime.spectrum`。
11. `native.components` 声明独立 ARTIQ 子实例与生命周期；`compiler.streaming` 管理磁盘编译，`runtime.streaming/run_store` 分别管理分块执行与持久采集状态。编译时设备并集初始化源码生成在 `compiler._stream_initializer`，后端和运行器共享 `spectrum_identity` 纯指纹函数；编译入口不导入运行器。

IR 保存 average/sweep/repeat/case 层次，未展开全部 shots。每个扫描点保存其静态事件，重复 shots 重用同一计划。默认限制 10,000 个扫描点和每点 100,000 个事件，避免无界展开。参数化优化在 exporter 层进行，编译时间表仍可逐点检查。

`sweep(parameter)` 的 context manager 返回该参数，供 `as cnt` 使用。repeat 的 count 可以是非负整数或无量纲整数 SweepParameter；构建时验证全部候选值，编译时核对绑定定义，并在每点以精确整数展开。零次不产生事件或 frame 更新。它不改变 host 平均顺序，也不引入实时动态控制流。

OpticsConfig 的快照保留类型和独立不可变数据；组合操作复用已校验配置，独立传入普通 pulse/beam 字典的入口仍校验结构。通用数值、单位和范围检查集中在 DSL 的 Square/delay/repeat；硬件栅格、资源与准备窗口仍由编译器检查。配置保持纯参数，不承载执行流程。

这里的源 IR 是 DSL 的 `Node` 树，`compiled.ir` 是它的可审查 JSON 表示。`logical_events` 虽然保留逻辑信号，却已按所选目标的 tick/grid 排程；`physical_events` 则进一步使用该后端的设备操作。二者都不应直接跨硬件复用。当前没有公开的 `compile_ir(serialized_ir, ...)`；换目标应从原实验重新编译。

`compiler.preparation` 复制配置并验证源结构；`compiler.points` 执行每点排程、采集
签名检查、目标展开和 native 绑定；`compiler.artifact` 组装同一格式的元数据与来源。
`pipeline` 维持调用顺序，磁盘 point sink 只改变保留方式。`compiler.store` 负责读取
已完成存储；中性的 `host_plan` 提供执行顺序与布局读取，results 无需导入编译流程。

`Section` 的资源与网格查询通过 scheduler 的明确方法完成；通用算法不 import
Spectrum 后端，也不读取它的活动 region 私有字段。Spectrum scheduler 决定 region
内 sample 网格与 region 外 native 网格，最终 frame 解析仍在放置后进行。

## 后端接口与兼容性

`Backend` 定义能力声明、配置检查、排程、物理展开和离线导出。`compile_experiment(..., backend=...)` 接受实例或已注册名称；`register_backend(instance)` 显式注册，不自动加载第三方插件。编译时复制后端策略，产物的 `export(...)` 使用该后端；`export_artiq(...)` 作为 ARTIQ 兼容入口保留，但不能用来转换其他后端已经编译的计划。

`BackendCapabilities` 在排程前检查设备类型、pulse 类型、操作及 average/sweep 的执行位置。声明只表达当前语言的可支持子集，不会扩展 DSL 语法。`Device` 保留原有 DDS 字段以兼容旧配置，同时允许新的 kind 标识符；类型是否受支持、DDS 时钟和配置栅格等目标检查转到编译阶段。新增配置可以使用 `Device` 的 dataclass 子类，保持字段可序列化。

`reference_setup` 是 `reference_lab` 的兼容包装；原 `(setup, mapping)` 用法继续可用。`LabConfig.compile(...)` 将配置集中传给编译器，不连接仪器。后端实现及迁移约束详见[后端接入说明](backends.md)。

## 生成与验证各自负责什么

ARTIQ 包的 `backend` 及设备路径模块负责目标展开；`codegen*` 只生成源码，
`verification*` 与 mock 驱动执行源码并核对预期，`export` 负责发布。通用目标记录
和 polar 记录分别在 `_contracts/polar_contracts`，不由验证器拥有生成器所需的类型。
后端包入口按需加载符号；内部调用直接依赖实际所有者，旧 `artiq/artiq_ram/artiq_polar`
等平铺路径只做兼容转发。

Spectrum AWG 的期望波形公式位于 `validation_awg`，与 `awg` 的 segment/step
回放分开，回放工具本身仍共用。DDS 校验按 `source_inputs` 重新构造程序，并比较
命令字段和 digest；它检查完整性与一致性，不能作为独立的物理 DDS 输出预测依据。
AWG 数学对照、DDS 重建检查和真实设备测量应保留不同的验证说明。

理想 Clifford 枚举与 seeded recovery 在 `experiments._single_qubit_clifford`，
一／双比特 RB 描述在 `experiments.rb_sequences`。这些模块不加载 DSL；
`experiments.templates` 与 `experiments.rb_sections` 消费数学结果，再构造实验或
Section。`experiments.ramsey/cooling` 放置可复用实验与光学操作；新实验室专用
流程仍放实验室包。

`spectrum_calibration` 保留为共享的标定分析／运行时解析模块，纯硬件身份在
`spectrum_identity`。根层共享入口与兼容模块有明确用途，不以消除所有平铺文件为目标。

## 参数化与调试导出

对于使用旧 Square 路径的 ARTIQ 计划，`CompiledExperiment.export(..., mode='auto')` 或兼容入口 `export_artiq(...)` 生成参数化程序。exporter 使用物理事件顺序和设备身份识别可复用程序；时间和 DDS 数值成为每点的整数参数。结构兼容的点共享参数化 kernel，不同结构分组。短事件序列可以通过可选事件槽复用其有序超序列。timed Square 当前使用逐点 kernel；`reference_lab` 已声明 timed Square 的示例延迟，因此参考模板也走这一路径。

这不是一个在 kernel 内动态解释任意 Python 对象的执行器。设备调用和事件顺序由生成的程序固定，参数行使用 ARTIQ 可表达的静态类型。没有 host RPC 被新增到严格 pulse 区间。

`mode='unrolled'` 保留逐点 `point_N()`，用于调试和对照。两个模式使用相同 compiled artifact，不重新排程或重新量化。host 平均循环及 `before_shot/after_shot/finish` 接口保持一致。

`verify_equivalence` 对每点分别执行两份生成程序，并核对事件及时间、DDS tuning words、相位参考和 shot 结束位置。host 循环顺序也进行核对。测试覆盖零时长、零延迟、不同平均顺序、自定义多维 sweep、结构变化和参数变异。

单个 shot 内的 `repeat` 仍在编译阶段展开。当前的优化减少扫描点间的程序复制，不承诺减少到固定一个 kernel、不承诺硬件循环执行或真实 ARTIQ 只编译一次。多个无法共享的结构仍会生成多个程序。

## ARTIQ 排程与相位

逻辑事件以 shot 内容起点为零。physical events 加上 `origin_mu` 以容纳先于逻辑起点的 DDS 准备动作。timed Square 使用 `dds_prepare / dds_commit / dds_effect`：准备允许在上一门输出期间发生，输出生效时间固定；同一缓冲状态不能在前次提交之前被覆盖。旧 Square 路径仍要求 RF-off 准备窗口。两种路径都不能侵犯显式保留资源或静默增加 gap。

输出的配置和开关满足实际提交顺序。独立 SPI bus 可以并行准备并穿插无关输出边沿；
同一或未知 bus、共享更新组、缓冲状态及 RTIO 提交容量继续检查。不同设备协议和
Square/RAM 模式有各自约束，不把一条路径的可行性推广为所有 DDS 路径可行。

DDS 准备窗口通过输出区间、保留资源、提交区间和边沿的有序索引查询，避免每次
向前搜索时遍历所有事件。先按既有顺序尝试，以保留旧可行计划；若失败，对相同
输出 deadline 的配置按较晚可开始时间优先重新尝试一次。例如两路前门分别在
4/6 µs 结束、新门均在 7 µs 开始、各需 1 µs 配置时，配置可分别放在 5–6/6–7 µs。
该重试只改变准备调用顺序，不改变逻辑输出时刻、不增加 gap、不放松共享资源约束。
成功重排记录 `preparation_reordered` 诊断。这是有界启发式，不是完备可行性求解器；
复杂交错窗口仍可能被拒绝。动态提交索引使用 Python 有序列表，插入仍有线性移动成本。

每个 shot 的 frame 重新开始。`phase` 表示相对于 `t0 + origin_mu` 的 tracking 偏移；频率改变时使用同一个参考原点下的新频率，不宣称任意跳频的累积相位自动连续。旧路径通过 `PHASE_MODE_TRACKING` 驱动实现；timed Square 预先计算实际 POW，并在固定提交时刻更新。`shift_phase/reset_frame` 在 timed Square 上产生真实硬件相位更新，可在长脉冲内部生效，不依赖后面是否有新的 `play`。RAM boundary 模式支持门边界与 RF-off 更新；旧路径仍拒绝，不降级为软件状态。细节见 [Square 时序契约](square_timing.md)和 [RAM 边界时序](ram_boundary_timing.md)。

相位数值和 DDS tuning words 的量化会记录。设备延迟补偿与相位偏移是独立 calibration，单纯设置一个 latency 不证明光学相位已校准。

当前只有一个持久驱动 frame F：`shift_phase(delta)` 加 delta，`Rz(theta)` 减 theta。
`reset_frame` 清整个 F；`reset_phase` 还清继承调制，并在该指令最终排程位置建立
零载波参考，不复位量子态。Rz 保留门级来源与 `virtual_z_frames()` 诊断，不能再次
从 F 扣除其贡献。底层使用同一相位更新机制，仍须满足硬件准备和响应约束；不能用
virtual Z 绕过带宽限制。Spectrum 默认以 triggered_sequence 入口定相，shot 参考
需显式选择；Section、operation、segment 和 chunk 不复位相位。当前公式见
[载波与调制相位](spectrum_phase_modes.md)，设备边界见[短门说明](phase_switching_and_virtual_z.md)。

`exact_duration` 拒绝不相等长度，用户应自己添加意图明确的 delay。纯 ARTIQ 显式
开启支持的时间量化后，先将操作持续时间落到映射设备栅格，再重新排程和验证；
不自动改写外部 alignment 约束。hybrid 拒绝 `allow_quantization=True`。

## 混合控制、原生组件与运行入口

混合排程 tick 与原生 ARTIQ mu 分开换算，AWG 和 DDS 各自满足目标网格。
同步组可以包含多张卡，每个活动 triggered_sequence 同刻触发该组声明的线路。
这保证计划边沿一致，不证明实卡同步或原子处光场相位。

`move` 使用独立 transport 配置，按真实分支顺序更新编译内部位置，不改用户
`q.position`。当前 Spectrum DDS 驱动交叉 AOD，与 AWG 量子控制分开；DDS 不共享
AWG 的软件绝对相位账本。站点占用、共享 tone 与回到初始布局由编译契约检查，
囚禁 hold 与失败处理见[DDS 搬运](transport_dds.md)。

Native 声明固定源文件、资源、参数和生命周期，不在 DSL 环境导入 kernel 模块。
`NativeModule.from_package` 复用静态依赖发现；`NativeProgram` 组合独立子实例，
以 `prefix* → timed* → DSL 控制 → suffix*` 调用。timed/suffix 的实际游标必须符合
声明，不补等待。initialize/finalize 每会话一次，after_shot 每 shot。
旧 `NativeKernelPackage.finalize_method` 保留每 shot 语义。

| 入口 | 当前边界 |
| --- | --- |
| `lab.compile`、export/report | 离线，无仪器连接 |
| `lab.connect()` / `HardwareSession` | 显式 session 与执行回执；当前没有 detector adapter 注入，不接受 acquisition |
| 生成程序的 `result_adapter` | host 的 before_shot/after_shot/finish；数据传输由用户适配器完成 |
| `StreamingRunner`＋`RunStore` | 分块执行、attempt 记录、数据提交与明确的恢复策略 |

`compile_stream` 按点落盘并限制受控数据和样本工作区；输入 AST、全局索引等仍在
内存，预算不是 RSS 上限。完整 store 发布后才允许运行，暂不支持中断编译续编。
`RunStore` 分离硬件完成和数据提交；恢复查找原 attempt 数据，unknown 默认停止，
不承诺物理 exactly-once。参见[读出契约](readout_adapters.md)与[分块执行](streamed_execution.md)。

## 协议与结果

`acquire(signal, duration=..., handle=..., qubits=..., metadata=...)` 在一个共享相机触发上关联多个位点；只发一次 TTL，编译器把 handle/各 qubit 纳入统一采集索引。`CompiledExperiment.acquisition_plan(point)` 返回主机适配器可检查的逐点采集声明。duration 是触发宽度，曝光及传输等待需要单独安排；同一 TTL 上相邻的独立采集触发必须有显式低电平间隔，不能合并成长脉冲。

`TriggerSequence` 是一个或多个带 offset/duration 的 TTL 触发步骤，可以占用多个逻辑信号；`Protocol` 是保留的兼容名称。步骤中的 `{qubit}` 替换为目标 ID；读出 duration 为 `None` 时取本实验 Qubit 的 `params.readout.duration`。更复杂的实时制备可以直接在 DSL 中描述；主机加载、相机数据取得和自适应步骤暂由外部 host adapter 负责。

每个 point/shot 中，同一个 handle/qubit 只允许一次测量；重复读出应使用不同 handles。所有扫描点的 handle/qubit 集合必须相同，以保持结果数组结构确定。

图像适配层还要求每个选中 handle 在每点仅对应一个逻辑采集事件；一次共享
`acquire` 可以有多个 site，但不能把不同 `measure/acquire` 的目标按 handle
合并成一帧。此检查不收紧标量 `ResultSet` 的每 qubit 索引规则。

`average` 定义采样次数和顺序，不直接平均波形、图像或状态码。`ResultSet.aggregate` 对有效数值归约，返回 mean、valid_counts 和 postselection 策略；无有效值时为 NaN。逐 shot 数据和 sequence 轴始终保留。

`ResultSet.from_compiled` 把完整 LabConfig、OpticsConfig/通用参数、Qubit、Experiment IR 和 CompiledExperiment 快照带入结果。旧 NPZ/JSON 兼容读取，新 HDF5 存档以临时文件加原子替换发布。`ImageResults` 保存每次曝光的一份原始整数图像、ROI/背景/阈值、占据和采集有效性；空位是有效 0，缺帧是 NaN。分析视图交给 xarray，详见[结果与成像](results_and_imaging.md)。

`FlatTopGaussian` 的平台时间逐点绑定，归一化包络经校准和幅值覆盖后生成实际 ASF 样本。RAM 预加载在定时 shot 之前进行，启动到包络的独立延迟通过 `Device.ram_latency_mu` 声明。重复播放配置 `ram_rearm_duration_mu` 后采用 matched-latency 契约，后端按实际样本和步长分配驻留 profile。可选 `ram_phase_update_duration_mu` 开启同profile/FTW的快速重放，其他调用使用完整rearm。再配置 `ram_phase_latency_mu` 开启有条件的连续重放：POW 可提前准备，输出分别记录包络/载波响应，并验证原有零值样本覆盖管线差。未开启时仍要求 RF-off 间隔。默认长序列导出使用逐点 kernel 内的事件表循环，调试模式逐事件展开；同一点混有 timed Square 时使用逐点展开。具体边界见[RAM 包络](pulses_and_ram.md)和[门边界时序](ram_boundary_timing.md)。

## 配置和可复现性

实验构建时保存 Qubit 的独立只读副本，不冻结用户手中的可编辑对象；setup/mapping
和后端策略在编译时复制。后端是无状态策略，目标参数放在 setup 中。编译 provenance
保存源 IR hash、配置内容 hash、所用 Qubit 快照及后端名称、版本、目标、相位策略
和能力声明。改变后端语义时更新版本，hash 不自动识别 Python 源码变化。编译对象
具有完整性校验，修改配置后应重新编译，而不是手改旧产物。

参数保存是本地文件事务：校验建议来源、单位和值，准备新 Qubit/revision/history，写临时文件并原子替换，成功后才提交内存状态。自动应用必须提供质量策略，和显式应用使用同一路径。未启用自动模式时，拟合只产生建议。

## 真实 ARTIQ 接入前的工作

逻辑信号经 `SignalMap` 绑定到 `Device.name`，导出代码再通过 `get_device(name)` 获取
外部 `device_db` 中的驱动。setup 中的 bus/update_group 是编译资源关系，不是完整
的底层连接数据库。core 地址、RTIO/驱动配置、物理接线和光路由部署侧管理；相机
与加载由明确配置的 result adapter 集成。已有 session/runtime 入口，但它们的存在
不等于真实设备和实验流程已经验收。

- 固定真实 ARTIQ、gateware、AD9910/CPLD 版本和 device database。
- 确认 core reference period、DDS sysclk、SPI 参数、更新同步与开关延迟。
- 将参考配置预算替换为驱动契约或经过检查的保守上界，测量必要的设备和光路标定。
- 实现真实制备/读出协议和 host result adapter，验证 arm/trigger/readout 的顺序和结果关联。
- 运行真实 `artiq_compile`；离线 mock 成功不能替代该检查。
- 依次验证 TTL、单 DDS、两 pulse Ramsey、跨通道对齐和 RB，并对照 RTIO analyzer、core log 与示波器/光电信号。

默认示例不操作仪器，不把 `timing_verified` 标志当作物理证明，也不把 mock 或
历史编译结果标为当前实机验收已通过。报告应引用实际导出源码与哈希，分别标注
AWG 最终 DAC、DDS 命令预测和实测结果。维护约定见[当前设计决定](design_decisions.md)。

参考：

- [ARTIQ RTIO timeline / errors](https://m-labs.hk/artiq/manual/rtio.html)
- [AD9910 driver implementation](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/ad9910.html)
- [Urukul shared resources](https://m-labs.hk/artiq/manual/_modules/artiq/coredevice/urukul.html)

## lab、参数表与初始化范围

两个光学示例采用统一 lab loader、轻量 OpticsConfig 和参数快照，具体用法与迁移说明见 [参数、操作与 experiment](lab_recipes_experiments.md)。初始化保留触发式读出，RSC 保留显式照明读出；两种 profile 的旧接线各自保留。

ARTIQ exporter 仅为本实验物理事件使用的设备生成初始化动作。若 DDS 与未使用库存共享 bus/update group，要求外部预先准备资源并显式声明 `prepared_shared_resources`，不自动执行可能影响邻居的 DDS 初始化。core reset 仍要求独占 RTIO 域；这不是并发隔离机制。

单次操作的显式关键字覆盖默认工作点，不修改共享参数。操作区块保存 `operation_name` 和 `effective_parameters`，因此基准快照与实际调用值可分别追溯。读出由 `trigger_readout` 或 `illuminated_readout` 的显式调用选择。
