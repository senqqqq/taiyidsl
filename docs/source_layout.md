# 源码分层与分阶段重构

目录按职责组织，设备或具体功能作为二级划分。迁移保持实验时序、相位、扫描顺序、
采集索引、native 生命周期和 notebook 的主要教学结构；公开 API 可通过薄转发兼容。
每阶段同时处理实际依赖，不只移动文件。阶段状态以本页为准，不把目标目录写成已完成。

## 阶段与验收范围

| 阶段 | 实施内容 | 必要验证 | 状态 |
| --- | --- | --- | --- |
| 1 | `reporting/`、`native/`；资源及真实源码清单；生成 AST 的命名结构；底层 artifact I/O | 报告样本与触发、分页与 JS、native 生命周期及生成源码等价、导入与安装资源 | 已完成 |
| 2 | `runtime/`、`results/`；资源所有权、执行阶段、数据提交边界 | 失败清理、已完成 shot 不重做、缺帧区别、存档兼容 | 已完成 |
| 3 | `dsl/`、`config/`、`compiler/`；配置快照、编译阶段与 point sink、排程资源接口 | 来源定位、扫描与采集、相关时序/相位、内存与磁盘编译等价 | 已完成 |
| 4 | 后端内部与可复用实验归类；生成与验证分离；保留旧入口 | 受影响后端的执行效果、源码／样本等价与对应实验示例；必要时离线编译 | 已完成 |

阶段之间保留可运行的检查点。结构迁移与异常路径修复分别审阅；不顺带增加真实相机、
新硬件能力或插件自动发现。每阶段只跑实际影响的检查，沿用仍然有效的证据。

## 当前已迁移的归属

```text
src/taiyidsl/
├── dsl/
│   ├── values.py           # SweepParameter、维度/计数验证与绑定值解析
│   ├── pulses.py           # 命名 pulse 与相位调制声明
│   ├── nodes.py            # 唯一源结构 Node
│   ├── _context.py         # 唯一构建上下文、用户来源定位
│   ├── experiment.py       # 实验树、构建 block、失败回滚
│   ├── instructions.py     # 追加指令意图，不编译或访问硬件
│   ├── authoring.py        # 装饰器与自由上下文写法
│   ├── sections.py         # 唯一 Section、section/add/layer 组合 API
│   ├── operations.py       # 可复用操作组合
│   └── _serialization.py   # 源 IR 编码，保留原字段及默认省略规则
├── config/
│   ├── models.py           # qubit、参数值、校准建议与版本记录
│   ├── role_parameters.py  # 只读角色参数
│   ├── editable_parameters.py # 校验更新的参数视图
│   ├── parameters.py       # 通用参数的独立只读快照
│   ├── optics.py           # 光学参数校验
│   ├── hardware.py         # 设备、接线、校准及触发声明
│   ├── descriptor.py       # 声明与静态数据库解析
│   ├── spectrum.py         # Spectrum 配置模型
│   ├── transport.py        # IZ、MoveProfile、搬运标定；无空间编译状态
│   ├── pair_calibration.py
│   └── _serialization.py   # 配置编码，不反向依赖 DSL
├── compiler/
│   ├── preparation.py     # 输入复制、后端验证和扫描结构解析
│   ├── points.py          # 逐点 schedule/lower、采集签名与 sink 契约
│   ├── pipeline.py        # 按原顺序编排阶段
│   ├── artifact.py        # CompiledExperiment、元数据及来源身份
│   ├── scheduling.py      # 通用静态排程与 frame 处理
│   ├── section_schedule.py # Section 放置算法；不定义 Section
│   ├── resources.py       # 排程的资源/时间网格查询
│   ├── timebase.py        # 共享精确时间网格换算，保留既有 native 约束
│   ├── shared_fields.py   # 共享驱动的编译约束
│   ├── spatial.py         # 每次编译的计划位置与搬运路径约束
│   ├── trigger_routes.py  # 逻辑触发与物理路径的绑定校验
│   ├── chunking.py        # 编译时分块及 host 循环生成
│   ├── streaming.py       # 逐点落盘与完成 store 发布
│   ├── store.py           # 已完成编译存储的读取和完整性检查
│   └── _stream_initializer.py # 生成设备并集初始化源码
├── backends/
│   ├── base.py            # 共同后端契约
│   ├── artiq/
│   │   ├── __init__.py    # 按需解析后端符号，不主动加载生成器/mock
│   │   ├── backend.py    # AD9910/TTL 目标展开与量化
│   │   ├── _preparation.py / _square.py / _ram_*.py # 设备路径约束
│   │   ├── _frames.py / _nco.py # 目标相位与 NCO 状态
│   │   ├── _contracts.py / polar_contracts.py # 共享目标/输出记录
│   │   ├── codegen.py / codegen_*.py # 生成 kernel 与 host 源码
│   │   ├── export.py     # 发布源码、计划与验证记录
│   │   ├── mock_drivers.py # 独立执行生成源码的设备模型
│   │   ├── verification.py / verification_*.py # 执行和对照
│   │   ├── validation_polar_output.py # polar 输出验证
│   │   ├── submission.py # 提交顺序与资源限制
│   │   └── waveforms.py  # ARTIQ 目标样本
│   └── artiq_spectrum/
│       ├── __init__.py    # 按需解析混合后端符号
│       ├── backend.py / scheduling.py # 混合时间域与目标展开
│       ├── phase.py       # AWG 载波与调制相位状态
│       ├── awg.py         # segment/step 构造、样本回放与重建
│       ├── validation_awg.py # 独立数学期望样本与回放结果对照
│       ├── dds.py         # DDS 命令构造、原意重建与完整性检查
│       ├── banks.py       # AWG/DDS bank 与容量
│       └── export.py      # 混合导出与触发资源契约
├── experiments/
│   ├── __init__.py         # 轻量命名空间，不加载实验构建模块
│   ├── ramsey.py / cooling.py # 可复用 Ramsey 与光学操作
│   ├── templates.py        # Rabi/Ramsey/RB 完整实验构造
│   ├── _single_qubit_clifford.py # 纯旋转表、枚举与 seeded recovery
│   ├── rb_sequences.py     # 纯一/双比特 Clifford 描述与采样
│   └── rb_sections.py      # 将固定 RB 描述映射到 DSL Section
├── native/
│   ├── __init__.py          # NativeKernelPackage/Module/Stage/Program
│   ├── declarations.py     # 单包声明、边界与绑定契约
│   ├── components.py       # 多组件声明与生命周期
│   ├── sources.py          # 不执行用户代码的静态源码发现
│   ├── audit.py            # 原生调用图与资源审计
│   ├── _generated.py       # 已验证生成类及命名 build 前置节点
│   ├── export.py           # 组合生成控制程序与 native 调用
│   └── component_export.py # 组件实例和会话生成代码
├── reporting/
│   ├── resources.py        # 资源读取与显式运行时源码清单
│   ├── experiment_report.py
│   ├── visualization.py    # 时序/脉冲表
│   ├── output_simulator.py # 最终量化输出查看
│   ├── paged_output.py     # CompilationStore 分页查看
│   ├── viewer_execution.py
│   ├── native_view.py
│   ├── dds_view.py
│   ├── capture_view.py
│   ├── spectrum_report.py
│   ├── spectrum_bank_report.py
│   ├── spectrum_dds_report.py
│   └── assets/             # HTML 和 JS 唯一源码
├── runtime/
│   ├── session.py          # 显式连接；拥有 worker 进程、管道及日志
│   ├── worker.py           # ARTIQ 环境中的设备所有者及生成程序执行
│   ├── streaming.py        # 恢复、预检、换块、shot、最终清理的编排
│   ├── contracts.py        # ShotContext、ObservationCommit、恢复状态类型
│   ├── run_store.py        # journal、资产完整性、提交与显式恢复决定
│   ├── readout.py          # 普通与持久回调协议，不管理相机连接
│   ├── _cleanup.py         # 尝试全部清理；保留主异常，追加清理错误 notes
│   └── spectrum/
│       ├── awg.py          # 有限播放、bank、上传与主机屏障
│       └── dds.py          # 持续 DDS hold、命令完成与停止
├── results/
│   ├── scalar.py           # ResultSet、状态与快照
│   ├── imaging.py          # 原始图像、ROI 和占据；不触发拍照
│   ├── archives.py         # 共用原子 HDF5 写入和 JSON 转换
│   ├── synthetic.py        # 明确标记的模拟数据
│   ├── analysis.py         # 通用拟合与校准建议
│   ├── rb_results.py       # 同 shot 联合统计
│   └── rb_analysis.py      # RB 拟合
├── spectrum_identity.py   # 编译与执行共享的纯标定/硬件指纹
├── spectrum_calibration.py # 标定分析与运行时解析共用，保留根层
├── host_plan.py           # 已编译 host 计划的顺序/布局读取，无编译依赖
├── ir.py                  # 唯一计划事件与采集目标查询
├── lab.py                 # 应用入口：选后端、编译、显式连接
└── _artifact_io.py         # 规范字节、哈希、原子 JSON 发布
```

后端和可复用实验已归入上述包。已迁移模块的平铺路径保留为兼容入口，不再承担
第二份实现；库内部导入规范位置，不经兼容入口绕行。拆分到多个所有者的旧模块按符号
解析，仍使用同一对象；单一所有者的旧模块转发到该实现。
根层并非必须清空：`lab` 是应用入口，`units/ir/host_plan/_artifact_io` 是共享基础能力。
`spectrum_calibration` 同时服务标定分析和运行时解析，保留根层共享边界，不能为按设备
归档而让运行器反向依赖实验或分析流程。
`reporting` 的包入口不主动加载全部查看器或硬件。报告读取实际导出结果与源码快照，
代码预览仍不等于执行记录。

`_artifact_io` 不依赖编译器、运行器或报告。`RunStore`、编译存储和报告各自使用它，
避免为了文件哈希或写 JSON 而导入完整的编译存储模块。编码和持久化顺序保持原契约。

## 职责与依赖方向

| 位置 | 唯一职责 |
| --- | --- |
| `dsl/` | 记录实验意图；Experiment、指令、pulse、Section 和 operation 组合 |
| `config/` | Qubit/设备/接线/标定模型、参数快照和配置解析 |
| `compiler/` | 编译阶段、扫描、排程、采集索引与分块编译 |
| `backends/artiq/` | AD9910/TTL 目标展开、代码生成、独立 mock 执行与验证 |
| `backends/artiq_spectrum/` | 混合时间域、AWG/DDS 计划、bank 和导出 |
| `native/` | 外部原生类的声明、源码固定、审计和组合；不是实验室 kernel 集合 |
| `runtime/` | 显式连接、上传、执行、生命周期、运行 journal 与读出接口 |
| `results/` | 标量/图像结果、存档与数据分析 |
| `reporting/` | 计划、样本、源码和执行证据的展示 |
| `experiments/` | 可复用实验／操作及纯序列数学；DSL 构建与理想算法分别归属，专用实验仍放实验室包 |

`lab.py` 保留使用入口；单位与唯一的计划事件模型继续保持轻量。不会同时建立
`models`、`domain`、`contracts` 三套重复模型。真正共享的数据格式要写清字段、单位、
拥有者和序列化规则，业务规则仍属于各自模块。

依赖约束：配置不调用编译/执行/报告；DSL 不加载驱动；编译器通过后端契约完成
目标展开；runtime 消费产物；reporting 只观察快照。应用和实验室包使用公共 API。
SDK 只在明确的硬件适配入口加载。

### 生成、验证与序列数学

ARTIQ 的 `codegen*` 不加载 mock 或验证器；`export` 编排源码生成、验证和写入。
`mock_drivers` 与 `verification*` 执行生成程序，对照计划的事件、设备字、相位和
host 次序。共享记录格式放在 `_contracts/polar_contracts`，不让生成器依赖验证器私有类。
两种后端的包入口按需解析符号；维护者直接导入所有者，避免读取数据工具时通过包入口
加载整套编译流程。

AWG 的 `validation_awg` 用独立数学公式计算期望样本，再与 `awg` 的有限回放结果
比较；回放／materialization 工具仍共用。DDS 的 `validate_dds_program` 在 `dds.py`
中按保存的原意重建，检查命令、限制和哈希；它是完整性／一致性检查，不是独立的
物理输出模型。两者都不能代替实测。

`experiments._single_qubit_clifford` 与 `experiments.rb_sequences` 只做理想数学，
不依赖 DSL 或实验构造。`templates` 和 `rb_sections` 单向消费这些算法；种子、
Clifford 顺序与 recovery 规则由数学层维护。旧 `templates.clifford_group/rb_sequences`
仍转发到同一算法，不复制第二套枚举。

### Section 只有一份领域定义

`Section`、`section()`、`add()`、`layer()` 属于 `dsl/sections.py`。
编译器处理的是 Section 的放置和资源约束，使用 `compiler/section_schedule.py`
这类明确名字，不再命名为另一份 `compiler/sections.py`，也不复制 Section 类型。
旧 `sections.py` 和 `section_schedule.py` 分别转发到上述唯一实现。

同理，`config/transport.py` 的 `IZ/MoveProfile/TransportConfig` 属于配置，
`compiler/spatial.py` 的 `SpatialState` 属于编译期间的空间状态。旧 transport 入口
按符号转发，不在配置包中重新导入空间编译器。

## 迁移与协作约定

- 同名 `.py` 转 package 时在包入口显式重导出公开 API，不同时保留同名文件和目录。
- 旧入口转发到同一实现，保持对象身份；新内部代码使用规范位置。旧私有路径不是永久公共 API。
- 源码定位仍指向用户实验行。模块迁移产生的来源路径变化与物理语义变化分开检查。
- HTML/JS 使用包资源读取。报告源码 catalog 显式记录实际实现，不复制兼容 shim 充当执行源码。
- 生成 ARTIQ 程序保持独立部署能力，不借分包让 kernel 依赖 DSL 内部实现。
- 测试保留有效断言，按阶段迁移归属；跨层用例继续检查真实消费者，不只检查文件是否存在。

第一阶段本地证据放在忽略目录 `output/refactor/phase1/`。它不是新克隆的前置依赖，
也不替代未来修改后的定向验证。旧发布准备产物位于 `output/release_prep/`，仅代表其
对应源码版本；后续包迁移不能沿用旧 wheel 的安装结论。

第一阶段完成了 24 项定向检查及一次新 wheel 的隔离目录安装/报告资源检查；复用锁定
依赖，不重新安装工具链。legacy 与多组件代表生成 Python 逐字节不变，因此未重编
真实 ARTIQ。组件来源记录修正为实际实验调用行；两份重点 notebook 内容保持不变。
第一阶段没有运行全量 pytest、全部教程或实机验收。

第二阶段把执行与数据各自分包，并将编译时初始化源码生成、纯硬件指纹和采集目标查询
移出运行器或编译器私有位置。`ir.acquisition_targets` 是共享事件查询，旧 compiler
入口仍转发。依赖检查禁止实现导入旧兼容层、results 直接依赖 compiler/runtime、
编译入口与后端直接依赖 runtime。原子归档、journal schema 和写入顺序不变。

执行失败仍按是否进入 kernel、是否完成硬件决定恢复状态；额外清理失败不再盖住主异常
或阻止后续清理。资源所有权与提交边界见[开发指南](development.md)和
[持久执行](streamed_execution.md)。第二阶段验证记录位于忽略目录
`output/refactor/phase2/`，与第一阶段记录分别保存。当前阶段状态见本页开头。

第二阶段实际通过 45 项定向检查：资源生命周期 14 项、持久执行 9 项、结果与归档
10 项、依赖边界/Spectrum/报告 12 项。修改的 Python 文件通过 Ruff lint 和非修改式
格式检查；新 wheel 在隔离目录安装，复用项目锁定依赖，核对公开导入和 129 个源码／
资源文件。两份重点 notebook 字节不变；另两份 Ramsey 教程及生成器仅同步源码路径
说明。未运行全量 pytest、完整 notebook、真实 ARTIQ 重编或实机；排程、相位、native
及代码生成的对应证据保存在本阶段审计记录中。


第三阶段完成 DSL、配置与编译分包，并保持 `Section` 的唯一所有者。配置编码独立于
DSL，host 计划读取独立于编译；搬运配置与编译位置状态分开。编译流程明确分为准备、
逐点编译与产物组装，内存/磁盘两种保留方式共用流程。通用 Section 算法通过明确接口
读取资源与网格；带 Spectrum 配置的普通 ARTIQ 路径仍保留既有 native 网格约束。

第三阶段通过 45 项定向用例（DSL 11、配置 8、编译/排程 16、依赖与搬运/触发 10）。
新旧编译编排在相同输入上的 4 个案例，完整产物、hash 与生成 Python 均相同，其中
包含 NativeProgram；网格边界另与旧 Section 算法比较完整结果。90 个修改的 Python
文件通过 Ruff；新 wheel 隔离目录安装检查了 135 个公开符号和 167 个源码/资源文件，
复用项目锁定依赖。第三阶段没有修改 6 份 notebook，也没有完整教程运行证据；
未运行全量 pytest、真实 ARTIQ 重编或实机。证据保存于 `output/refactor/phase3/`。
第四阶段当前状态见本页开头，后续教程验收与该历史记录分别保存。

第四阶段完成：ARTIQ 生成与 mock 验证分离，Spectrum 样本／命令与后端编排
分开，可复用实验与纯 Clifford 算法分别归属。四份主要教程在最终代码上全新 kernel
离线执行，并比较前后编译语义。完整离线检查暴露的旧测试失配已定向修复、复验；
新 wheel 的公开导入与真实报告资源通过独立安装目录检查。实际范围及限制见
[验证记录](validation.md)，本地证据位于 `output/refactor/phase4/`。
